"""Tests for pre-fire balance & position reconciliation.

Safety-critical: reconcile() must HALT on any drift or missing data, and only
return ok=True when expected and actual genuinely agree.
"""

from __future__ import annotations

from tradingagents.exchange.reconciliation import (
    reconcile,
    count_open_positions_from_fills,
)


def test_clean_match_is_ok():
    r = reconcile(
        expected_open_positions=2, actual_open_positions=2,
        intended_capital_usd=100.0, actual_balance_usd=100.0,
    )
    assert r.ok is True
    assert r.halt_reasons == ()
    assert bool(r) is True


def test_position_drift_halts():
    r = reconcile(
        expected_open_positions=1, actual_open_positions=3,
        intended_capital_usd=100.0, actual_balance_usd=100.0,
    )
    assert r.ok is False
    assert "position_drift" in r.halt_reasons
    assert r.detail["position_drift"] == 2


def test_missing_positions_halts():
    r = reconcile(
        expected_open_positions=0, actual_open_positions=None,
        intended_capital_usd=100.0, actual_balance_usd=100.0,
    )
    assert r.ok is False
    assert "positions_unavailable" in r.halt_reasons


def test_missing_balance_halts():
    r = reconcile(
        expected_open_positions=0, actual_open_positions=0,
        intended_capital_usd=100.0, actual_balance_usd=None,
    )
    assert r.ok is False
    assert "balance_unavailable" in r.halt_reasons


def test_insufficient_balance_halts():
    r = reconcile(
        expected_open_positions=0, actual_open_positions=0,
        intended_capital_usd=100.0, actual_balance_usd=60.0,
    )
    assert r.ok is False
    assert "insufficient_balance" in r.halt_reasons
    assert r.detail["balance_shortfall_usd"] == 40.0


def test_balance_within_tolerance_is_ok():
    """Minor fee/rounding diffs within tolerance are noise, not a halt."""
    r = reconcile(
        expected_open_positions=0, actual_open_positions=0,
        intended_capital_usd=100.0, actual_balance_usd=99.5,  # 0.5 < 1.0 tol
    )
    assert r.ok is True


def test_balance_just_below_tolerance_halts():
    r = reconcile(
        expected_open_positions=0, actual_open_positions=0,
        intended_capital_usd=100.0, actual_balance_usd=98.0,  # 2.0 > 1.0 tol
    )
    assert r.ok is False
    assert "insufficient_balance" in r.halt_reasons


def test_negative_values_halt():
    r = reconcile(
        expected_open_positions=0, actual_open_positions=-1,
        intended_capital_usd=100.0, actual_balance_usd=-5.0,
    )
    assert r.ok is False
    assert "positions_negative" in r.halt_reasons
    assert "balance_negative" in r.halt_reasons


def test_surplus_balance_is_ok():
    """More money than we'll size against is fine."""
    r = reconcile(
        expected_open_positions=0, actual_open_positions=0,
        intended_capital_usd=100.0, actual_balance_usd=500.0,
    )
    assert r.ok is True


# ---- count_open_positions_from_fills ----

def test_count_confirmed_fills():
    rows = [
        {"market_id": "A", "outcome": "FILLED"},
        {"market_id": "B", "outcome": "FILLED"},
        {"market_id": "C", "outcome": "UNFILLED"},
        {"market_id": "D", "status": "SKIPPED"},
    ]
    assert count_open_positions_from_fills(rows) == 2


def test_count_legacy_paper_fills():
    rows = [
        {"market_id": "A", "filled": True, "status": "FILLED"},
        {"market_id": "B", "filled": True},
    ]
    assert count_open_positions_from_fills(rows) == 2


def test_count_excludes_unconfirmed():
    rows = [
        {"market_id": "A", "outcome": "UNCONFIRMED"},
        {"market_id": "B", "status": "ERROR"},
    ]
    assert count_open_positions_from_fills(rows) == 0


def test_count_zeroes_resolved_markets():
    rows = [
        {"market_id": "A", "outcome": "FILLED"},
        {"market_id": "A", "status": "RESOLVED_WIN"},  # closed
    ]
    assert count_open_positions_from_fills(rows) == 0


def test_count_ignores_rows_without_market_id():
    rows = [{"outcome": "FILLED"}, {"filled": True}]
    assert count_open_positions_from_fills(rows) == 0
