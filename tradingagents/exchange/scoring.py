"""Score paper fills against Polymarket resolution outcomes.

Pure functions:
  - classify_outcome(closed, outcome_prices) -> (MarketOutcome, current_yes_price)
    Reads gamma `/markets` response fields and decides whether the market has
    resolved YES/NO/50-50, is still trading, or is in the UMA dispute window.
  - score_position(fill, outcome, current_yes_price) -> dict
    Computes realized P&L for resolved outcomes and mark-to-market P&L for
    pending positions.

I/O helpers (gamma + filesystem):
  - load_jsonl_rows(path_or_dir, date=None) -> list[dict]
    Read fills from one date or all dates, tolerating corrupted lines.
  - fetch_outcomes(market_ids) -> dict[str, dict]
    Per-market gamma fetch with per-market GammaAPIError isolation.
  - is_uma_finalized(market) -> bool
    True only when umaResolutionStatuses contains "resolved" (excludes proposed
    state where UMA dispute window is still open).

Both score_fills.py and calibrate.py consume these — single source of truth.
"""

from __future__ import annotations

import json
import logging
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


class MarketOutcome(str, Enum):
    PENDING = "PENDING"          # market still open
    YES_WINS = "YES_WINS"        # outcomePrices ~ [1, 0]
    NO_WINS = "NO_WINS"          # outcomePrices ~ [0, 1]
    CANCELED = "CANCELED"        # outcomePrices ~ [0.5, 0.5]
    UNKNOWN = "UNKNOWN"          # closed but neither cleanly resolved nor 50-50


# Tolerance bands for resolution detection. Polymarket settles to exactly 1/0
# in normal flow but legacy markets sometimes show very-close-to-1 floats.
_RESOLVED_HI = 0.99
_RESOLVED_LO = 0.01
_CANCEL_LO = 0.49
_CANCEL_HI = 0.51


def classify_outcome(
    closed: bool,
    outcome_prices: list[float] | None,
) -> tuple[MarketOutcome, float | None]:
    """Decide the market's resolution state from gamma fields.

    Args:
        closed: gamma `closed` boolean.
        outcome_prices: gamma `outcomePrices` already parsed to floats [yes, no].

    Returns:
        (outcome, current_yes_price). current_yes_price is set only for PENDING
        markets and is the live YES price for MTM calculations.
    """
    if not outcome_prices or len(outcome_prices) < 2:
        return (MarketOutcome.UNKNOWN, None)

    yes, no = float(outcome_prices[0]), float(outcome_prices[1])

    if not closed:
        return (MarketOutcome.PENDING, yes)

    # Closed market - determine resolution
    if yes >= _RESOLVED_HI and no <= _RESOLVED_LO:
        return (MarketOutcome.YES_WINS, None)
    if no >= _RESOLVED_HI and yes <= _RESOLVED_LO:
        return (MarketOutcome.NO_WINS, None)
    if _CANCEL_LO <= yes <= _CANCEL_HI and _CANCEL_LO <= no <= _CANCEL_HI:
        return (MarketOutcome.CANCELED, None)
    # Closed but unclean: dispute window, legacy [0,0] data, or other anomaly
    return (MarketOutcome.UNKNOWN, None)


def score_position(
    fill: dict[str, Any],
    outcome: MarketOutcome,
    current_yes_price: float | None,
) -> dict[str, Any]:
    """Compute P&L for one paper-fill position.

    Args:
        fill: dict with at least `direction` (BUY_YES|BUY_NO), `contracts`,
            `filled_usd`, `fee_estimate_if_win`.
        outcome: from classify_outcome.
        current_yes_price: live YES price for MTM (PENDING only).

    Returns dict with:
        status: RESOLVED_WIN | RESOLVED_LOSS | CANCELED | PENDING | UNRESOLVED
        payout_usd: USDC payout received (0 for losses, contract value for wins)
        fee_paid: fee actually paid (only on winning resolves)
        pnl_usd: realized P&L (RESOLVED/CANCELED) or 0.0 otherwise
        roi: pnl_usd / filled_usd (or None if filled_usd is 0)
        mtm_value_usd: PENDING only, current market value of the position
        mtm_pnl_usd: PENDING only, mtm_value_usd - filled_usd
    """
    direction = fill["direction"]
    contracts = float(fill["contracts"])
    filled_usd = float(fill["filled_usd"])
    fee_if_win = float(fill.get("fee_estimate_if_win", 0.0))

    base = {
        "status": "UNRESOLVED",
        "payout_usd": 0.0,
        "fee_paid": 0.0,
        "pnl_usd": 0.0,
        "roi": None,
        "mtm_value_usd": None,
        "mtm_pnl_usd": None,
    }

    if outcome == MarketOutcome.UNKNOWN:
        return base

    if outcome == MarketOutcome.PENDING:
        if current_yes_price is None:
            return base
        if direction == "BUY_YES":
            mtm_value = contracts * current_yes_price
        else:  # BUY_NO
            mtm_value = contracts * (1.0 - current_yes_price)
        return {
            **base,
            "status": "PENDING",
            "mtm_value_usd": round(mtm_value, 6),
            "mtm_pnl_usd": round(mtm_value - filled_usd, 6),
        }

    if outcome == MarketOutcome.CANCELED:
        # 50-50 refund: each contract returns $0.50, no fee charged
        payout = contracts * 0.5
        pnl = payout - filled_usd
        return {
            **base,
            "status": "CANCELED",
            "payout_usd": round(payout, 6),
            "fee_paid": 0.0,
            "pnl_usd": round(pnl, 6),
            "roi": round(pnl / filled_usd, 6) if filled_usd > 0 else None,
        }

    # YES_WINS or NO_WINS: did the position win?
    is_win = (
        (direction == "BUY_YES" and outcome == MarketOutcome.YES_WINS)
        or (direction == "BUY_NO" and outcome == MarketOutcome.NO_WINS)
    )

    if is_win:
        payout = contracts * 1.0
        pnl = payout - fee_if_win - filled_usd
        return {
            **base,
            "status": "RESOLVED_WIN",
            "payout_usd": round(payout, 6),
            "fee_paid": round(fee_if_win, 6),
            "pnl_usd": round(pnl, 6),
            "roi": round(pnl / filled_usd, 6) if filled_usd > 0 else None,
        }

    # Loss: position is worthless
    return {
        **base,
        "status": "RESOLVED_LOSS",
        "payout_usd": 0.0,
        "fee_paid": 0.0,
        "pnl_usd": round(-filled_usd, 6),
        "roi": -1.0 if filled_usd > 0 else None,
    }


