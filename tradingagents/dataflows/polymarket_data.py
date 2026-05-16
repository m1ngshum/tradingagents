"""Polymarket Gamma REST API client.

Read-only data layer for Phase A: fetch open markets and individual market
metadata. No wallet, no auth, Gamma is a public REST endpoint.

Schema returned by Gamma `/markets` is messy (outcomePrices is a JSON-stringified
array, volume is a string). This module normalises each market into a flat dict
with proper types and a derived `yes_price` field. Markets with unparseable
outcomePrices are filtered out rather than crashing the caller.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 10.0


class GammaAPIError(Exception):
    """Raised when Gamma returns a non-2xx response or unparseable body."""


class CLOBAPIError(Exception):
    """Raised when CLOB returns a non-2xx response or unparseable body."""


def _is_transient_http_error(exc: BaseException) -> bool:
    """Return True if the exception is worth retrying.

    Retry on:
      - Network errors (timeout, connection refused, DNS) - httpx.RequestError
      - 429 (rate limit) and 5xx (server error) - httpx.HTTPStatusError
    Do NOT retry on 4xx client errors (e.g. 404 not-found, 400 bad request) -
    those will fail again with the same response no matter how often we retry.
    """
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_is_transient_http_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _http_get_with_retry(url: str, **kwargs: Any) -> httpx.Response:
    """GET with retry on transient failures (429, 5xx, network errors).

    Final exception is re-raised after retries are exhausted so the calling
    function can convert it into a domain-specific error (GammaAPIError or
    CLOBAPIError).
    """
    resp = httpx.get(url, **kwargs)
    resp.raise_for_status()
    return resp


def _normalise_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one Gamma market dict into a normalised shape.

    Returns None when the market has malformed outcomePrices. The caller
    filters these out so a single bad market never crashes the pipeline.
    """
    try:
        prices = json.loads(raw.get("outcomePrices", "[]"))
        if not isinstance(prices, list) or len(prices) < 2:
            return None
        yes_price = float(prices[0])
    except (json.JSONDecodeError, ValueError, TypeError):
        return None

    try:
        volume = float(raw.get("volume", "0"))
    except (ValueError, TypeError):
        volume = 0.0

    try:
        liquidity = float(raw.get("liquidity", "0"))
    except (ValueError, TypeError):
        liquidity = 0.0

    # Token IDs are required for /book queries. Markets that are missing or
    # have malformed token ids cannot be paper-filled or executed against,
    # so set them to None and let the caller decide.
    yes_token_id: str | None = None
    no_token_id: str | None = None
    try:
        token_ids = json.loads(raw.get("clobTokenIds", "[]"))
        if isinstance(token_ids, list) and len(token_ids) >= 2:
            yes_token_id = str(token_ids[0]) or None
            no_token_id = str(token_ids[1]) or None
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    return {
        "id": raw.get("id"),
        "question": raw.get("question"),
        "yes_price": yes_price,
        "volume": volume,
        "liquidity": liquidity,
        "end_date": raw.get("endDate"),
        "active": bool(raw.get("active", False)),
        "closed": bool(raw.get("closed", False)),
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        # Fields needed for downstream calibration + cluster cap. Preserved
        # raw (string form) so callers can re-parse with the same logic that
        # gamma writes. Defaults are conservative: empty list for statuses,
        # None for the negRisk grouping ID, raw slug for fallback grouping.
        "slug": raw.get("slug"),
        "negRiskRequestID": raw.get("negRiskRequestID") or None,
        "umaResolutionStatuses": raw.get("umaResolutionStatuses"),
    }


def get_open_markets(
    limit: int = 50,
    order: str | None = None,
    ascending: bool = True,
    end_date_max: str | None = None,
    end_date_min: str | None = None,
) -> list[dict[str, Any]]:
    """Return active, open Polymarket markets.

    Args:
        limit: max number of markets to return.
        order: optional gamma sort field. Pass "endDate" to get markets that
            close soonest first (with ascending=True). When None, gamma
            returns its default ordering (volume-ish).
        ascending: sort direction when `order` is set.
        end_date_max: optional ISO-8601 cutoff (e.g. "2026-06-01"). Filters
            out markets whose endDate is past the cutoff. Server-side filter.

    Markets with unparseable outcomePrices are filtered out (logged but not
    raised). Network errors raise GammaAPIError with the original message.
    """
    params: dict[str, str] = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
    }
    if order is not None:
        params["order"] = order
        params["ascending"] = "true" if ascending else "false"
    if end_date_max is not None:
        params["end_date_max"] = end_date_max
    if end_date_min is not None:
        params["end_date_min"] = end_date_min
    try:
        resp = _http_get_with_retry(
            f"{GAMMA_BASE}/markets", params=params, timeout=DEFAULT_TIMEOUT
        )
    except httpx.HTTPStatusError as e:
        raise GammaAPIError(f"Gamma /markets returned {e.response.status_code}: {e}") from e
    except httpx.RequestError as e:
        raise GammaAPIError(f"Gamma /markets request failed: {e}") from e

    raw = resp.json()
    if not isinstance(raw, list):
        raise GammaAPIError(f"Gamma /markets returned non-list: {type(raw).__name__}")

    normalised: list[dict[str, Any]] = []
    for item in raw:
        market = _normalise_market(item)
        if market is None:
            logger.warning("Skipped malformed Polymarket market: id=%s", item.get("id"))
            continue
        normalised.append(market)
    return normalised


