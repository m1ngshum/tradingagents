"""Tests for PolymarketExecutor sizing logic."""

import pytest
from tradingagents.agents.schemas import PolymarketDecision, PolymarketDirection
from tradingagents.exchange.polymarket_executor import (
    PolymarketExecutionDisabled,
    PolymarketExecutor,
    size_polymarket_order,
)


def _decision(direction, confidence, yes_price=0.30):
    return PolymarketDecision(
        market_id="test-market",
        question="Will X happen?",
        direction=direction,
        confidence=confidence,
        rationale="test",
        yes_price_at_analysis=yes_price,
        cycle_ts=0,
    )


# ---------------------------------------------------------------------------
# Sizing tests
# ---------------------------------------------------------------------------

class TestSizePolymarketOrder:
    def test_hold_returns_zero(self):
        result = size_polymarket_order(_decision(PolymarketDirection.HOLD, 0.8), 10_000)
        assert result["usd"] == 0.0
        assert result["reason"] == "HOLD"

    def test_low_confidence_returns_zero(self):
        result = size_polymarket_order(_decision(PolymarketDirection.BUY_YES, 0.54), 10_000)
        assert result["usd"] == 0.0
        assert "confidence" in result["reason"]

    def test_no_edge_returns_zero(self):
        # confidence 0.60 <= buy_price 0.65 → no edge
        result = size_polymarket_order(_decision(PolymarketDirection.BUY_YES, 0.60, yes_price=0.65), 10_000)
        assert result["usd"] == 0.0
        assert "no edge" in result["reason"]

    def test_buy_yes_positive_edge(self):
        # confidence 0.70, yes_price 0.30 → edge=0.40, kelly=0.40/0.70≈0.571, half=0.286, cap 0.20
        result = size_polymarket_order(_decision(PolymarketDirection.BUY_YES, 0.70, yes_price=0.30), 10_000)
        assert result["usd"] > 0
        assert result["fraction"] <= 0.20

    def test_buy_no_positive_edge(self):
        # BUY_NO at yes_price=0.80 → buy_price=0.20, confidence=0.70 → edge=0.50
        result = size_polymarket_order(_decision(PolymarketDirection.BUY_NO, 0.70, yes_price=0.80), 10_000)
        assert result["usd"] > 0
        assert result["fraction"] <= 0.20

    def test_price_too_low_skipped(self):
        result = size_polymarket_order(_decision(PolymarketDirection.BUY_YES, 0.80, yes_price=0.01), 10_000)
        assert result["usd"] == 0.0
        assert "min" in result["reason"]

    def test_price_too_high_skipped(self):
        result = size_polymarket_order(_decision(PolymarketDirection.BUY_YES, 0.98, yes_price=0.98), 10_000)
        assert result["usd"] == 0.0
        assert "max" in result["reason"]

    def test_max_fraction_cap(self):
        # Huge edge should still be capped at 20%
        result = size_polymarket_order(_decision(PolymarketDirection.BUY_YES, 0.99, yes_price=0.05), 10_000)
        assert result["fraction"] == pytest.approx(0.20)
        assert result["usd"] == pytest.approx(2_000.0)


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------

class TestPolymarketExecutorGate:
    def test_raises_without_private_key(self, monkeypatch):
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
        monkeypatch.setenv("POLYMARKET_KEY", "k")
        monkeypatch.setenv("POLYMARKET_SECRET", "s")
        monkeypatch.setenv("POLYMARKET_PASSPHRASE", "p")
        monkeypatch.setenv("POLYMARKET_FUNDER", "0xabc")
        with pytest.raises(PolymarketExecutionDisabled, match="POLYMARKET_PRIVATE_KEY"):
            PolymarketExecutor()

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xdeadbeef")
        monkeypatch.delenv("POLYMARKET_KEY", raising=False)
        monkeypatch.setenv("POLYMARKET_SECRET", "s")
        monkeypatch.setenv("POLYMARKET_PASSPHRASE", "p")
        monkeypatch.setenv("POLYMARKET_FUNDER", "0xabc")
        with pytest.raises(PolymarketExecutionDisabled, match="POLYMARKET_KEY"):
            PolymarketExecutor()