# ---------------------------------------------------------------------------
# I/O helpers — shared between score_fills.py and calibrate.py
# ---------------------------------------------------------------------------


def load_jsonl_rows(
    fills_dir: Path,
    date: str | None = None,
    glob_pattern: str = "paper-fills-*.jsonl",
) -> list[dict]:
    """Load fills from one date or all dates under fills_dir.

    Tolerates corrupted JSONL lines (mid-write truncation, partial bytes from
    overlapping fires) — logs a warning and skips the bad line.
    """
    if date:
        # Replace the '*' wildcard with the specific date.
        filename = glob_pattern.replace("*", date)
        paths = [fills_dir / filename]
    else:
        paths = sorted(fills_dir.glob(glob_pattern))

    fills: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    fills.append(json.loads(raw))
                except json.JSONDecodeError as e:
                    logger.warning(
                        "skipping corrupted line %s in %s: %s",
                        lineno, path, e,
                    )
                    continue
    return fills


def is_uma_finalized(market: dict) -> bool:
    """True only when UMA resolution is finalized (not just proposed).

    The Gamma `closed` boolean can be True while UMA is still in the dispute
    window (umaResolutionStatuses contains 'proposed' but not 'resolved').
    Calibration computations should exclude proposed-but-not-final outcomes
    OR report them in a separate column. See PLAN-research-capture-and-cluster-cap.md F12.
    """
    raw = market.get("umaResolutionStatuses")
    if not raw:
        return False
    try:
        statuses = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return False
    return any(s == "resolved" for s in statuses)


def fetch_outcomes(
    market_ids: Iterable[str],
    fetch_market: Any,
) -> dict[str, dict]:
    """Fetch each market once with per-market error isolation.

    Args:
        market_ids: iterable of market IDs to fetch.
        fetch_market: callable taking a market_id, returning a normalised dict
            with at least 'closed', 'yes_price', and (optionally) the raw
            gamma fields needed for `is_uma_finalized`. In practice this is
            polymarket_data.get_market_by_id.

    Returns:
        {market_id: {outcome, current_yes_price, is_finalized}}
    """
    # Import here to avoid circular import (polymarket_data may import scoring helpers).
    from tradingagents.dataflows.polymarket_data import GammaAPIError

    out: dict[str, dict] = {}
    for mid in sorted(set(market_ids)):
        try:
            m = fetch_market(mid)
        except GammaAPIError as e:
            print(f"  warn: market {mid} fetch failed: {e}", file=sys.stderr)
            out[mid] = {
                "outcome": MarketOutcome.UNKNOWN,
                "current_yes_price": None,
                "is_finalized": False,
            }
            continue

        # Normalised dict exposes yes_price + closed; reconstruct prices for classify.
        prices = [m["yes_price"], 1.0 - m["yes_price"]]
        outcome, current_yes = classify_outcome(closed=m["closed"], outcome_prices=prices)
        out[mid] = {
            "outcome": outcome,
            "current_yes_price": current_yes,
            "is_finalized": is_uma_finalized(m),
        }
    return out


def count_fills_by_cluster(
    fills_dir: Path,
    date: str,
    *,
    exclude_statuses: tuple[str, ...] = ("SKIPPED", "ERROR"),
) -> dict[str, int]:
    """Count placed BUY positions per cluster_id in a day's fills.

    Used by the cluster-cap gate in run_polymarket.py (and any future caller
    that needs the same view). SKIPPED rows are audit-trail, not positions,
    and must not count against the cap.
    """
    from collections import Counter
    fill_log = fills_dir / f"paper-fills-{date}.jsonl"
    if not fill_log.exists():
        return {}
    fills = load_jsonl_rows(fills_dir, date=date)
    counts: Counter = Counter()
    for f in fills:
        if f.get("status") in exclude_statuses:
            continue
        cid = f.get("cluster_id")
        if cid:
            counts[cid] += 1
    return dict(counts)
