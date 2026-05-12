import pytest
from pydantic import ValidationError
from tradingagents.agents.schemas import StockDecision, StockDirection


def test_stock_direction_values():
    assert StockDirection.LONG == "LONG"
    assert StockDirection.SHORT == "SHORT"
    assert StockDirection.HOLD == "HOLD"


def test_stock_decision_valid():
    d = StockDecision(
        ticker="AAPL",
        direction=StockDirection.LONG,
        confidence=0.72,
        rationale="Strong earnings momentum and improving margins.",
        price_at_analysis=185.50,
        horizon_days=5,
    )
    assert d.ticker == "AAPL"
    assert d.confidence == 0.72
    assert d.horizon_days == 5


def test_stock_decision_confidence_bounds():
    with pytest.raises(ValidationError):
        StockDecision(
            ticker="AAPL", direction=StockDirection.LONG,
            confidence=1.5, rationale="x", price_at_analysis=100.0,
        )
    with pytest.raises(ValidationError):
        StockDecision(
            ticker="AAPL", direction=StockDirection.LONG,
            confidence=-0.1, rationale="x", price_at_analysis=100.0,
        )


def test_stock_decision_price_positive():
    with pytest.raises(ValidationError):
        StockDecision(
            ticker="AAPL", direction=StockDirection.LONG,
            confidence=0.6, rationale="x", price_at_analysis=-1.0,
        )
