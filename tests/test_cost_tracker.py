"""Tests for tradingagents/exchange/cost_tracker.py."""

from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace

from tradingagents.exchange.cost_tracker import (
    CostTracker,
    TokenAccumulator,
    estimate_llm_cost,
)
from tradingagents.exchange.io_utils import append_jsonl


def test_empty_log_zero_spent(tmp_path: Path):
    log = tmp_path / "decisions-2026-05-16.jsonl"
    ct = CostTracker(decision_log_path=log, budget_usd=15.0)
    assert ct.spent_today() == 0.0
    assert ct.remaining() == 15.0
    assert ct.is_exhausted() is False


def test_sums_cost_usd_across_rows(tmp_path: Path):
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"cost_usd": 0.05})
    append_jsonl(log, {"cost_usd": 0.10})
    append_jsonl(log, {"cost_usd": 0.20})
    ct = CostTracker(decision_log_path=log, budget_usd=1.0)
    assert ct.spent_today() == pytest.approx(0.35)
    assert ct.remaining() == pytest.approx(0.65)


def test_missing_cost_field_treated_as_zero(tmp_path: Path):
    """Legacy decision rows without cost_usd must not crash the gate."""
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"direction": "HOLD"})  # no cost_usd
    append_jsonl(log, {"direction": "BUY_YES", "cost_usd": 0.10})
    ct = CostTracker(decision_log_path=log, budget_usd=1.0)
    assert ct.spent_today() == pytest.approx(0.10)


def test_non_numeric_cost_field_treated_as_zero(tmp_path: Path):
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"cost_usd": "n/a"})
    append_jsonl(log, {"cost_usd": None})
    append_jsonl(log, {"cost_usd": 0.25})
    ct = CostTracker(decision_log_path=log, budget_usd=1.0)
    assert ct.spent_today() == pytest.approx(0.25)


def test_exhausted_when_spent_at_or_above_budget(tmp_path: Path):
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"cost_usd": 15.0})
    ct = CostTracker(decision_log_path=log, budget_usd=15.0)
    assert ct.is_exhausted() is True
    assert ct.remaining() == 0.0


def test_budget_zero_disables_gate(tmp_path: Path):
    """Pass budget=0 to disable the ceiling entirely (unbounded mode)."""
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"cost_usd": 999.99})
    ct = CostTracker(decision_log_path=log, budget_usd=0.0)
    assert ct.is_exhausted() is False
    assert ct.remaining() == 0.0  # budget=0 → no remaining, but not exhausted


def test_negative_budget_rejected():
    with pytest.raises(ValueError):
        CostTracker(decision_log_path=Path("/tmp/x"), budget_usd=-1.0)


def test_status_snapshot(tmp_path: Path):
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"cost_usd": 0.30})
    ct = CostTracker(decision_log_path=log, budget_usd=1.00)
    s = ct.status()
    assert s["spent_today_usd"] == pytest.approx(0.30)
    assert s["budget_usd"] == 1.00
    assert s["remaining_usd"] == pytest.approx(0.70)
    assert s["exhausted"] is False


def test_missing_log_file_is_zero(tmp_path: Path):
    """First fire of the day: log file doesn't exist yet."""
    log = tmp_path / "decisions-2026-05-16.jsonl"
    ct = CostTracker(decision_log_path=log, budget_usd=15.0)
    assert ct.spent_today() == 0.0
    assert ct.is_exhausted() is False


def test_record_increments_in_process(tmp_path: Path):
    """record() updates the in-memory cache without re-reading disk."""
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"cost_usd": 0.10})
    ct = CostTracker(decision_log_path=log, budget_usd=1.0)
    assert ct.spent_today() == pytest.approx(0.10)
    ct.record(0.25)
    assert ct.spent_today() == pytest.approx(0.35)
    ct.record(0.50)
    assert ct.spent_today() == pytest.approx(0.85)


def test_record_handles_none_and_non_numeric(tmp_path: Path):
    """Callers may pass None or junk — must not raise, must not crash."""
    log = tmp_path / "decisions-2026-05-16.jsonl"
    ct = CostTracker(decision_log_path=log, budget_usd=1.0)
    ct.record(None)
    ct.record("not a number")
    assert ct.spent_today() == 0.0


