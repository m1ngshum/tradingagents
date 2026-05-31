"""Tests for execution-quality metrics + the paper-shadow purity guarantee."""

import inspect

import pytest

from tradingagents.exchange.execution_metrics import compute_execution_metrics


class TestComputeExecutionMetrics:
    def test_empty(self):
        m = compute_execution_metrics([])
        assert m["paper_attempts"] == 0
        assert m["paper_fill_rate"] is None
        assert m["live_fill_rate"] is None

    def test_paper_fill_rate_excludes_skipped(self):
        fills = [
            {"filled": True, "slippage_pp": 1.0, "status": "ok"},
            {"filled": False, "status": "ok"},
            {"status": "SKIPPED", "reason": "cluster_full"},  # audit row, not an attempt
        ]
        m = compute_execution_metrics(fills)
        assert m["paper_attempts"] == 2
        assert m["paper_fill_rate"] == pytest.approx(0.5)

    def test_live_fill_rate(self):
        fills = [
            {"live": True, "outcome": "FILLED"},
            {"live": True, "outcome": "UNFILLED"},
            {"live": True, "outcome": "UNCONFIRMED"},
            {"live": True, "status": "SKIPPED"},  # no outcome -> not an attempt
        ]
        m = compute_execution_metrics(fills)
        assert m["live_attempts"] == 3
        assert m["live_filled"] == 1
        assert m["live_fill_rate"] == pytest.approx(1 / 3)

    def test_slippage_aggregates(self):
        fills = [
            {"filled": True, "slippage_pp": 2.0, "status": "ok"},
            {"filled": True, "slippage_pp": 4.0, "status": "ok"},
        ]
        m = compute_execution_metrics(fills)
        assert m["mean_slippage_pp"] == pytest.approx(3.0)
        assert m["median_slippage_pp"] == pytest.approx(3.0)

    def test_live_realized_bps_is_deferred_not_faked(self):
        m = compute_execution_metrics([{"live": True, "outcome": "FILLED"}])
        assert m["live_realized_vs_quoted_bps"] is None


class TestPaperFillPurity:
    """The paper shadow must stay pure: no executor/network imports, and
    simulate_fill returns ONLY the fill-log schema (PLAN.md §4)."""

    def test_no_executor_or_network_references(self):
        import tradingagents.exchange.paper_fill as pf
        src = inspect.getsource(pf)
        for forbidden in (
            "ClobClient", "import requests", "web3",
            "polymarket_executor", "post_order", "create_market_order",
        ):
            assert forbidden not in src, f"paper_fill must not reference {forbidden!r}"

    def test_simulate_fill_returns_expected_schema(self):
        from tradingagents.exchange.paper_fill import simulate_fill
        out = simulate_fill([{"price": 0.40, "size": 100}], budget_usd=10.0)
        # The fill-log schema simulate_fill is allowed to produce.
        expected = {
            "filled", "filled_usd", "contracts", "vwap", "remaining_budget",
            "levels_consumed", "slippage_pp", "fee_estimate_if_win",
        }
        assert set(out.keys()) == expected
        assert out["filled"] is True

    def test_simulate_fill_empty_book_is_unfilled(self):
        from tradingagents.exchange.paper_fill import simulate_fill
        out = simulate_fill([], budget_usd=10.0)
        assert out["filled"] is False
        assert out["contracts"] == 0.0
