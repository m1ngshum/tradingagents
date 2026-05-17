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


# ---------------------------------------------------------------------------
# Model-name normalization (groups dash-vs-dot version variants)
# ---------------------------------------------------------------------------


def _load_normalize_fn():
    """Side-load calibrate.py (it's a script, not a package) once at import."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("calibrate_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._normalize_model


# Hoisted to module level so three normalization tests share one exec_module
# instead of re-parsing calibrate.py per test.
_normalize_model = _load_normalize_fn()


def test_normalize_model_collapses_dash_between_digits():
    """The 2026-05-17 calibration report split 'sonnet-4.6' (n=16, 25% win)
    from 'sonnet-4-6' (n=3, 100% win) as two models — same model, dash
    vs dot in version segment. Normalization must collapse them."""
    assert _normalize_model("anthropic/claude-sonnet-4-6") == "anthropic/claude-sonnet-4.6"
    assert _normalize_model("anthropic/claude-sonnet-4.6") == "anthropic/claude-sonnet-4.6"
    assert _normalize_model("anthropic/claude-opus-4-7") == "anthropic/claude-opus-4.7"


def test_normalize_model_collapses_multi_segment_versions():
    """Real Anthropic model `claude-3-5-sonnet` should normalize to
    `claude-3.5-sonnet` (same model on OpenRouter)."""
    assert _normalize_model("anthropic/claude-3-5-sonnet") == "anthropic/claude-3.5-sonnet"
    assert _normalize_model("anthropic/claude-3-5-haiku") == "anthropic/claude-3.5-haiku"


def test_normalize_model_preserves_non_version_dashes():
    """Dashes that aren't between digits (provider prefix, model family
    names like 'gpt-4o') must be preserved."""
    assert _normalize_model("openai/gpt-4o") == "openai/gpt-4o"
    assert _normalize_model("openai/gpt-4o-mini") == "openai/gpt-4o-mini"
    assert _normalize_model("anthropic/claude-haiku-4.5") == "anthropic/claude-haiku-4.5"


def test_normalize_model_handles_unknown_and_empty():
    """Defensive: unknown/empty values from missing model fields."""
    assert _normalize_model("unknown") == "unknown"
    assert _normalize_model("") == ""
