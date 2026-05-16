"""Tests for resolve_cluster_id in polymarket_data.py.

Three-tier resolution: negRiskRequestID → /events lookup → synthetic
unknown:base_slug fallback. The synthetic fallback is the fail-safe — sibling
markets that share a base slug share a cluster even when Gamma exposes no link.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from tradingagents.dataflows.polymarket_data import (
    _base_slug,
    resolve_cluster_id,
)


class TestBaseSlug:
    @pytest.mark.parametrize("slug,expected", [
        # Trump-Xi family — drop the keyword between "say" and "during"
        ("will-trump-say-ai-during-events-with-xi-jinping",
         "will-trump-say-during-events-with-xi-jinping"),
        ("will-trump-say-crypto-or-bitcoin-during-events-with-xi-jinping",
         "will-trump-say-during-events-with-xi-jinping"),
        ("will-trump-say-rare-earth-during-events-with-xi-jinping",
         "will-trump-say-during-events-with-xi-jinping"),
        ("will-trump-say-tough-negotiator-during-events-with-xi-jinping",
         "will-trump-say-during-events-with-xi-jinping"),
        # "by" stopword
        ("will-biden-say-inflation-by-march", "will-biden-say-by-march"),
    ])
    def test_strips_keyword_from_say_markets(self, slug, expected):
        assert _base_slug(slug) == expected

    @pytest.mark.parametrize("slug", [
        "",
        None,
        "lynx-vs-wings-final",  # not a "say" market — no derivable base
        "spacex-starship-flight-12-launch-by-may-15",  # no "say"
    ])
    def test_returns_none_for_non_say_slugs(self, slug):
        assert _base_slug(slug) is None


class TestResolveClusterId:
    def test_returns_neg_risk_first(self):
        m = {"negRiskRequestID": "0xabc123", "slug": "anything"}
        assert resolve_cluster_id(m) == "negRisk:0xabc123"

    def test_empty_neg_risk_falls_through(self):
        # Empty string should be treated as missing
        m = {
            "negRiskRequestID": "",
            "slug": "will-trump-say-ai-during-events-with-xi-jinping",
        }
        # Will hit the events lookup; mock it to fail then fall back to synthetic
        with patch(
            "tradingagents.dataflows.polymarket_data._http_get_with_retry",
            side_effect=httpx.RequestError("network down"),
        ):
            cid = resolve_cluster_id(m)
        assert cid == "unknown:will-trump-say-during-events-with-xi-jinping"

    def test_events_lookup_returns_event_id(self):
        m = {"negRiskRequestID": None, "slug": "will-trump-say-ai-during-events-with-xi-jinping"}
        fake = MagicMock()
        fake.json.return_value = [{"id": "12345", "title": "Trump-Xi summit"}]
        with patch(
            "tradingagents.dataflows.polymarket_data._http_get_with_retry",
            return_value=fake,
        ):
            cid = resolve_cluster_id(m)
        assert cid == "event:12345"

    def test_events_lookup_empty_falls_back_to_synthetic(self):
        m = {"slug": "will-trump-say-ai-during-events-with-xi-jinping"}
        fake = MagicMock()
        fake.json.return_value = []
        with patch(
            "tradingagents.dataflows.polymarket_data._http_get_with_retry",
            return_value=fake,
        ):
            cid = resolve_cluster_id(m)
        assert cid == "unknown:will-trump-say-during-events-with-xi-jinping"

    def test_events_lookup_http_error_falls_back_to_synthetic(self):
        m = {"slug": "will-trump-say-ai-during-events-with-xi-jinping"}
        with patch(
            "tradingagents.dataflows.polymarket_data._http_get_with_retry",
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500)),
        ):
            cid = resolve_cluster_id(m)
        assert cid == "unknown:will-trump-say-during-events-with-xi-jinping"

    def test_both_paths_fail_unrecognisable_slug_returns_none(self):
        """The fail-safe: when nothing groups the market, refuse to BUY."""
        # No "say" verb → _base_slug returns None.
        m = {"slug": "lynx-vs-wings-game-1"}
        with patch(
            "tradingagents.dataflows.polymarket_data._http_get_with_retry",
            side_effect=httpx.RequestError("network down"),
        ):
            cid = resolve_cluster_id(m)
        assert cid is None

    def test_allow_events_lookup_false_skips_http(self):
        """For test paths and cost-sensitive callers, skip the network entirely."""
        m = {"slug": "will-trump-say-ai-during-events-with-xi-jinping"}
        with patch(
            "tradingagents.dataflows.polymarket_data._http_get_with_retry",
        ) as mock_http:
            cid = resolve_cluster_id(m, allow_events_lookup=False)
        mock_http.assert_not_called()
        assert cid == "unknown:will-trump-say-during-events-with-xi-jinping"

    def test_sibling_markets_share_cluster_id(self):
        """The whole point: 7 Trump-Xi sibling markets must resolve to ONE cluster."""
        siblings = [
            "will-trump-say-ai-during-events-with-xi-jinping",
            "will-trump-say-crypto-or-bitcoin-during-events-with-xi-jinping",
            "will-trump-say-farmer-during-events-with-xi-jinping",
            "will-trump-say-rare-earth-during-events-with-xi-jinping",
            "will-trump-say-hong-kong-during-events-with-xi-jinping",
            "will-trump-say-iran-during-events-with-xi-jinping",
            "will-trump-say-tough-negotiator-during-events-with-xi-jinping",
        ]
        cluster_ids = {
            resolve_cluster_id({"slug": s}, allow_events_lookup=False)
            for s in siblings
        }
        assert len(cluster_ids) == 1, f"siblings should share one cluster, got {cluster_ids}"
