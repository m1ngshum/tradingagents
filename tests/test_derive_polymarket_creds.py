"""Tests for scripts/derive_polymarket_creds.py.

The actual derivation (HTTP + EIP-712 sig) is not exercised here — only the
input-validation guards. Routine startup depends on this script either
emitting valid `export` lines OR exiting non-zero so the bash gate can
fall back to paper mode without crashing the fire.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "derive_polymarket_creds.py"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    base = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    base.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, env=base, timeout=10,
    )


def test_missing_private_key_exits_2():
    r = _run({"POLYMARKET_FUNDER": "0xabc"})
    assert r.returncode == 2
    assert "POLYMARKET_PRIVATE_KEY" in r.stderr


def test_missing_funder_exits_2():
    r = _run({"POLYMARKET_PRIVATE_KEY": "0xdeadbeef"})
    assert r.returncode == 2
    assert "POLYMARKET_FUNDER" in r.stderr


def test_empty_string_treated_as_missing():
    """Routine sets vars to '' when the user hasn't pasted them yet; must
    behave the same as if they were unset. Otherwise the script crashes
    py_clob_client deep inside instead of failing cleanly at the gate."""
    r = _run({"POLYMARKET_PRIVATE_KEY": "", "POLYMARKET_FUNDER": "0xabc"})
    assert r.returncode == 2
    assert "POLYMARKET_PRIVATE_KEY" in r.stderr


def test_whitespace_only_treated_as_missing():
    r = _run({"POLYMARKET_PRIVATE_KEY": "   ", "POLYMARKET_FUNDER": "0xabc"})
    assert r.returncode == 2