def get_market_by_id(market_id: str) -> dict[str, Any]:
    """Fetch a single market by Polymarket ID.

    Raises GammaAPIError on network failure or unparseable response.
    """
    try:
        resp = _http_get_with_retry(
            f"{GAMMA_BASE}/markets/{market_id}", timeout=DEFAULT_TIMEOUT
        )
    except httpx.HTTPStatusError as e:
        raise GammaAPIError(
            f"Gamma /markets/{market_id} returned {e.response.status_code}"
        ) from e
    except httpx.RequestError as e:
        raise GammaAPIError(f"Gamma /markets/{market_id} request failed: {e}") from e

    raw = resp.json()
    market = _normalise_market(raw)
    if market is None:
        raise GammaAPIError(f"Market {market_id} has malformed outcomePrices")
    return market


def get_order_book(token_id: str) -> dict[str, Any]:
    """Fetch the CLOB order book for a Polymarket token (YES or NO side).

    Returns a normalised dict:
        {
          "asset_id": str,
          "market": str,        # the conditionId
          "bids": [{"price": float, "size": float}, ...],
          "asks": [{"price": float, "size": float}, ...],
          "timestamp": str,     # ms epoch as string
        }

    Polymarket's /book returns prices and sizes as strings; this function
    converts them to floats. Levels with unparseable values are skipped
    (logged) rather than crashing the caller. Raises CLOBAPIError on
    network failure.
    """
    try:
        resp = _http_get_with_retry(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPStatusError as e:
        raise CLOBAPIError(
            f"CLOB /book returned {e.response.status_code} for {token_id}"
        ) from e
    except httpx.RequestError as e:
        raise CLOBAPIError(f"CLOB /book request failed: {e}") from e

    raw = resp.json()
    if not isinstance(raw, dict):
        raise CLOBAPIError(f"CLOB /book returned non-dict: {type(raw).__name__}")

    def _to_levels(items: Any) -> list[dict[str, float]]:
        out: list[dict[str, float]] = []
        if not isinstance(items, list):
            return out
        for x in items:
            try:
                price = float(x["price"])
                size = float(x["size"])
            except (KeyError, ValueError, TypeError):
                logger.warning("Skipping malformed CLOB level: %s", x)
                continue
            out.append({"price": price, "size": size})
        return out

    return {
        "asset_id": raw.get("asset_id"),
        "market": raw.get("market"),
        "bids": _to_levels(raw.get("bids")),
        "asks": _to_levels(raw.get("asks")),
        "timestamp": raw.get("timestamp"),
    }


# ---------------------------------------------------------------------------
# Cluster resolution — groups correlated markets (e.g. "Will Trump say X
# during Xi event?" sibling markets) so the routine can cap exposure per
# event. Resolves three ways:
#   1. `negRiskRequestID` from the market (Polymarket's negRisk sibling ID)
#   2. `/events?slug=<base-slug>` parent event lookup
#   3. synthetic `unknown:<base-slug>` so sibling markets with the same base
#      slug still share a cluster (fail-safe, not fail-open)
# ---------------------------------------------------------------------------


def _base_slug(slug: str) -> str | None:
    """Strip the trailing keyword from a market slug.

    Used as the cluster key when negRisk + events lookups both miss. Example:
        will-trump-say-ai-during-events-with-xi-jinping
      → will-trump-say-during-events-with-xi-jinping

    The heuristic: drop the segment that follows the verb. For "will-X-say-Y-..."
    slugs, drop "y". For other patterns, return None (caller refuses the BUY).
    """
    if not slug:
        return None
    parts = slug.split("-")
    if "say" in parts:
        i = parts.index("say")
        # Drop the keyword(s) immediately after "say" up to the next stopword.
        stopwords = {"during", "by", "before", "after", "on", "at", "in", "to"}
        # Find first stopword after "say"
        j = i + 1
        while j < len(parts) and parts[j] not in stopwords:
            j += 1
        if j > i + 1:
            return "-".join(parts[:i+1] + parts[j:])
    return None


def resolve_cluster_id(
    market: dict[str, Any],
    *,
    allow_events_lookup: bool = True,
) -> str | None:
    """Resolve a market's cluster ID for per-event exposure caps.

    Resolution order:
      1. `negRiskRequestID` on the market itself — Polymarket's native sibling
         ID. Cheapest and most authoritative.
      2. `/events?slug=<base-slug>` — find the parent event whose markets list
         includes this one. Costs one extra HTTP call per unique slug-base.
         Skipped when `allow_events_lookup=False` (test path or cost-sensitive
         caller).
      3. Synthetic `unknown:<base-slug>` — sibling markets with the same base
         slug share a cluster even when Gamma exposes no link. Fail-safe.

    Returns the cluster ID string, or None if no fallback can be derived
    (slug is unrecognisable) — the caller should refuse the BUY in that case.
    """
    neg_risk_id = market.get("negRiskRequestID")
    if neg_risk_id:
        return f"negRisk:{neg_risk_id}"

    slug = market.get("slug") or ""

    if allow_events_lookup and slug:
        try:
            resp = _http_get_with_retry(
                f"{GAMMA_BASE}/events",
                timeout=DEFAULT_TIMEOUT,
                params={"slug": slug, "limit": 1},
            )
            events = resp.json()
            if isinstance(events, list) and events:
                event_id = events[0].get("id")
                if event_id:
                    return f"event:{event_id}"
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.warning(
                "events lookup failed for slug %s: %s — falling back to synthetic",
                slug, e,
            )

    base = _base_slug(slug)
    if base:
        return f"unknown:{base}"

    # Cannot derive any grouping — caller should refuse the BUY.
    return None
