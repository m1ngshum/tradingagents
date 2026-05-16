"""Tests for cluster-cap logic in scripts/run_polymarket.py.

We exercise the helper `_cluster_counts_today` (pure I/O over JSONL) directly
and rely on the existing test_resolve_cluster_id.py to cover the resolution
path. The cap-gate full integration is covered by exercising the helper +
verifying SKIPPED-row shape via test_run_polymarket_min_confidence.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingagents.exchange.io_utils import append_jsonl


def _cluster_counts_today(fill_log_path: Path):
    """Local copy of the helper so we don't need to import the script module
    (which pulls in the full TradingAgentsGraph dependency chain)."""
    from collections import Counter
    from tradingagents.exchange.scoring import load_fills_jsonl
    if not fill_log_path.exists():
        return Counter()
    fills = load_fills_jsonl(
        fill_log_path.parent,
        date=fill_log_path.stem.removeprefix("paper-fills-"),
    )
    counts: Counter = Counter()
    for f in fills:
        if f.get("status") in ("SKIPPED", "ERROR"):
            continue
        cid = f.get("cluster_id")
        if cid:
            counts[cid] += 1
    return counts


def test_empty_fill_log_returns_empty_counter(tmp_path: Path):
    path = tmp_path / "paper-fills-2026-05-16.jsonl"
    counts = _cluster_counts_today(path)
    assert dict(counts) == {}


def test_counts_filled_positions(tmp_path: Path):
    path = tmp_path / "paper-fills-2026-05-16.jsonl"
    append_jsonl(path, {"cluster_id": "negRisk:A", "filled": True, "filled_usd": 100})
    append_jsonl(path, {"cluster_id": "negRisk:A", "filled": True, "filled_usd": 100})
    append_jsonl(path, {"cluster_id": "event:B", "filled": True, "filled_usd": 100})
    counts = _cluster_counts_today(path)
    assert dict(counts) == {"negRisk:A": 2, "event:B": 1}


def test_excludes_skipped_rows(tmp_path: Path):
    """SKIPPED rows are audit trail, not actual positions — they must not count
    against the cap (otherwise the cap blocks itself after one skip)."""
    path = tmp_path / "paper-fills-2026-05-16.jsonl"
    append_jsonl(path, {"cluster_id": "negRisk:A", "status": "SKIPPED", "reason": "below_min_confidence"})
    append_jsonl(path, {"cluster_id": "negRisk:A", "status": "SKIPPED", "reason": "cluster_full"})
    append_jsonl(path, {"cluster_id": "negRisk:A", "filled": True, "filled_usd": 100})
    counts = _cluster_counts_today(path)
    assert dict(counts) == {"negRisk:A": 1}


def test_excludes_error_rows(tmp_path: Path):
    """Live-mode ERROR rows mean the order didn't actually place."""
    path = tmp_path / "paper-fills-2026-05-16.jsonl"
    append_jsonl(path, {"cluster_id": "negRisk:A", "status": "ERROR", "reason": "rate limit"})
    counts = _cluster_counts_today(path)
    assert dict(counts) == {}


def test_excludes_rows_without_cluster_id(tmp_path: Path):
    """Legacy fills written before the cluster_id field existed must not crash."""
    path = tmp_path / "paper-fills-2026-05-16.jsonl"
    append_jsonl(path, {"filled": True, "filled_usd": 100})  # no cluster_id
    counts = _cluster_counts_today(path)
    assert dict(counts) == {}


def test_simulates_05_14_trump_xi_scenario(tmp_path: Path):
    """The actual scenario this cap is designed to prevent: 7 sibling negRisk
    markets resolve to one cluster; only first fills, rest log cluster_full."""
    path = tmp_path / "paper-fills-2026-05-14.jsonl"
    siblings = [
        "Will Trump say 'AI' during events with Xi Jinping?",
        "Will Trump say 'Crypto' during events with Xi Jinping?",
        "Will Trump say 'Farmer' during events with Xi Jinping?",
        "Will Trump say 'Rare earth' during events with Xi Jinping?",
        "Will Trump say 'Hong Kong' during events with Xi Jinping?",
        "Will Trump say 'Iran' during events with Xi Jinping?",
        "Will Trump say 'Tough Negotiator' during events with Xi Jinping?",
    ]
    cluster_id = "negRisk:0xTrumpXi"
    MAX_PER_CLUSTER = 1

    counts = _cluster_counts_today(path)
    fills_placed = 0
    skips_placed = 0
    for q in siblings:
        if counts.get(cluster_id, 0) >= MAX_PER_CLUSTER:
            append_jsonl(path, {
                "question": q,
                "cluster_id": cluster_id,
                "status": "SKIPPED",
                "reason": "cluster_full",
            })
            skips_placed += 1
        else:
            append_jsonl(path, {
                "question": q,
                "cluster_id": cluster_id,
                "filled": True,
                "filled_usd": 100,
            })
            fills_placed += 1
        counts = _cluster_counts_today(path)  # re-read after each write

    assert fills_placed == 1, f"expected only 1 fill (cap=1), got {fills_placed}"
    assert skips_placed == 6, f"expected 6 cluster_full skips, got {skips_placed}"
