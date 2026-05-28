import pytest
from pydantic import ValidationError
from unittest.mock import MagicMock, patch
from tradingagents.agents.schemas import StockDecision, StockDirection
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG


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


def _mock_graph() -> TradingAgentsGraph:
    config = {**DEFAULT_CONFIG, "llm_provider": "openrouter", "deep_think_llm": "test", "quick_think_llm": "test"}
    with patch("tradingagents.graph.trading_graph.create_llm_client") as mock_llm:
        mock_client = MagicMock()
        mock_client.get_llm.return_value = MagicMock()
        mock_llm.return_value = mock_client
        return TradingAgentsGraph(config=config)


def test_propagate_stock_hold_on_empty_news():
    graph = _mock_graph()
    with patch("tradingagents.dataflows.polymarket_news.search_event_news", return_value=[]):
        _, decision = graph.propagate_stock("AAPL", price=185.0)
    assert isinstance(decision, StockDecision)
    assert decision.direction == StockDirection.HOLD
    assert decision.confidence == 0.0
    assert "LOW_CONFIDENCE" in decision.rationale


def test_propagate_stock_returns_decision_with_news():
    graph = _mock_graph()
    fake_news = [{"title": "Apple beats earnings", "text": "Apple reported strong Q2.", "published_date": "2026-05-10"}]
    fake_decision = StockDecision(
        ticker="AAPL", direction=StockDirection.LONG,
        confidence=0.70, rationale="Strong earnings.", price_at_analysis=185.0,
    )
    bull_update = {"investment_debate_state": {"bull_history": "Bull case here.", "bear_history": "", "history": "", "current_response": "", "judge_decision": "", "count": 1}}
    bear_update = {"investment_debate_state": {"bull_history": "Bull case here.", "bear_history": "Bear case here.", "history": "", "current_response": "", "judge_decision": "", "count": 2}}
    with patch("tradingagents.dataflows.polymarket_news.search_event_news", return_value=fake_news), \
         patch("yfinance.Ticker") as mock_yf:
        mock_yf.return_value.history.return_value = MagicMock(empty=True)
        bull_node = MagicMock(return_value=bull_update)
        bear_node = MagicMock(return_value=bear_update)
        structured_llm = MagicMock()
        structured_llm.invoke.return_value = fake_decision
        with patch("tradingagents.agents.researchers.bull_researcher.create_bull_researcher", return_value=bull_node), \
             patch("tradingagents.agents.researchers.bear_researcher.create_bear_researcher", return_value=bear_node):
            graph.quick_thinking_llm = MagicMock()
            graph.quick_thinking_llm.with_structured_output.return_value = structured_llm
            _, decision = graph.propagate_stock("AAPL", price=185.0)
    assert isinstance(decision, StockDecision)
    assert decision.ticker == "AAPL"


@pytest.fixture(scope="module")
def trader_prompt() -> str:
    """Run propagate_stock against mocks once per module, return the prompt
    that was passed to structured_llm.invoke. Shared by the assertion tests
    below so we don't re-execute the full bull/bear pipeline per test."""
    graph = _mock_graph()
    fake_news = [{"title": "x", "text": "y", "published_date": "2026-05-10"}]
    fake_decision = StockDecision(
        ticker="AAPL", direction=StockDirection.HOLD,
        confidence=0.6, rationale="ok", price_at_analysis=185.0,
    )
    bull_update = {"investment_debate_state": {"bull_history": "B", "bear_history": "", "history": "", "current_response": "", "judge_decision": "", "count": 1}}
    bear_update = {"investment_debate_state": {"bull_history": "B", "bear_history": "BR", "history": "", "current_response": "", "judge_decision": "", "count": 2}}
    with patch("tradingagents.dataflows.polymarket_news.search_event_news", return_value=fake_news), \
         patch("yfinance.Ticker") as mock_yf:
        mock_yf.return_value.history.return_value = MagicMock(empty=True)
        bull_node = MagicMock(return_value=bull_update)
        bear_node = MagicMock(return_value=bear_update)
        structured_llm = MagicMock()
        structured_llm.invoke.return_value = fake_decision
        with patch("tradingagents.agents.researchers.bull_researcher.create_bull_researcher", return_value=bull_node), \
             patch("tradingagents.agents.researchers.bear_researcher.create_bear_researcher", return_value=bear_node):
            graph.quick_thinking_llm = MagicMock()
            graph.quick_thinking_llm.with_structured_output.return_value = structured_llm
            graph.propagate_stock("AAPL", price=185.0)
    return structured_llm.invoke.call_args[0][0]


def test_stock_trader_prompt_drops_confidence_anchor(trader_prompt: str):
    """Regression locks. PR #21 banned the numeric ladder ('0.48-0.52' etc).
    Today's PR also drops the three-condition (a)+(b)+(c) HOLD test that
    forced 100% HOLD across 10 days of production fires by requiring all
    three conditions to deviate from HOLD."""
    for banned in ("0.48-0.52", "0.53-0.59", "0.60-0.69", "0.70+"):
        assert banned not in trader_prompt, (
            f"prompt still contains anchor band {banned!r} — "
            f"sonnet will lock onto the middle of the lowest band"
        )
    # The (a)+(b)+(c) three-condition test forced 100% HOLD across 10 days
    # of production because almost nothing clears all three. Replaced by
    # per-direction asymmetry framing.
    assert "(a) a specific recent catalyst" not in trader_prompt
    assert "(c) clear pricing dislocation relative to the catalyst" not in trader_prompt


def test_stock_trader_prompt_has_asymmetric_setup_framing(trader_prompt: str):
    """The new framing forces the model to commit when it's reasoning
    circularly between 'real positives' and 'real risks'."""
    # Force commit on balanced cases (the previous prompt let model HOLD them)
    assert '"balanced" is a high bar' in trader_prompt
    assert "asymmetric" in trader_prompt.lower()
    # Pre-event setups are now explicitly allowed as LONG/SHORT, not just HOLD
    assert "Pre-event setups" in trader_prompt or "PRE-EVENT SETUPS" in trader_prompt
    # SHORT base-rate caution preserved but softened (stretched setups OK)
    assert "SHORT" in trader_prompt
    assert "stretched" in trader_prompt.lower()
    # Confidence still grounded in evidence, not bands
    assert "high evidence" in trader_prompt.lower()
