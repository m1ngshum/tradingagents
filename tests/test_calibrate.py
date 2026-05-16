"""Tests for scripts/calibrate.py.

The full pipeline (state-repo + Gamma) is exercised manually; this file
covers the edge cases that should never crash: empty input, missing
optional fields, zero resolved markets.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tradingagents.exchange.io_utils import append_jsonl


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "calibrate.py"


def test_help_runs():
    """--help works without loading any state."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "Calibration analysis" in result.stdout
    assert "--dir" in result.stdout
    assert "--min-resolved-for-recommendation" in result.stdout


def test_missing_dir_exits_2(tmp_path: Path):
    """Pointing at a non-existent state repo errors cleanly."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(tmp_path / "does-not-exist")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "does not exist" in result.stderr


def test_empty_fills_exits_1(tmp_path: Path):
    """No placed fills produces a clean 'no data' exit, not a crash."""
    (tmp_path / "polymarket").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert "No placed fills" in result.stdout or "No placed fills" in result.stderr


def test_all_skipped_treated_as_empty(tmp_path: Path):
    """A day where every decision was SKIPPED (gates fired) should not look
    like real fills — calibrate must filter those out."""
    pdir = tmp_path / "polymarket"
    pdir.mkdir()
    fills_path = pdir / "paper-fills-2026-05-16.jsonl"
    for i in range(5):
        append_jsonl(fills_path, {
            "market_id": f"mid_{i}",
            "status": "SKIPPED",
            "reason": "below_min_confidence",
        })
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, f"expected 'no fills' exit, got {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
