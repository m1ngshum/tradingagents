import os
import pytest
from unittest.mock import MagicMock, patch
from tradingagents.agents.schemas import StockDecision, StockDirection
from tradingagents.exchange.alpaca_executor import AlpacaExecutor, AlpacaExecutionDisabled, size_stock_order


def _long_decision(ticker="AAPL", price=185.0, conf=0.70) -> StockDecision:
    return StockDecision(
        ticker=ticker, direction=StockDirection.LONG,
        confidence=conf, rationale="Test.", price_at_analysis=price,
    )


def _short_decision(ticker="TSLA", price=250.0, conf=0.68) -> StockDecision:
    return StockDecision(
        ticker=ticker, direction=StockDirection.SHORT,
        confidence=conf, rationale="Test.", price_at_analysis=price,
    )


def _hold_decision(ticker="MSFT", price=420.0, conf=0.50) -> StockDecision:
    return StockDecision(
        ticker=ticker, direction=StockDirection.HOLD,
        confidence=conf, rationale="Test.", price_at_analysis=price,
    )


def test_size_stock_order_hold_returns_zero():
    result = size_stock_order(_hold_decision(), capital_usd=10000)
    assert result["usd"] == 0.0
    assert result["reason"] == "HOLD"


def test_size_stock_order_low_confidence_skipped():
    low = StockDecision(ticker="AAPL", direction=StockDirection.LONG, confidence=0.50, rationale="x", price_at_analysis=100.0)
    result = size_stock_order(low, capital_usd=10000)
    assert result["usd"] == 0.0
    assert "confidence" in result["reason"]


def test_size_stock_order_long_positive():
    result = size_stock_order(_long_decision(conf=0.70), capital_usd=10000)
    assert result["usd"] > 0
    assert result["fraction"] <= 0.10  # capped at 10%


def test_size_stock_order_short_positive():
    result = size_stock_order(_short_decision(conf=0.68), capital_usd=10000)
    assert result["usd"] > 0


def test_gates_blocked_without_api_key(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(AlpacaExecutionDisabled, match="ALPACA_API_KEY"):
        AlpacaExecutor()


def test_gates_blocked_without_secret_key(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(AlpacaExecutionDisabled, match="ALPACA_SECRET_KEY"):
        AlpacaExecutor()


def test_gates_pass_in_paper_mode(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_tc.return_value = MagicMock()
        executor = AlpacaExecutor()
    assert executor is not None


def test_hold_decision_returns_skipped(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_tc.return_value = MagicMock()
        executor = AlpacaExecutor()
    result = executor.place_order(_hold_decision(), capital_usd=10000)
    assert result["status"] == "SKIPPED"
    assert result["reason"] == "HOLD"


def test_long_order_submitted(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    fake_order = MagicMock()
    fake_order.id = "order-123"
    fake_order.status = "accepted"
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_client = MagicMock()
        mock_client.submit_order.return_value = fake_order
        mock_tc.return_value = mock_client
        executor = AlpacaExecutor()
        result = executor.place_order(_long_decision(), capital_usd=10000)
    assert result["status"] == "SUBMITTED"
    assert result["order_id"] == "order-123"
    assert result["side"] == "buy"


def test_short_order_submitted(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    fake_order = MagicMock()
    fake_order.id = "order-456"
    fake_order.status = "accepted"
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_client = MagicMock()
        mock_client.submit_order.return_value = fake_order
        mock_tc.return_value = mock_client
        executor = AlpacaExecutor()
        result = executor.place_order(_short_decision(), capital_usd=10000)
    assert result["status"] == "SUBMITTED"
    assert result["side"] == "sell"


def test_zero_sizing_returns_skipped(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_tc.return_value = MagicMock()
        executor = AlpacaExecutor()
        with patch("tradingagents.exchange.alpaca_executor.size_stock_order", return_value={"usd": 0.0, "fraction": 0.0, "reason": "negative Kelly"}):
            result = executor.place_order(_long_decision(), capital_usd=10000)
    assert result["status"] == "SKIPPED"
