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


# CLOB post_order response `status` values. A FOK (fill-or-kill) order either
# matches fully or is killed; "matched" is the only fill. Anything else means
# we do NOT hold the position, and the caller must not record exposure.
# Reference: py-clob-client OrderType.FOK responses.
_FILLED_STATUSES = frozenset({"matched"})
_UNFILLED_STATUSES = frozenset({"unmatched", "cancelled", "canceled", "killed", "delayed"})


def classify_order_response(resp: dict) -> dict:
    """Map a raw CLOB post_order response to a settlement verdict.

    Returns dict with:
        outcome: FILLED | UNFILLED | UNKNOWN
        order_status: the raw status string
        filled_usd: best-effort matched notional (0.0 if unfilled/unknown)

    FAIL-SAFE: an unrecognised or missing status is UNKNOWN, never FILLED.
    The caller treats UNKNOWN as "did not fill" for exposure accounting and
    flags it for human reconciliation — we never assume a position exists
    without explicit confirmation.
    """
    if not isinstance(resp, dict):
        return {"outcome": "UNKNOWN", "order_status": "no_response", "filled_usd": 0.0}

    # success=False short-circuits to UNFILLED regardless of status text.
    if resp.get("success") is False:
        return {
            "outcome": "UNFILLED",
            "order_status": str(resp.get("status", "failed")),
            "filled_usd": 0.0,
        }

    status = str(resp.get("status", "")).strip().lower()

    # Best-effort matched notional. CLOB returns making/taking amounts as
    # strings; for a BUY, the USDC we spent is the "making" side.
    filled_usd = 0.0
    for key in ("makingAmount", "making_amount", "matchedAmount"):
        if key in resp:
            try:
                filled_usd = float(resp[key])
                break
            except (TypeError, ValueError):
                pass

    if status in _FILLED_STATUSES:
        return {"outcome": "FILLED", "order_status": status, "filled_usd": filled_usd}
    if status in _UNFILLED_STATUSES:
        return {"outcome": "UNFILLED", "order_status": status, "filled_usd": 0.0}
    return {"outcome": "UNKNOWN", "order_status": status or "missing", "filled_usd": 0.0}


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
        POLYMARKET_PRIVATE_KEY   — signer private key (for order signing)
        POLYMARKET_KEY           — L2 API key
        POLYMARKET_SECRET        — L2 API secret
        POLYMARKET_PASSPHRASE    — L2 API passphrase
        POLYMARKET_FUNDER        — wallet holding USDC collateral (the funder)

    Account-type env var (CRITICAL — wrong value = orders sign against the
    wrong wallet and fail or misbehave):
        POLYMARKET_SIGNATURE_TYPE — 0 | 1 | 2. Default 0.
            0 = EOA: signer address IS the funder (browser wallet you control
                directly, key == funder address).
            1 = Magic/email (Polymarket proxy): you log in with email/Google;
                the signer EOA controls a SEPARATE proxy contract that holds
                the USDC. FUNDER MUST be that proxy/deposit address, NOT the
                signer address. This is the type for email-login accounts.
            2 = browser-wallet proxy (MetaMask-linked Polymarket proxy).

    For signature_type 1/2 the funder ≠ signer; set POLYMARKET_FUNDER to the
    Polygon proxy address that actually holds your collateral (the address
    Polymarket's Deposit-on-Polygon flow funds, NOT the Ethereum bridge
    address and NOT the "API use only" profile address).
    """

    _HOST = "https://clob.polymarket.com"
    _CHAIN_ID = 137  # Polygon
    _VALID_SIG_TYPES = (0, 1, 2)

    def __init__(self) -> None:
        if ClobClient is None:
            raise PolymarketExecutionDisabled("py-clob-client not installed")

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        api_key = os.environ.get("POLYMARKET_KEY")
        api_secret = os.environ.get("POLYMARKET_SECRET")
        api_passphrase = os.environ.get("POLYMARKET_PASSPHRASE")
        funder = os.environ.get("POLYMARKET_FUNDER")
        sig_type = self._resolve_signature_type()

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

        # Guard the silent-misconfig footgun: for proxy account types the funder
        # MUST differ from the signer's own address. If someone sets sig_type=1
        # but points funder at the signer EOA, orders sign against an empty
        # wallet. We can't always know the signer address cheaply, but if the
        # funder equals the key's address for a proxy type, that's certainly wrong.
        self._signer_address = self._derive_signer_address(private_key)
        if (
            sig_type in (1, 2)
            and self._signer_address is not None
            and funder.lower() == self._signer_address.lower()
        ):
            raise PolymarketExecutionDisabled(
                f"POLYMARKET_SIGNATURE_TYPE={sig_type} (proxy) but POLYMARKET_FUNDER "
                f"equals the signer address {funder}. For proxy accounts the funder "
                f"must be the SEPARATE proxy wallet that holds USDC, not the signer. "
                f"Set POLYMARKET_FUNDER to your Polygon proxy/deposit address."
            )

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
            signature_type=sig_type,
            funder=funder,
        )
        self._signature_type = sig_type
        self._funder = funder
        logger.info(
            "PolymarketExecutor ready (signature_type=%s funder=%s signer=%s)",
            sig_type, funder, self._signer_address,
        )

    @classmethod
    def _resolve_signature_type(cls) -> int:
        raw = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0").strip()
        try:
            v = int(raw)
        except ValueError:
            raise PolymarketExecutionDisabled(
                f"POLYMARKET_SIGNATURE_TYPE must be 0, 1, or 2; got {raw!r}"
            )
        if v not in cls._VALID_SIG_TYPES:
            raise PolymarketExecutionDisabled(
                f"POLYMARKET_SIGNATURE_TYPE must be 0, 1, or 2; got {v}"
            )
        return v

    @staticmethod
    def _derive_signer_address(private_key: str) -> str | None:
        """Best-effort derive the signer's address from its key (for the
        funder!=signer safety check). Returns None if eth_account isn't
        available — the check is then skipped, never blocks on its own absence."""
        try:
            from eth_account import Account
            return Account.from_key(private_key).address
        except Exception:  # noqa: BLE001 — derivation is advisory only
            return None

    def get_usdc_balance(self) -> float | None:
        """Best-effort live USDC collateral balance. None if it can't be read.

        Returns None (not 0) on any error so the reconciler treats it as
        'unavailable' and HALTS rather than reading a missing balance as zero
        and trading against phantom funds.
        """
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self._client.get_balance_allowance(params)
            raw = resp.get("balance") if isinstance(resp, dict) else None
            if raw is None:
                return None
            # CLOB returns balance in USDC base units (6 decimals).
            return float(raw) / 1_000_000.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_usdc_balance failed: %s", exc)
            return None

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

            order_id = resp.get("orderID", resp.get("id", "unknown")) if isinstance(resp, dict) else "unknown"
            verdict = classify_order_response(resp if isinstance(resp, dict) else {})

            # Reconciliation: only FILLED counts as a real position. UNFILLED is
            # a clean no-op (FOK killed). UNKNOWN means we could not confirm —
            # surface it loudly so a human reconciles rather than the bot
            # silently assuming a fill it may not have.
            status_map = {"FILLED": "FILLED", "UNFILLED": "UNFILLED", "UNKNOWN": "UNCONFIRMED"}
            result_status = status_map[verdict["outcome"]]
            # Exposure recorded only on confirmed fill; fall back to intended
            # size if the API didn't echo a matched amount.
            filled_usd = verdict["filled_usd"] or (sizing["usd"] if verdict["outcome"] == "FILLED" else 0.0)

            log_fn = logger.info if verdict["outcome"] == "FILLED" else logger.warning
            log_fn(
                "Polymarket order %s: market=%s dir=%s intended=$%.2f filled=$%.2f id=%s raw_status=%s",
                result_status, decision.market_id, decision.direction.value,
                sizing["usd"], filled_usd, order_id, verdict["order_status"],
            )
            return {
                "status": result_status,
                "order_id": order_id,
                "order_status": verdict["order_status"],
                "outcome": verdict["outcome"],
                "direction": decision.direction.value,
                "token_id": token_id,
                "usd": sizing["usd"],
                "filled_usd": filled_usd,
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
