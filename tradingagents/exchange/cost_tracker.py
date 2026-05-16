"""Daily LLM-spend ceiling for the routines.

Each routine fire sums today's `cost_usd` field across already-logged
decisions and refuses to invoke the LLM once the budget is exhausted. The
routine then completes gracefully (state still pushed) instead of bleeding
mid-run when OpenRouter rate-limits or runs out of credit.

Per-routine budgets are intentional (separate env vars for polymarket vs
stocks) so a chatty polymarket day cannot starve the stocks routine.

The script that owns the JSONL passes the day's `decisions-*.jsonl` path
and a budget cap; this module is pure I/O + arithmetic, no LLM calls.
"""

from __future__ import annotations

import logging
from pathlib import Path

from tradingagents.exchange.scoring import load_fills_jsonl

logger = logging.getLogger(__name__)


class CostTracker:
    """Sum cost_usd across today's decisions and gate further LLM calls."""

    def __init__(
        self,
        decision_log_path: Path,
        budget_usd: float,
    ) -> None:
        if budget_usd < 0:
            raise ValueError(f"budget_usd must be non-negative, got {budget_usd}")
        self._path = decision_log_path
        self._budget = budget_usd
        # In-memory tally — we recompute from disk on each call so concurrent
        # fires see each other's costs, but cache between checks within one
        # process. Disk read is microseconds at <100 decisions/day.
        self._cache_spent: float | None = None

    @property
    def budget_usd(self) -> float:
        return self._budget

    def spent_today(self) -> float:
        """Sum cost_usd across decisions written today. Tolerant of missing
        fields (legacy entries default to 0)."""
        if not self._path.exists():
            return 0.0
        # Glob pattern matches the single dated file.
        date = self._path.stem.removeprefix("decisions-")
        glob_pattern = self._path.name  # exact filename
        # load_fills_jsonl already handles corrupted lines.
        rows = load_fills_jsonl(
            self._path.parent,
            date=date,
            glob_pattern=glob_pattern,
        )
        spent = 0.0
        for r in rows:
            cost = r.get("cost_usd")
            if cost is None:
                continue
            try:
                spent += float(cost)
            except (TypeError, ValueError):
                logger.warning("non-numeric cost_usd in %s: %r", self._path, cost)
                continue
        return spent

    def remaining(self) -> float:
        return max(0.0, self._budget - self.spent_today())

    def is_exhausted(self) -> bool:
        """True if no LLM call should be made (budget == 0 disables tracking)."""
        if self._budget <= 0:
            return False  # 0 = unlimited / disabled
        return self.spent_today() >= self._budget

    def status(self) -> dict:
        """Snapshot dict for logging / SKIPPED-row reasons."""
        spent = self.spent_today()
        return {
            "spent_today_usd": round(spent, 4),
            "budget_usd": self._budget,
            "remaining_usd": round(max(0.0, self._budget - spent), 4),
            "exhausted": self._budget > 0 and spent >= self._budget,
        }
