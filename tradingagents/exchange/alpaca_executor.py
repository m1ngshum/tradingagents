"""Alpaca paper-trading executor for Phase C stock decisions.

Safety-gated identically to live_executor.py: requires explicit env vars
and raises AlpacaExecutionDisabled if keys are absent.

Paper mode (ALPACA_PAPER=true) is the default and never risks real capital.
Real-money execution requires ALPACA_PAPER=false AND both key env vars set.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tradingagents.agents.schemas import StockDecision, StockDirection

logger = logging.getLogger(__name__)

_MIN_CONFIDENCE = 0.55
_KELLY_MULTIPLIER = 0.50
_MAX_FRACTION = 0.10

try:
    from alpaca.trading.client import TradingClient
except ImportError:
    TradingClient = None  # type: ignore[assignment,misc]


class AlpacaExecutionDisabled(Exception):
    """Raised when Alpaca keys are not configured."""


def size_stock_order(
    decision: StockDecision,
    capital_usd: float,
    *,
    kelly_multiplier: float = _KELLY_MULTIPLIER,
    max_fraction: float = _MAX_FRACTION,
    min_confidence: float = _MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Compute notional USD size using half-Kelly for a stock direction bet.

    Treats a 5-day equity trade as a coin-flip with p=confidence and b=1 (1:1 payoff).
    Conservative; real Kelly would use expected return / volatility.

    Returns dict with keys: fraction, usd, reason.
    """
    if capital_usd <= 0:
        raise ValueError(f"capital_usd must be positive, got {capital_usd}")
    if decision.direction == StockDirection.HOLD:
        return {"fraction": 0.0, "usd": 0.0, "reason": "HOLD"}
    if decision.confidence < min_confidence:
        return {"fraction": 0.0, "usd": 0.0, "reason": f"confidence {decision.confidence:.2f} < {min_confidence}"}

    p = decision.confidence
    q = 1.0 - p
    b = 1.0
    raw_kelly = (b * p - q) / b
    if raw_kelly <= 0:
        return {"fraction": 0.0, "usd": 0.0, "reason": f"negative Kelly ({raw_kelly:.4f})"}

    fraction = min(raw_kelly * kelly_multiplier, max_fraction)
    usd = round(capital_usd * fraction, 2)
    return {
        "fraction": round(fraction, 4),
        "usd": usd,
        "reason": f"half-Kelly {raw_kelly:.4f} → {fraction:.4f} of capital",
    }


class AlpacaExecutor:
    """Submit paper (or real) orders to Alpaca for stock decisions.

    Instantiation requires ALPACA_API_KEY and ALPACA_SECRET_KEY env vars.
    ALPACA_PAPER=true (default) routes to paper-api.alpaca.markets.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key:
            raise AlpacaExecutionDisabled("ALPACA_API_KEY env var not set")
        if not secret_key:
            raise AlpacaExecutionDisabled("ALPACA_SECRET_KEY env var not set")

        paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
        if TradingClient is None:
            raise AlpacaExecutionDisabled("alpaca-py not installed (pip install alpaca-py)")

        self._client = TradingClient(api_key, secret_key, paper=paper)
        self._paper = paper
        logger.info("AlpacaExecutor ready (paper=%s)", paper)

    def place_order(
        self,
        decision: StockDecision,
        capital_usd: float,
    ) -> dict[str, Any]:
        """Size and submit a market order for the given StockDecision.

        Returns a dict with status: SUBMITTED | SKIPPED | ERROR.
        """
        sizing = size_stock_order(decision, capital_usd)

        if sizing["usd"] <= 0:
            return {
                "status": "SKIPPED",
                "reason": sizing["reason"],
                "sizing": sizing,
                "ticker": decision.ticker,
            }

        side_str = "buy" if decision.direction == StockDirection.LONG else "sell"

        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            side = OrderSide.BUY if decision.direction == StockDirection.LONG else OrderSide.SELL
            req = MarketOrderRequest(
                symbol=decision.ticker,
                notional=sizing["usd"],
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            logger.info(
                "Alpaca %s order submitted: ticker=%s notional=$%.2f id=%s",
                side_str, decision.ticker, sizing["usd"], order.id,
            )
            return {
                "status": "SUBMITTED",
                "order_id": str(order.id),
                "order_status": str(order.status),
                "side": side_str,
                "notional_usd": sizing["usd"],
                "sizing": sizing,
                "ticker": decision.ticker,
                "paper": self._paper,
            }
        except Exception as exc:
            logger.error("Alpaca order failed: ticker=%s error=%s", decision.ticker, exc)
            return {
                "status": "ERROR",
                "reason": str(exc),
                "ticker": decision.ticker,
                "sizing": sizing,
            }
