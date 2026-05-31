"""Pre-fire balance & position reconciliation.

Before a live fire, the bot must confirm its view of the world matches
reality. Two drifts kill bots silently:

  1. POSITION DRIFT — the bot's fill log says it holds N positions worth $X,
     but the exchange says something different (a fill the bot recorded never
     settled, or a position resolved/was liquidated without the bot noticing).
  2. BALANCE DRIFT — the bot sizes against `--capital`, but the actual USDC
     balance is lower (prior losses, withdrawals, fees). Sizing against money
     you don't have places orders that fail or over-leverage.

This module is the pure comparator. The exchange-fetch wrapper lives in the
executor; here we just decide, given (expected, actual), whether to proceed.

FAIL-CLOSED: any ambiguity (missing data, unparseable amounts, actual < 0)
returns a HALT verdict. The caller refuses to trade live and flags for human
review rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass


# Tolerance for floating-point / minor-fee balance differences. Drift within
# this band is noise, not a real discrepancy.
_BALANCE_TOLERANCE_USD = 1.0


@dataclass(frozen=True)
class ReconResult:
    ok: bool                 # True => safe to trade live this fire
    halt_reasons: tuple[str, ...]
    detail: dict

    def __bool__(self) -> bool:
        return self.ok


def reconcile(
    *,
    expected_open_positions: int,
    actual_open_positions: int | None,
    intended_capital_usd: float,
    actual_balance_usd: float | None,
    balance_tolerance_usd: float = _BALANCE_TOLERANCE_USD,
) -> ReconResult:
    """Decide whether live trading is safe given expected vs actual state.

    Args:
        expected_open_positions: count the bot believes it holds (from fill log).
        actual_open_positions: count the exchange reports. None => couldn't fetch.
        intended_capital_usd: the --capital the bot will size against.
        actual_balance_usd: real USDC available. None => couldn't fetch.
        balance_tolerance_usd: balance diffs within this are treated as noise.

    Returns ReconResult; .ok is False (HALT) on ANY of:
        - couldn't fetch actual positions or balance (None)
        - actual position count != expected (drift)
        - actual balance < intended capital - tolerance (insufficient funds)
        - negative / nonsensical actual values
    """
    reasons: list[str] = []
    detail: dict = {
        "expected_open_positions": expected_open_positions,
        "actual_open_positions": actual_open_positions,
        "intended_capital_usd": intended_capital_usd,
        "actual_balance_usd": actual_balance_usd,
    }

    # --- position reconciliation ---
    if actual_open_positions is None:
        reasons.append("positions_unavailable")
    elif actual_open_positions < 0:
        reasons.append("positions_negative")
    elif actual_open_positions != expected_open_positions:
        reasons.append("position_drift")
        detail["position_drift"] = actual_open_positions - expected_open_positions

    # --- balance reconciliation ---
    if actual_balance_usd is None:
        reasons.append("balance_unavailable")
    elif actual_balance_usd < 0:
        reasons.append("balance_negative")
    elif actual_balance_usd < intended_capital_usd - balance_tolerance_usd:
        reasons.append("insufficient_balance")
        detail["balance_shortfall_usd"] = round(
            intended_capital_usd - actual_balance_usd, 4
        )

    return ReconResult(ok=not reasons, halt_reasons=tuple(reasons), detail=detail)


def count_open_positions_from_fills(fill_rows: list[dict]) -> int:
    """Count positions the bot believes are open, from its fill log.

    A position is 'open' if it was a confirmed FILL and has not been recorded
    as resolved/closed. We count rows whose outcome is FILLED (live) or that
    were paper-filled, minus any later close record for the same market.

    This is deliberately simple — for the canary (max ~2 positions/day) an
    exact count is easy. If it ever needs lot-level tracking, replace this.
    """
    open_by_market: dict[str, int] = {}
    for r in fill_rows:
        mid = r.get("market_id")
        if not mid:
            continue
        status = r.get("status")
        outcome = r.get("outcome")
        # Confirmed live fill or legacy paper fill
        is_open = (
            outcome == "FILLED"
            or (status not in ("SKIPPED", "ERROR", "UNFILLED", "UNCONFIRMED")
                and r.get("filled") is True)
        )
        if is_open:
            open_by_market[mid] = open_by_market.get(mid, 0) + 1
        # A close/resolution record zeroes the market
        if status in ("CLOSED", "RESOLVED_WIN", "RESOLVED_LOSS", "CANCELED"):
            open_by_market[mid] = 0
    return sum(1 for v in open_by_market.values() if v > 0)


def open_exposure_from_fills(fill_rows: list[dict]) -> float:
    """Sum the cost basis (filled_usd) of positions the bot believes are open.

    Mirrors count_open_positions_from_fills' open-detection, but accumulates the
    dollar cost basis instead of a count, and zeroes a market's exposure on a
    close/resolution record. Feeds the NOTIONAL_EXPOSURE ceiling in gate.py:
    this is the real exposure cap for a slow-settling instrument whose positions
    don't resolve for days/weeks (so the realized-loss breaker can't bound it).

    A missing/unparseable filled_usd contributes 0. Callers pair this with
    reconcile() (which HALTs on unreadable exchange state), so a corrupt fill log
    can't silently uncap exposure.
    """
    exposure_by_market: dict[str, float] = {}
    for r in fill_rows:
        mid = r.get("market_id")
        if not mid:
            continue
        status = r.get("status")
        outcome = r.get("outcome")
        is_open = (
            outcome == "FILLED"
            or (status not in ("SKIPPED", "ERROR", "UNFILLED", "UNCONFIRMED")
                and r.get("filled") is True)
        )
        if is_open:
            try:
                amt = float(r.get("filled_usd") or 0.0)
            except (TypeError, ValueError):
                amt = 0.0
            exposure_by_market[mid] = exposure_by_market.get(mid, 0.0) + amt
        if status in ("CLOSED", "RESOLVED_WIN", "RESOLVED_LOSS", "CANCELED"):
            exposure_by_market[mid] = 0.0
    return round(sum(v for v in exposure_by_market.values() if v > 0), 6)