def test_record_triggers_initial_load_if_needed(tmp_path: Path):
    """If record() is the first call, it must seed from disk first."""
    log = tmp_path / "decisions-2026-05-16.jsonl"
    append_jsonl(log, {"cost_usd": 0.10})
    ct = CostTracker(decision_log_path=log, budget_usd=1.0)
    ct.record(0.20)  # before any spent_today() call
    assert ct.spent_today() == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Token accumulator + cost estimator (wire-up for the budget gate)
# ---------------------------------------------------------------------------


def test_estimate_llm_cost_known_model():
    # sonnet pricing: $3/MTok in, $15/MTok out
    # 1000 in + 500 out = 0.003 + 0.0075 = 0.0105
    assert estimate_llm_cost("anthropic/claude-sonnet-4.6", 1000, 500) == pytest.approx(0.0105)


def test_estimate_llm_cost_unknown_model_falls_back_to_sonnet():
    """Unknown models should over-report (safe side) using sonnet pricing,
    so the daily budget gate fires earlier rather than later for new models."""
    known = estimate_llm_cost("anthropic/claude-sonnet-4.6", 1000, 500)
    unknown = estimate_llm_cost("some-future-model", 1000, 500)
    assert known == unknown


def test_estimate_llm_cost_zero_tokens():
    assert estimate_llm_cost("anthropic/claude-sonnet-4.6", 0, 0) == 0.0


def test_estimate_llm_cost_negative_tokens_returns_zero():
    """Defensive: negative token counts shouldn't produce negative cost."""
    assert estimate_llm_cost("anthropic/claude-sonnet-4.6", -100, 500) == 0.0


def _fake_llm_response(prompt_tokens: int, completion_tokens: int):
    """Mimics langchain LLMResult.llm_output shape that ChatOpenAI emits."""
    return SimpleNamespace(
        llm_output={
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        }
    )


def test_token_accumulator_sums_across_calls():
    acc = TokenAccumulator()
    acc.on_llm_end(_fake_llm_response(100, 50))
    acc.on_llm_end(_fake_llm_response(200, 75))
    acc.on_llm_end(_fake_llm_response(300, 25))
    assert acc.prompt_tokens == 600
    assert acc.completion_tokens == 150
    # 600 * 3 + 150 * 15 = 1800 + 2250 = 4050 µUSD = 0.004050
    assert acc.total_cost_usd("anthropic/claude-sonnet-4.6") == pytest.approx(0.00405)


def test_token_accumulator_handles_missing_usage():
    """Some adapters don't emit token_usage; the accumulator must not crash."""
    acc = TokenAccumulator()
    acc.on_llm_end(SimpleNamespace(llm_output=None))
    acc.on_llm_end(SimpleNamespace(llm_output={}))
    acc.on_llm_end(SimpleNamespace(llm_output={"token_usage": {}}))
    assert acc.total_cost_usd("anthropic/claude-sonnet-4.6") == 0.0


def test_token_accumulator_handles_alt_field_names():
    """Some adapters (Anthropic-native) use input_tokens/output_tokens."""
    acc = TokenAccumulator()
    acc.on_llm_end(SimpleNamespace(
        llm_output={"usage": {"input_tokens": 100, "output_tokens": 50}}
    ))
    assert acc.prompt_tokens == 100
    assert acc.completion_tokens == 50


def test_token_accumulator_is_callback_compatible():
    """LangChain probes framework attrs (raise_error, ignore_*) during
    dispatch. Inheriting from BaseCallbackHandler gives us no-op defaults.
    Regression: a prior version stubbed only on_* via __getattr__ and
    crashed when langchain looked up handler.raise_error."""
    from langchain_core.callbacks import BaseCallbackHandler

    acc = TokenAccumulator()
    assert isinstance(acc, BaseCallbackHandler)
    # The framework attrs that bit us before — these MUST resolve to falsy
    # defaults, not raise AttributeError.
    assert acc.raise_error is False
    assert acc.ignore_llm is False
    assert acc.ignore_chain is False
    assert acc.ignore_agent is False
    assert acc.ignore_retriever is False
    assert acc.ignore_chat_model is False
