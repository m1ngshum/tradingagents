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


def test_empty_decisions_exits_1(tmp_path: Path):
    """No directional decisions → clean 'no data' exit, not a crash.

    Asserts the exact output string the script emits — caught by review:
    the prior test checked 'No placed fills' which never matches.
    """
    (tmp_path / "polymarket").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "No directional decisions" in combined, (
        f"expected 'No directional decisions' in output, got:\n{combined}"
    )


def test_all_hold_decisions_treated_as_empty(tmp_path: Path):
    """A day where every decision was HOLD (no directional bet) should not
    look like real trades — calibrate filters to BUY_YES/BUY_NO only.

    Writes decisions-*.jsonl (the file calibrate actually reads), not fills.
    """
    pdir = tmp_path / "polymarket"
    pdir.mkdir()
    decisions_path = pdir / "decisions-2026-05-16.jsonl"
    for i in range(5):
        append_jsonl(decisions_path, {
            "market_id": f"mid_{i}",
            "direction": "HOLD",
            "confidence": 0.5,
        })
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, (
        f"expected 'no directional decisions' exit, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_output_path_outside_sandbox_rejected(tmp_path: Path):
    """--output must be within home or /tmp. Reject other paths to prevent
    accidental writes to system locations."""
    (tmp_path / "polymarket").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--dir", str(tmp_path),
         "--output", "/etc/calibrate_report.md"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "--output must be within" in result.stderr


def test_output_path_in_tmp_accepted(tmp_path: Path):
    """/tmp is an allowed root."""
    (tmp_path / "polymarket").mkdir()
    out_file = Path("/tmp") / f"calibrate_test_{tmp_path.name}.md"
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--dir", str(tmp_path),
             "--output", str(out_file)],
            capture_output=True, text=True, timeout=30,
        )
        # Will exit 1 (no decisions) but NOT 2 (sandbox reject)
        assert result.returncode == 1
        assert "--output must be within" not in result.stderr
    finally:
        if out_file.exists():
            out_file.unlink()
