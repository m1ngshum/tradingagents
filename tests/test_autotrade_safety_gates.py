"""Tests for the autotrade (--live) safety gates added when enabling live mode.

Three gates covered:
1. Executor's _MIN_CONFIDENCE raised from 0.55 → 0.85 (defense in depth: matches
   script-level default so anyone running --min-confidence below 0.85 still gets
   blocked at the executor layer).
2. --max-orders-per-fire flag (default 5): caps the number of SUBMITTED live
   orders in one fire even if cluster cap and min-confidence let many through.
3. TRADINGAGENTS_AUTOTRADE_KILL_SWITCH env var: out-of-band disable for --live.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "run_polymarket.py"


def _run_script(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Same isolation pattern as test_run_polymarket_min_confidence.py."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "EXA_API_KEY": "",
        "OPENROUTER_API_KEY": "",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Executor min-confidence (defense in depth)
# ---------------------------------------------------------------------------


def test_executor_min_confidence_is_085():
    """_MIN_CONFIDENCE on the executor must match the script-level default
    (0.85). If anyone lowers --min-confidence below this, the executor still
    refuses to size a live order. Caught in PR #19 era — would have made the
    NO EDGE bot autotrade losing positions."""
    from tradingagents.exchange.polymarket_executor import _MIN_CONFIDENCE
    assert _MIN_CONFIDENCE == 0.85, (
        f"executor _MIN_CONFIDENCE drifted from script default 0.85 (now {_MIN_CONFIDENCE}). "
        f"Either update DEFAULT_CONFIG.polymarket_min_confidence to match or revert this."
    )


# ---------------------------------------------------------------------------
# --max-orders-per-fire (per-fire cap on submitted orders)
# ---------------------------------------------------------------------------


def test_max_orders_per_fire_in_help():
    """Flag must be documented in --help."""
    result = _run_script(["--help"])
    assert result.returncode == 0
    assert "--max-orders-per-fire" in result.stdout
    assert "5" in result.stdout, "help must show default value"
    assert "max_orders_per_fire" in result.stdout, "help must mention the skip reason string"


def test_max_orders_per_fire_accepts_zero_to_disable():
    """0 must mean 'no cap' (matches the convention of --max-per-cluster=0)."""
    result = _run_script(["--max-orders-per-fire", "0", "--limit", "1"])
    # argparse passes; script will then fail on missing EXA key.
    assert "must be a finite" not in result.stderr
    assert "--max-orders-per-fire" not in result.stderr or "invalid" not in result.stderr


def test_max_orders_per_fire_rejects_non_int():
    """Type validation — non-int values get rejected by argparse with exit 2."""
    result = _run_script(["--max-orders-per-fire", "not-a-number", "--limit", "1"])
    assert result.returncode == 2
    assert "invalid int" in result.stderr or "max-orders-per-fire" in result.stderr


# ---------------------------------------------------------------------------
# Kill switch env var
# ---------------------------------------------------------------------------


def test_kill_switch_disables_live_mode():
    """Setting TRADINGAGENTS_AUTOTRADE_KILL_SWITCH=1 + --live must downgrade
    to paper mode silently (with a stderr warning), not crash. This is the
    out-of-band emergency stop that doesn't require editing the routine UI.

    NOTE: this test doesn't actually run live mode — it asserts that the
    kill-switch path is triggered by checking stderr for the warning. The
    script will still exit with code 2 because EXA_API_KEY is empty, but
    the kill-switch warning should appear BEFORE that exit."""
    result = _run_script(
        ["--live", "--limit", "1"],
        env_extra={"TRADINGAGENTS_AUTOTRADE_KILL_SWITCH": "1"},
    )
    # The script should emit the kill-switch warning to stderr.
    assert "AUTOTRADE KILL SWITCH ACTIVE" in result.stderr, (
        f"kill switch did not trigger; stderr:\n{result.stderr}"
    )


def test_kill_switch_empty_does_not_trigger():
    """Empty string env var must NOT trigger the kill switch (since unset
    env vars often appear as empty strings in dotenv-style configs)."""
    result = _run_script(
        ["--live", "--limit", "1"],
        env_extra={"TRADINGAGENTS_AUTOTRADE_KILL_SWITCH": ""},
    )
    assert "AUTOTRADE KILL SWITCH ACTIVE" not in result.stderr


@pytest.mark.parametrize("falsy", ["0", "false", "no", "FALSE", "False"])
def test_kill_switch_falsy_values_do_not_trigger(falsy: str):
    """Explicit falsy values (0, false, no) must NOT trigger so users can
    leave the env var permanently set and toggle by value."""
    result = _run_script(
        ["--live", "--limit", "1"],
        env_extra={"TRADINGAGENTS_AUTOTRADE_KILL_SWITCH": falsy},
    )
    assert "AUTOTRADE KILL SWITCH ACTIVE" not in result.stderr


def test_kill_switch_inactive_without_live_flag():
    """Without --live, the kill switch has nothing to do — must be a no-op."""
    result = _run_script(
        ["--limit", "1"],
        env_extra={"TRADINGAGENTS_AUTOTRADE_KILL_SWITCH": "1"},
    )
    assert "AUTOTRADE KILL SWITCH ACTIVE" not in result.stderr
