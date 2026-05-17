"""Daily LLM-spend ceiling for the routines.

Each routine fire sums today's `cost_usd` field across already-logged
decisions and refuses to invoke the LLM once the budget is exhausted. The
routine then completes gracefully (state still pushed) instead of bleeding
mid-run when OpenRouter rate-limits or runs out of credit.

Per-routine budgets are intentional (separate env vars for polymarket vs
stocks) so a chatty polymarket day cannot starve the stocks routine.

The script that owns the JSONL passes the day's `decisions-*.jsonl` path
and a budget cap; this module is pure I/O + arithmetic, no LLM calls.

Performance: the tracker reads the JSONL exactly once per process (in
`spent_today()`) and caches the result. Subsequent checks within the
same fire MUST go through `record(cost)` after each new decision is
appended to disk, which updates the cache in lockstep. This eliminates
the per-market O(N) re-scan that would otherwise happen inside the
market loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents.exchange.scoring import load_jsonl_rows

logger = logging.getLogger(__name__)


# Per-model USD pricing (per 1M tokens, (input, output)). When a model isn't
# listed we fall back to sonnet pricing so the ceiling errs on the safe side.
# Update as Anthropic/OpenAI/OpenRouter pricing shifts.
_MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-4.7": (3.0, 15.0),
    "anthropic/claude-sonnet-4.6": (3.0, 15.0),
    "anthropic/claude-sonnet-4.5": (3.0, 15.0),
    "anthropic/claude-sonnet-4": (3.0, 15.0),
    "anthropic/claude-opus-4.7": (15.0, 75.0),
    "anthropic/claude-opus-4.6": (15.0, 75.0),
    "anthropic/claude-opus-4.5": (15.0, 75.0),
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
    "openai/gpt-4.1": (2.5, 10.0),
    "openai/gpt-4o": (2.5, 10.0),
    "openai/gpt-4o-mini": (0.15, 0.6),
}
_DEFAULT_PRICING_USD_PER_MTOK = (3.0, 15.0)


def estimate_llm_cost(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate USD cost from token usage. Falls back to sonnet pricing when
    the model isn't in the table, so unknown models over-report rather than
    under-report against the budget.
    """
    if prompt_tokens < 0 or completion_tokens < 0:
        return 0.0
    in_rate, out_rate = _MODEL_PRICING_USD_PER_MTOK.get(
        model or "", _DEFAULT_PRICING_USD_PER_MTOK
    )
    return (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000


class TokenAccumulator(BaseCallbackHandler):
    """LangChain callback that sums prompt+completion tokens across LLM calls.

    Bound once via `llm.with_config(callbacks=[accumulator])` and reused across
    bull/bear/trader synthesis within a single propagate_market invocation.
    Read `.total_cost_usd(model)` after all calls complete.

    Resilient by design: if a provider doesn't emit token_usage, the
    accumulator stays at 0 rather than raising — the cost gate still works,
    it just under-reports for that call.
    """

    def __init__(self) -> None:
        super().__init__()
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ANN401
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        self.prompt_tokens += int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        )
        self.completion_tokens += int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )

    def total_cost_usd(self, model: str | None) -> float:
        return estimate_llm_cost(model, self.prompt_tokens, self.completion_tokens)


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
        # Lazy-loaded on first spent_today() call; subsequent reads consume
        # the cache. Callers MUST `record(cost)` after appending new rows
        # to keep the cache in sync. See `record()` docstring.
        self._cache_spent: float | None = None

    @property
    def budget_usd(self) -> float:
        return self._budget

    def _initial_load(self) -> float:
        """One-time read of the existing log to seed the cache."""
        if not self._path.exists():
            return 0.0
        date = self._path.stem.removeprefix("decisions-")
        rows = load_jsonl_rows(
            self._path.parent,
            date=date,
            glob_pattern=self._path.name,
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

    def spent_today(self) -> float:
        """Cached cost-sum. First call reads disk; subsequent calls return
        the in-memory tally (updated by `record()`).
        """
        if self._cache_spent is None:
            self._cache_spent = self._initial_load()
        return self._cache_spent

    def record(self, cost_usd: float | None) -> None:
        """Increment the cached spend after a new decision lands on disk.

        Call this AFTER `append_jsonl(decision_log_path, payload)` so the
        cache reflects what is actually persisted. `None` or non-numeric
        cost is treated as 0 (consistent with `spent_today()` semantics).
        """
        if self._cache_spent is None:
            # Trigger a load first so we start from the correct baseline.
            self._cache_spent = self._initial_load()
        if cost_usd is None:
            return
        try:
            self._cache_spent += float(cost_usd)
        except (TypeError, ValueError):
            logger.warning("non-numeric cost passed to record(): %r", cost_usd)

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
