"""Realized-loss circuit breaker for live trading.

The rate limiter caps how MANY times the bot acts; this caps how much it can
LOSE. A bot can stay under the call limit and still bleed its bankroll one
losing trade at a time. This breaker is the backstop that makes unattended
automation safe regardless of whether the strategy has edge.

Two independent trips, checked before any live order is placed:

  1. DAILY LOSS  — cumulative realized loss today >= daily_limit  -> trip
  2. TOTAL DRAWDOWN — peak_equity - current_equity >= drawdown_limit -> trip

Once tripped, `is_tripped()` returns True for the rest of the relevant window
(daily resets at UTC rollover; drawdown trip is sticky until manually cleared,
because a max-drawdown breach means the strategy is broken, not just unlucky).

State is persisted to disk so it survives across CLI invocations / routine
fires within the same window — the same way rate_limiter does it.

Design choices:
  - Realized only. We don't trip on mark-to-market paper swings; only on
    losses that actually closed. Avoids whipsaw from transient price moves.
  - Fail-CLOSED. If the state file is corrupt or P&L can't be read, we treat
    the breaker as TRIPPED. A safety device that fails open is not a safety
    device. (rate_limiter fails open because over-spending LLM tokens is cheap;
    over-losing capital is not.)
  - Env-configurable, with conservative defaults. 0 disables a given limit.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_DAILY_LOSS_LIMIT = "TRADINGAGENTS_DAILY_LOSS_LIMIT_USD"
ENV_DRAWDOWN_LIMIT = "TRADINGAGENTS_MAX_DRAWDOWN_USD"

# Conservative defaults sized to the canary capital ($100). At $40/day max
# exposure (2 orders x $20), a $30 daily loss limit trips well before a full
# wipe. Drawdown limit defaults to 50% of a $100 float.
DEFAULT_DAILY_LOSS_LIMIT = 30.0
DEFAULT_DRAWDOWN_LIMIT = 50.0


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _resolve_limit(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", env_name, raw, default)
        return default
    if v < 0:
        logger.warning("%s=%s is negative; using default %s", env_name, v, default)
        return default
    return v


class LossBreaker:
    """Track realized P&L and trip when daily-loss or drawdown limits breach.

    Args:
        state_file: path to the JSON state file (per-instrument; pass distinct
            paths for polymarket vs stocks so their breakers are independent).
        daily_loss_limit: USD. 0 disables. Default from env or 30.
        drawdown_limit: USD peak-to-trough. 0 disables. Default from env or 50.
    """

    def __init__(
        self,
        state_file: Path,
        daily_loss_limit: float | None = None,
        drawdown_limit: float | None = None,
    ) -> None:
        self._path = Path(state_file)
        self._daily_limit = (
            daily_loss_limit
            if daily_loss_limit is not None
            else _resolve_limit(ENV_DAILY_LOSS_LIMIT, DEFAULT_DAILY_LOSS_LIMIT)
        )
        self._drawdown_limit = (
            drawdown_limit
            if drawdown_limit is not None
            else _resolve_limit(ENV_DRAWDOWN_LIMIT, DEFAULT_DRAWDOWN_LIMIT)
        )

    # ---- state I/O (fail-closed) ----

    def _blank(self) -> dict:
        return {
            "day": _utc_today(),
            "daily_realized_pnl": 0.0,
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "manual_trip": False,
            "_corrupt": False,
        }

    def _load(self) -> dict:
        if not self._path.exists():
            return self._blank()
        try:
            with self._path.open("r", encoding="utf-8") as f:
                s = json.load(f)
            for k in ("day", "daily_realized_pnl", "peak_equity", "current_equity"):
                if k not in s:
                    raise ValueError(f"missing key {k}")
            s.setdefault("manual_trip", False)
            s["_corrupt"] = False
            # Daily counters roll over at UTC date change; equity/peak persist.
            if s["day"] != _utc_today():
                s["day"] = _utc_today()
                s["daily_realized_pnl"] = 0.0
            return s
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.error("LossBreaker state unreadable (%s): failing CLOSED", e)
            blank = self._blank()
            blank["_corrupt"] = True
            return blank

    def _save(self, state: dict) -> None:
        state.pop("_corrupt", None)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f)
        tmp.replace(self._path)  # atomic

    # ---- recording ----

    def record_realized_pnl(self, pnl_usd: float, current_equity: float | None = None) -> None:
        """Record a closed trade's realized P&L. Call after each settlement.

        Optionally pass current_equity to update the drawdown tracker; if
        omitted, equity is adjusted by pnl_usd from its last known value.
        """
        s = self._load()
        try:
            pnl = float(pnl_usd)
        except (TypeError, ValueError):
            logger.warning("non-numeric pnl %r ignored", pnl_usd)
            return
        s["daily_realized_pnl"] = round(s["daily_realized_pnl"] + pnl, 6)
        if current_equity is not None:
            s["current_equity"] = float(current_equity)
        else:
            s["current_equity"] = round(s["current_equity"] + pnl, 6)
        if s["current_equity"] > s["peak_equity"]:
            s["peak_equity"] = s["current_equity"]
        self._save(s)

    def set_equity(self, equity: float) -> None:
        """Seed/refresh equity (e.g. from a live balance reconciliation)."""
        s = self._load()
        s["current_equity"] = float(equity)
        if s["current_equity"] > s["peak_equity"]:
            s["peak_equity"] = s["current_equity"]
        self._save(s)

    def trip_manually(self) -> None:
        """Hard manual trip (sticky until cleared)."""
        s = self._load()
        s["manual_trip"] = True
        self._save(s)

    def reset(self) -> None:
        """Clear all trips + counters. Requires explicit human action."""
        self._save({
            "day": _utc_today(),
            "daily_realized_pnl": 0.0,
            "peak_equity": 0.0,
            "current_equity": 0.0,
            "manual_trip": False,
        })

    # ---- the gate ----

    def is_tripped(self) -> bool:
        """True if NO live order should be placed. Fails CLOSED on corruption."""
        s = self._load()
        if s.get("_corrupt"):
            return True
        if s.get("manual_trip"):
            return True
        # Daily loss: daily_realized_pnl is negative when losing.
        if self._daily_limit > 0 and s["daily_realized_pnl"] <= -self._daily_limit:
            return True
        # Drawdown from peak.
        if self._drawdown_limit > 0:
            dd = s["peak_equity"] - s["current_equity"]
            if dd >= self._drawdown_limit:
                return True
        return False

    def status(self) -> dict:
        s = self._load()
        dd = s["peak_equity"] - s["current_equity"]
        reasons = []
        if s.get("_corrupt"):
            reasons.append("state_corrupt")
        if s.get("manual_trip"):
            reasons.append("manual_trip")
        if self._daily_limit > 0 and s["daily_realized_pnl"] <= -self._daily_limit:
            reasons.append("daily_loss_limit")
        if self._drawdown_limit > 0 and dd >= self._drawdown_limit:
            reasons.append("max_drawdown")
        return {
            "tripped": bool(reasons),
            "reasons": reasons,
            "daily_realized_pnl": round(s["daily_realized_pnl"], 4),
            "daily_loss_limit": self._daily_limit,
            "drawdown": round(dd, 4),
            "drawdown_limit": self._drawdown_limit,
            "peak_equity": round(s["peak_equity"], 4),
            "current_equity": round(s["current_equity"], 4),
        }
