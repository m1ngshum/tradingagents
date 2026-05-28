"""Polymarket live-trading executor for Phase B.

Uses py-clob-client L2 auth (api_key / api_secret / api_passphrase) derived
from the proxy wallet private key.  The private key is only loaded at runtime
from POLYMARKET_PRIVATE_KEY — it is never committed to the repo.

Safety gate: all four env vars must be present or PolymarketExecutionDisabled
is raised.  No orders are submitted unless confidence >= _MIN_CONFIDENCE and
the trade has positive Kelly edge.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tradingagents.agents.schemas import PolymarketDecision, PolymarketDirection

logger = logging.getLogger(__name__)

_MIN_CONFIDENCE = 0.85      # matches script-level --min-confidence default; defense in depth
_KELLY_MULTIPLIER = 0.50
_MAX_FRACTION = 0.20        # higher cap than stocks — max loss is capped at $1/share
_MIN_PRICE = 0.02           # skip markets priced below 2c (illiquid)
_MAX_PRICE = 0.97           # skip markets priced above 97c (near-zero upside after fees)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
except ImportError:
    ClobClient = None  # type: ignore[assignment,misc]


class PolymarketExecutionDisabled(Exception):
    """Raised when Polymarket credentials are not configured."""


def size_polymarket_order(
    decision: PolymarketDecision,
    capital_usd: float,
    *,
    kelly_multiplier: float = _KELLY_MULTIPLIER,
    max_fraction: float = _MAX_FRACTION,
    min_confidence: float = _MIN_CONFIDENCE,
    min_price: float = _MIN_PRICE,
    max_price: float = _MAX_PRICE,
) -> dict[str, Any]:
    """Compute USD size using half-Kelly for a prediction market bet.

    For a binary market:
        buy_price  = yes_price        (BUY_YES) or (1 - yes_price) (BUY_NO)
        edge       = confidence - buy_price
        kelly      = edge / (1 - buy_price)

    Returns dict with keys: usd, fraction, reason.
    """
    if capital_usd <= 0:
        raise ValueError(f"capital_usd must be positive, got {capital_usd}")

    if decision.direction == PolymarketDirection.HOLD:
        return {"fraction": 0.0, "usd": 0.0, "reason": "HOLD"}

    if decision.confidence < min_confidence:
        return {
            "fraction": 0.0,
            "usd": 0.0,
            "reason": f"confidence {decision.confidence:.2f} < {min_confidence}",
        }

    yes_price = decision.yes_price_at_analysis
    if decision.direction == PolymarketDirection.BUY_YES:
        buy_price = yes_price
    else:
        buy_price = 1.0 - yes_price

    if buy_price < min_price:
        return {"fraction": 0.0, "usd": 0.0, "reason": f"price {buy_price:.3f} < min {min_price}"}
    if buy_price > max_price:
        return {"fraction": 0.0, "usd": 0.0, "reason": f"price {buy_price:.3f} > max {max_price}"}

    edge = decision.confidence - buy_price
    if edge <= 0:
        return {"fraction": 0.0, "usd": 0.0, "reason": f"no edge ({edge:.4f})"}

    raw_kelly = edge / (1.0 - buy_price)
    if raw_kelly <= 0:
        return {"fraction": 0.0, "usd": 0.0, "reason": f"negative Kelly ({raw_kelly:.4f})"}

    fraction = min(raw_kelly * kelly_multiplier, max_fraction)
    usd = round(capital_usd * fraction, 2)
    return {
        "fraction": round(fraction, 4),
        "usd": usd,
        "reason": f"half-Kelly edge={edge:.4f} raw={raw_kelly:.4f} → {fraction:.4f} of capital",
    }


class PolymarketExecutor:
    """Submit live orders to Polymarket CLOB for PolymarketDecisions.

    Requires env vars:
        POLYMARKET_PRIVATE_KEY   — proxy wallet private key (for order signing)
        POLYMARKET_KEY           — L2 API key
        POLYMARKET_SECRET        — L2 API secret
        POLYMARKET_PASSPHRASE    — L2 API passphrase
        POLYMARKET_FUNDER        — proxy wallet address
    """

    _HOST = "https://clob.polymarket.com"
    _CHAIN_ID = 137  # Polygon

    def __init__(self) -> None:
        if ClobClient is None:
            raise PolymarketExecutionDisabled("py-clob-client not installed")

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        api_key = os.environ.get("POLYMARKET_KEY")
        api_secret = os.environ.get("POLYMARKET_SECRET")
        api_passphrase = os.environ.get("POLYMARKET_PASSPHRASE")
        funder = os.environ.get("POLYMARKET_FUNDER")

        missing = [
            k for k, v in {
                "POLYMARKET_PRIVATE_KEY": private_key,
                "POLYMARKET_KEY": api_key,
                "POLYMARKET_SECRET": api_secret,
                "POLYMARKET_PASSPHRASE": api_passphrase,
                "POLYMARKET_FUNDER": funder,
            }.items() if not v
        ]
        if missing:
            raise PolymarketExecutionDisabled(f"Missing env vars: {', '.join(missing)}")

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        self._client = ClobClient(
            self._HOST,
            key=private_key,
            chain_id=self._CHAIN_ID,
            creds=creds,
            signature_type=0,   # EOA (Type 0) — key address == funder address
            funder=funder,
        )
        logger.info("PolymarketExecutor ready (funder=%s)", funder)

    def place_order(
        self,
        decision: PolymarketDecision,
        token_id: str,
        capital_usd: float,
    ) -> dict[str, Any]:
        """Size and submit a market order for the given PolymarketDecision.

        Args:
            decision:    The trader's decision including direction and confidence.
            token_id:    The CLOB token ID for the side being bought
                         (yes_token_id for BUY_YES, no_token_id for BUY_NO).
            capital_usd: Total capital to size against.

        Returns dict with status: SUBMITTED | SKIPPED | ERROR.
        """
        sizing = size_polymarket_order(decision, capital_usd)

        if sizing["usd"] <= 0:
            return {
                "status": "SKIPPED",
                "reason": sizing["reason"],
                "sizing": sizing,
                "market_id": decision.market_id,
                "question": decision.question,
            }

        yes_price = decision.yes_price_at_analysis
        buy_price = yes_price if decision.direction == PolymarketDirection.BUY_YES else (1.0 - yes_price)

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=sizing["usd"],
                side=BUY,
                price=buy_price,
            )
            signed_order = self._client.create_market_order(order_args)
            resp = self._client.post_order(signed_order, OrderType.FOK)

            order_id = resp.get("orderID", resp.get("id", "unknown"))
            status_raw = resp.get("status", "unknown")
            logger.info(
                "Polymarket order submitted: market=%s direction=%s usd=%.2f id=%s",
                decision.market_id, decision.direction.value, sizing["usd"], order_id,
            )
            return {
                "status": "SUBMITTED",
                "order_id": order_id,
                "order_status": status_raw,
                "direction": decision.direction.value,
                "token_id": token_id,
                "usd": sizing["usd"],
                "buy_price": buy_price,
                "sizing": sizing,
                "market_id": decision.market_id,
                "question": decision.question,
            }
        except Exception as exc:
            logger.error(
                "Polymarket order failed: market=%s error=%s", decision.market_id, exc
            )
            return {
                "status": "ERROR",
                "reason": str(exc),
                "market_id": decision.market_id,
                "sizing": sizing,
            }
