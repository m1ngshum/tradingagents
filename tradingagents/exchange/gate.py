"""Centralized, pure trade-decision gate.

This module consolidates the fire-level safety checks that decide whether the
bot may trade live this fire. It is the safety heart of the live path: keeping
the logic here (a) makes it unit-testable in isolation and (b) lets the run
scripts express the "compute live-permission ONCE at fire entry, never
re-enable per market" invariant structurally — `evaluate_fire` returns a single
`live_allowed` bool that the caller uses to disable the executor for the whole
fire; nothing downstream can flip it back on.

Two outcome levels (constants used across the run scripts):
  FIRE_HALT   — a fire-level guard tripped: disable live for the ENTIRE fire
                (the run downgrades to paper). Applied once, at fire entry.
  MARKET_VETO — a market-level check failed: skip this ONE market, continue.
                (Reserved for the Phase-2 per-market gate; defined here so both
                levels live in one place.)

`evaluate_fire` is PURE: all live/runtime state (env, breaker status, reconcile
result) is read by the caller and passed in. No clock, no network, no env reads
inside — the same inputs always yield the same verdict, so tests need no mocks.

NOTE: the NOTIONAL_EXPOSURE ceiling is now wired as one more fire-level guard
(Phase 2). A pure `evaluate_market` for the per-market decision (with cost-aware
edge) remains a Phase-2 follow-on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Outcome levels.
FIRE_HALT = "FIRE_HALT"
MARKET_VETO = "MARKET_VETO"
OK = "OK"

# Env strings treated as "off" for boolean flags.
_FALSEY = frozenset({"", "0", "false", "no"})

KILL_SWITCH_ENV = "TRADINGAGENTS_AUTOTRADE_KILL_SWITCH"


def env_flag_active(value: str | None) -> bool:
    """True if an env-var string should be treated as 'on'. Pure given the value."""
    return bool(value) and value.strip().lower() not in _FALSEY


def kill_switch_active() -> bool:
    """Read the out-of-band autotrade kill switch from the environment.

    Impure (reads os.environ) by design — this is the one env adapter both run
    scripts share so the truthy-parsing rule lives in exactly one place. The
    pure gate takes the resulting bool, not the env.
    """
    return env_flag_active(os.environ.get(KILL_SWITCH_ENV))


@dataclass(frozen=True)
class FireVerdict:
    """Fire-level decision: may the bot place ANY live order this fire?"""

    live_allowed: bool
    level: str  # OK or FIRE_HALT
    reason_codes: tuple[str, ...]
    detail: dict

    def __bool__(self) -> bool:
        return self.live_allowed


def evaluate_fire(
    *,
    kill_switch_on: bool,
    breaker_tripped: bool,
    reconcile_ok: bool,
    open_exposure_usd: float = 0.0,
    exposure_budget_usd: float = 0.0,
) -> FireVerdict:
    """Decide whether live trading is permitted at all this fire.

    Any tripped guard => FIRE_HALT (live disabled; caller downgrades to paper).
    Fails closed: callers pass `reconcile_ok=False` when the reconcile result is
    unavailable/ambiguous, which halts here.

    Args:
        kill_switch_on: the out-of-band kill switch is engaged.
        breaker_tripped: the realized-loss / drawdown breaker is tripped.
        reconcile_ok: pre-fire balance + position reconciliation passed.
        open_exposure_usd: cost basis of positions already open (from the fill
            log). Defaults to 0.0.
        exposure_budget_usd: the experiment's hard notional ceiling. 0 disables
            the check (default), so existing callers are unaffected.

    The NOTIONAL_EXPOSURE guard is the REAL exposure cap for a slow-settling
    instrument: the realized-loss breaker only sees settled P&L, but Polymarket
    positions don't resolve for days/weeks, so already-open notional is the only
    thing that actually bounds how much can be at risk. If open exposure has
    reached the budget, no new fire may add more — FIRE_HALT.

    Returns a FireVerdict; `.live_allowed` is False (FIRE_HALT) if ANY guard
    tripped, with every tripped guard listed in `.reason_codes`.
    """
    reasons: list[str] = []
    if kill_switch_on:
        reasons.append("KILL_SWITCH")
    if breaker_tripped:
        reasons.append("LOSS_BREAKER")
    if not reconcile_ok:
        reasons.append("RECONCILE")
    if exposure_budget_usd > 0 and open_exposure_usd >= exposure_budget_usd:
        reasons.append("NOTIONAL_EXPOSURE")
    allowed = not reasons
    return FireVerdict(
        live_allowed=allowed,
        level=OK if allowed else FIRE_HALT,
        reason_codes=tuple(reasons),
        detail={
            "kill_switch_on": kill_switch_on,
            "breaker_tripped": breaker_tripped,
            "reconcile_ok": reconcile_ok,
            "open_exposure_usd": open_exposure_usd,
            "exposure_budget_usd": exposure_budget_usd,
        },
    )


@dataclass(frozen=True)
class MarketVerdict:
    """Per-market decision (MARKET_VETO level): place an order for THIS market,
    or skip it and continue the fire."""

    allow: bool
    reason_code: str  # OK or the veto reason
    detail: dict

    def __bool__(self) -> bool:
        return self.allow


def evaluate_market(
    *,
    direction: str,
    confidence: float,
    min_confidence: float,
    buy_price: float,
    min_edge: float = 0.0,
) -> MarketVerdict:
    """Per-market gate. Pure; ordered short-circuit (first failure wins).

    Args:
        direction: 'BUY_YES' | 'BUY_NO' | 'HOLD'.
        confidence: calibrated win probability for the side being bought.
        min_confidence: floor below which we never trade.
        buy_price: price of the side bought (yes_price for BUY_YES,
            1 - yes_price for BUY_NO) — the caller resolves the side.
        min_edge: cost-aware edge floor (fees + margin). 0 DISABLES the edge
            gate (default), so it's opt-in for the live pilot and changes no
            existing paper behavior.

    EDGE_NET_OF_COST is the honest edge check: a position clears only if
    confidence beats the price by more than trading costs. With min_edge=0 the
    check is off; the pilot sets it to ~fees+margin.
    """
    if direction == "HOLD":
        return MarketVerdict(False, "HOLD", {})
    if confidence < min_confidence:
        return MarketVerdict(
            False, "CONFIDENCE_FLOOR",
            {"confidence": confidence, "min_confidence": min_confidence},
        )
    edge = confidence - buy_price
    if min_edge > 0 and edge < min_edge:
        return MarketVerdict(
            False, "EDGE_NET_OF_COST", {"edge": edge, "min_edge": min_edge},
        )
    return MarketVerdict(True, OK, {"edge": edge})
