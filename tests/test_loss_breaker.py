"""Tests for the realized-loss circuit breaker.

Safety-critical: these verify the breaker trips when it must, stays tripped
appropriately, and FAILS CLOSED on corruption (the property that makes
unattended automation safe).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingagents.exchange.loss_breaker import LossBreaker


def _bk(tmp_path: Path, daily=30.0, dd=50.0) -> LossBreaker:
    return LossBreaker(tmp_path / "lb.json", daily_loss_limit=daily, drawdown_limit=dd)


def test_fresh_breaker_not_tripped(tmp_path):
    assert _bk(tmp_path).is_tripped() is False


def test_daily_loss_trips_at_limit(tmp_path):
    bk = _bk(tmp_path, daily=30.0)
    bk.record_realized_pnl(-20.0)
    assert bk.is_tripped() is False
    bk.record_realized_pnl(-10.0)  # cumulative -30 == limit
    assert bk.is_tripped() is True


def test_daily_loss_does_not_trip_on_wins(tmp_path):
    bk = _bk(tmp_path, daily=30.0)
    bk.record_realized_pnl(-25.0)
    bk.record_realized_pnl(+50.0)  # net +25
    assert bk.is_tripped() is False


def test_drawdown_trips_from_peak(tmp_path):
    bk = _bk(tmp_path, daily=0.0, dd=50.0)  # daily disabled, dd=50
    bk.set_equity(100.0)          # peak=100
    bk.record_realized_pnl(-49.0) # equity 51, dd 49
    assert bk.is_tripped() is False
    bk.record_realized_pnl(-1.0)  # equity 50, dd 50 == limit
    assert bk.is_tripped() is True


def test_drawdown_measured_from_peak_not_start(tmp_path):
    bk = _bk(tmp_path, daily=0.0, dd=50.0)
    bk.set_equity(100.0)
    bk.record_realized_pnl(+100.0)  # equity 200, peak 200
    bk.record_realized_pnl(-49.0)   # equity 151, dd from peak = 49
    assert bk.is_tripped() is False
    bk.record_realized_pnl(-2.0)    # equity 149, dd 51 > 50
    assert bk.is_tripped() is True


def test_limit_zero_disables(tmp_path):
    bk = _bk(tmp_path, daily=0.0, dd=0.0)
    bk.set_equity(100.0)
    bk.record_realized_pnl(-9999.0)
    assert bk.is_tripped() is False


def test_manual_trip_is_sticky(tmp_path):
    bk = _bk(tmp_path)
    bk.trip_manually()
    assert bk.is_tripped() is True
    # New instance reading same file still tripped
    bk2 = _bk(tmp_path)
    assert bk2.is_tripped() is True


def test_reset_clears_trip(tmp_path):
    bk = _bk(tmp_path)
    bk.trip_manually()
    assert bk.is_tripped() is True
    bk.reset()
    assert bk.is_tripped() is False


def test_state_persists_across_instances(tmp_path):
    bk = _bk(tmp_path, daily=30.0)
    bk.record_realized_pnl(-30.0)
    assert _bk(tmp_path, daily=30.0).is_tripped() is True


def test_fails_closed_on_corrupt_state(tmp_path):
    """THE safety property: corrupt state => tripped, not open."""
    p = tmp_path / "lb.json"
    p.write_text("{ this is not valid json ")
    bk = LossBreaker(p, daily_loss_limit=30.0, drawdown_limit=50.0)
    assert bk.is_tripped() is True
    assert "state_corrupt" in bk.status()["reasons"]


def test_daily_resets_at_utc_rollover(tmp_path):
    p = tmp_path / "lb.json"
    # Hand-write yesterday's tripped daily state
    p.write_text(json.dumps({
        "day": "2020-01-01",
        "daily_realized_pnl": -100.0,
        "peak_equity": 100.0,
        "current_equity": 0.0,
        "manual_trip": False,
    }))
    bk = LossBreaker(p, daily_loss_limit=30.0, drawdown_limit=0.0)
    # Daily loss rolled over -> daily limit no longer tripped
    s = bk.status()
    assert s["daily_realized_pnl"] == 0.0
    assert "daily_loss_limit" not in s["reasons"]


def test_drawdown_persists_across_day_rollover(tmp_path):
    """Drawdown is sticky across days — a max-DD breach means broken strategy."""
    p = tmp_path / "lb.json"
    p.write_text(json.dumps({
        "day": "2020-01-01",
        "daily_realized_pnl": -100.0,
        "peak_equity": 100.0,
        "current_equity": 40.0,  # dd=60
        "manual_trip": False,
    }))
    bk = LossBreaker(p, daily_loss_limit=0.0, drawdown_limit=50.0)
    assert bk.is_tripped() is True  # dd survives rollover
    assert "max_drawdown" in bk.status()["reasons"]


def test_non_numeric_pnl_ignored(tmp_path):
    bk = _bk(tmp_path)
    bk.record_realized_pnl("oops")  # type: ignore[arg-type]
    bk.record_realized_pnl(None)    # type: ignore[arg-type]
    assert bk.status()["daily_realized_pnl"] == 0.0


def test_status_shape(tmp_path):
    bk = _bk(tmp_path, daily=30.0, dd=50.0)
    bk.set_equity(100.0)
    bk.record_realized_pnl(-10.0)
    s = bk.status()
    assert s["tripped"] is False
    assert s["daily_realized_pnl"] == -10.0
    assert s["daily_loss_limit"] == 30.0
    assert s["drawdown_limit"] == 50.0
    assert s["peak_equity"] == 100.0
