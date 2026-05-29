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


def test_short_order_uses_integer_qty_not_notional(monkeypatch):
    """Alpaca rejects fractional shorts (code 42210000). SHORT orders must use
    qty=<int> (floored from notional/price), not notional=<float>."""
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    fake_order = MagicMock(); fake_order.id = "order-789"; fake_order.status = "accepted"
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc, \
         patch("tradingagents.exchange.alpaca_executor.MarketOrderRequest") as mock_req:
        mock_client = MagicMock(); mock_client.submit_order.return_value = fake_order
        mock_tc.return_value = mock_client
        executor = AlpacaExecutor()
        # SHORT INTU @ $378.29, conf 0.57 → half-Kelly 0.07 of $10k = $700 → 1 whole share
        decision = StockDecision(
            ticker="INTU", direction=StockDirection.SHORT,
            confidence=0.57, rationale="Test.", price_at_analysis=378.29,
        )
        result = executor.place_order(decision, capital_usd=10000)
    assert result["status"] == "SUBMITTED"
    # MarketOrderRequest must be called with qty (int >= 1), NOT notional
    kwargs = mock_req.call_args.kwargs
    assert "notional" not in kwargs or kwargs.get("notional") is None, \
        f"SHORT order must not use notional, got kwargs={kwargs}"
    assert isinstance(kwargs["qty"], int), f"qty must be int for SHORT, got {type(kwargs['qty'])}"
    assert kwargs["qty"] >= 1, f"qty must be >= 1, got {kwargs['qty']}"


def test_short_order_skipped_when_qty_rounds_to_zero(monkeypatch):
    """If notional / price < 1 share, SKIP rather than send an invalid order."""
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_tc.return_value = MagicMock()
        executor = AlpacaExecutor()
        # SHORT a $500 stock with only $100 of sizing → 0 whole shares → skip
        decision = StockDecision(
            ticker="BRK.A", direction=StockDirection.SHORT,
            confidence=0.56, rationale="Test.", price_at_analysis=500.0,
        )
        result = executor.place_order(decision, capital_usd=1000)
    assert result["status"] == "SKIPPED"
    assert "fractional" in result["reason"].lower() or "qty" in result["reason"].lower()


def test_long_order_still_uses_notional(monkeypatch):
    """LONG orders should keep using notional (Alpaca accepts fractional longs)."""
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    fake_order = MagicMock(); fake_order.id = "order-long"; fake_order.status = "accepted"
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc, \
         patch("tradingagents.exchange.alpaca_executor.MarketOrderRequest") as mock_req:
        mock_client = MagicMock(); mock_client.submit_order.return_value = fake_order
        mock_tc.return_value = mock_client
        executor = AlpacaExecutor()
        executor.place_order(_long_decision(), capital_usd=10000)
    kwargs = mock_req.call_args.kwargs
    assert "notional" in kwargs and kwargs["notional"] > 0, \
        f"LONG must keep using notional, got kwargs={kwargs}"


def test_order_error_returns_error_status(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = RuntimeError("connection timeout")
        mock_tc.return_value = mock_client
        executor = AlpacaExecutor()
        result = executor.place_order(_long_decision(), capital_usd=10000)
    assert result["status"] == "ERROR"
    assert "connection timeout" in result["reason"]


# ---- account equity + position fetch (loss breaker / reconciliation inputs) ----

def _executor(monkeypatch, client):
    monkeypatch.setenv("ALPACA_API_KEY", "fake_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake_secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    with patch("tradingagents.exchange.alpaca_executor.TradingClient") as mock_tc:
        mock_tc.return_value = client
        return AlpacaExecutor()


def test_get_account_equity_returns_float(monkeypatch):
    client = MagicMock()
    client.get_account.return_value = MagicMock(equity="10250.75")
    ex = _executor(monkeypatch, client)
    assert ex.get_account_equity() == 10250.75


def test_get_account_equity_none_on_error(monkeypatch):
    """Must return None (not 0) so the reconciler HALTS rather than trading
    against a phantom zero balance."""
    client = MagicMock()
    client.get_account.side_effect = RuntimeError("api down")
    ex = _executor(monkeypatch, client)
    assert ex.get_account_equity() is None


def test_count_open_positions(monkeypatch):
    client = MagicMock()
    client.get_all_positions.return_value = [MagicMock(), MagicMock(), MagicMock()]
    ex = _executor(monkeypatch, client)
    assert ex.count_open_positions() == 3


def test_count_open_positions_none_on_error(monkeypatch):
    client = MagicMock()
    client.get_all_positions.side_effect = RuntimeError("api down")
    ex = _executor(monkeypatch, client)
    assert ex.count_open_positions() is None
