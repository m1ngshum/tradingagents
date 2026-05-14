"""Tests for the freshen_signals Kelly recompute and file scanning."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Import the script directly (it's not a package member).
_FRESHEN_PATH = Path(__file__).resolve().parent.parent / "scripts" / "freshen_signals.py"
_spec = importlib.util.spec_from_file_location("freshen_signals", _FRESHEN_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["freshen_signals"] = _mod
_spec.loader.exec_module(_mod)

_kelly_edge = _mod._kelly_edge
_iter_discovery_files = _mod._iter_discovery_files
_load_signals = _mod._load_signals


class TestKellyEdge:
    """Same edge formula as discover_polymarket; verified independently."""

    def test_buy_yes_positive_edge(self):
        # yes_price 0.30, confidence 0.70 → edge = (0.70 - 0.30) / 0.70 = 0.571
        assert _kelly_edge(0.30, "BUY_YES", 0.70) == pytest.approx(0.5714, abs=1e-3)

    def test_buy_no_positive_edge(self):
        # yes_price 0.80 → buy_price=0.20; conf 0.70 → edge=(0.70-0.20)/0.80=0.625
        assert _kelly_edge(0.80, "BUY_NO", 0.70) == pytest.approx(0.625, abs=1e-3)

    def test_hold_returns_zero(self):
        assert _kelly_edge(0.50, "HOLD", 0.80) == 0.0

    def test_negative_edge_clamped_to_zero(self):
        # confidence < buy_price → negative kelly → clamp to 0
        assert _kelly_edge(0.70, "BUY_YES", 0.50) == 0.0

    def test_extreme_price_returns_zero(self):
        assert _kelly_edge(0.999, "BUY_YES", 0.99) == 0.0
        assert _kelly_edge(0.001, "BUY_NO", 0.99) == 0.0


class TestIterDiscoveryFiles:
    def test_specific_date_returns_matching_file(self, tmp_path, monkeypatch):
        f = tmp_path / "discoveries-2026-05-13.jsonl"
        f.write_text('{"market_id":"x"}\n')
        monkeypatch.setattr(_mod, "POLYMARKET_OUTPUT_DIR", tmp_path)
        result = list(_iter_discovery_files("2026-05-13", max_age_hours=999))
        assert result == [f]

    def test_specific_date_no_match_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "POLYMARKET_OUTPUT_DIR", tmp_path)
        assert list(_iter_discovery_files("2026-05-13", max_age_hours=999)) == []

    def test_no_date_scans_recent(self, tmp_path, monkeypatch):
        import os, time
        f1 = tmp_path / "discoveries-2026-05-12.jsonl"
        f2 = tmp_path / "discoveries-2026-05-13.jsonl"
        f1.write_text("")
        f2.write_text("")
        # Make f1 very old (10 days ago)
        old = time.time() - 10 * 86400
        os.utime(f1, (old, old))
        monkeypatch.setattr(_mod, "POLYMARKET_OUTPUT_DIR", tmp_path)
        result = list(_iter_discovery_files(None, max_age_hours=24))
        assert result == [f2]


class TestLoadSignals:
    def test_skips_holds_and_zero_edge(self, tmp_path):
        f = tmp_path / "d.jsonl"
        import json
        lines = [
            {"market_id": "1", "direction": "HOLD", "confidence": 0.5, "kelly_edge": 0.0,
             "yes_price_at_analysis": 0.5, "question": "q1"},
            {"market_id": "2", "direction": "BUY_NO", "confidence": 0.7, "kelly_edge": 0.0,
             "yes_price_at_analysis": 0.5, "question": "q2"},
            {"market_id": "3", "direction": "BUY_YES", "confidence": 0.7, "kelly_edge": 0.3,
             "yes_price_at_analysis": 0.3, "question": "q3"},
        ]
        f.write_text("\n".join(json.dumps(l) for l in lines))
        signals = _load_signals([f])
        assert len(signals) == 1
        assert signals[0]["market_id"] == "3"

    def test_dedupes_across_files(self, tmp_path):
        import json
        f1 = tmp_path / "a.jsonl"
        f2 = tmp_path / "b.jsonl"
        sig = {"market_id": "42", "direction": "BUY_NO", "confidence": 0.7,
               "kelly_edge": 0.2, "yes_price_at_analysis": 0.8, "question": "q"}
        f1.write_text(json.dumps(sig) + "\n")
        f2.write_text(json.dumps(sig) + "\n")
        signals = _load_signals([f1, f2])
        assert len(signals) == 1

    def test_tolerates_malformed_lines(self, tmp_path):
        import json
        f = tmp_path / "d.jsonl"
        f.write_text(
            "not-json\n" +
            json.dumps({"market_id": "1", "direction": "BUY_YES", "confidence": 0.7,
                        "kelly_edge": 0.3, "yes_price_at_analysis": 0.3,
                        "question": "q"}) + "\n"
        )
        signals = _load_signals([f])
        assert len(signals) == 1
