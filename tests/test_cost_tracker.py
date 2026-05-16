"""Tests for tradingagents/exchange/cost_tracker.py."""

from __future__ import annotations

import pytest
from pathlib import Path

from tradingagents.exchange.cost_tracker import CostTracker
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
