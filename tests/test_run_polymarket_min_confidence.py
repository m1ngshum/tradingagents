"""Tests for the --min-confidence gate in scripts/run_polymarket.py.

We avoid importing run_polymarket directly (it pulls in TradingAgentsGraph
+ Exa + a heavy chain of dependencies). Validation paths are testable via
subprocess; the gate-skip JSONL-write behavior is exercised by simulating
the helper directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "run_polymarket.py"


def _run_script(args: list[str]) -> subprocess.CompletedProcess:
    """Run the script with controlled env so it fails fast on missing keys.

    The script calls load_dotenv(PROJECT_ROOT/.env) at import time. To prevent
    that from loading real keys (which would let the script run the full LLM
    pipeline and time out), explicitly set the relevant vars to empty strings
    — python-dotenv does not override existing env vars by default.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "EXA_API_KEY": "",
        "OPENROUTER_API_KEY": "",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=30,
    )


@pytest.mark.parametrize("bad_value", ["nan", "-0.5", "1.5", "inf", "-inf"])
def test_min_confidence_rejects_out_of_range(bad_value: str):
    """Range validation: nan / inf / outside [0,1] must exit with non-zero."""
    result = _run_script(["--min-confidence", bad_value, "--limit", "1"])
    # argparse.error() exits with code 2 and writes to stderr
    assert result.returncode == 2, f"expected exit 2 for {bad_value!r}, got {result.returncode}\nstderr={result.stderr}"
    assert "--min-confidence" in result.stderr, f"expected error mention min-confidence: {result.stderr}"


def test_min_confidence_accepts_zero():
    """0.0 must be allowed (explicit opt-out of the gate)."""
    result = _run_script(["--min-confidence", "0.0", "--limit", "1"])
    # argparse passes; script will then fail on missing EXA_API_KEY (code 2)
    # but the error mentions EXA, not --min-confidence
    assert "--min-confidence" not in result.stderr or "must be a finite" not in result.stderr


def test_min_confidence_accepts_one():
    """1.0 must be allowed (block-all is a valid choice for paranoid mode)."""
    result = _run_script(["--min-confidence", "1.0", "--limit", "1"])
    assert "must be a finite" not in result.stderr


def test_help_text_mentions_default_and_audit():
    """Help text must surface default value + audit-log behavior."""
    result = _run_script(["--help"])
    assert result.returncode == 0
    assert "--min-confidence" in result.stdout
    assert "SKIPPED row" in result.stdout, "help should describe the audit log entry"
    assert "below_min_confidence" in result.stdout, "help should mention reason string"


def test_default_value_is_from_config():
    """Default --min-confidence must come from default_config.polymarket_min_confidence."""
    from tradingagents.default_config import DEFAULT_CONFIG
    result = _run_script(["--help"])
    expected = DEFAULT_CONFIG["polymarket_min_confidence"]
    assert f"{expected}" in result.stdout, (
        f"help text must show default {expected} from DEFAULT_CONFIG"
    )


def test_skip_payload_shape(tmp_path: Path):
    """Verify the SKIPPED-row JSONL payload contains the audit fields we promise."""
    # Build the exact shape the script writes; if this drifts from the script,
    # the integration test for --min-confidence will catch it.
    from tradingagents.exchange.io_utils import append_jsonl

    payload = {
        "ts": "2026-05-16T10:00:00+00:00",
        "market_id": "12345",
        "question": "Test market",
        "direction": "BUY_YES",
        "confidence": 0.7,
        "yes_price_at_analysis": 0.3,
        "status": "SKIPPED",
        "reason": "below_min_confidence",
        "min_confidence": 0.85,
    }
    fill_log = tmp_path / "paper-fills-2026-05-16.jsonl"
    append_jsonl(fill_log, payload)
    rows = [json.loads(line) for line in fill_log.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "SKIPPED"
    assert row["reason"] == "below_min_confidence"
    assert row["min_confidence"] == 0.85
    assert row["confidence"] == 0.7
    assert row["direction"] == "BUY_YES"
