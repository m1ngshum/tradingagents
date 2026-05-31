"""Tests for the centralized fire-level safety gate (gate.py).

These prove the safety properties the run scripts rely on:
  (a) any tripped guard => live disabled for the WHOLE fire (FIRE_HALT);
  (b) the verdict is a single value computed from injected state, so a later
      per-market decision cannot re-enable live;
  (c) reconcile-failure (incl. unavailable data) halts — fail closed.
The gate is pure, so no mocks are needed for evaluate_fire.
"""

import pytest

from tradingagents.exchange import gate
from tradingagents.exchange.gate import (
    FIRE_HALT,
    OK,
    FireVerdict,
    env_flag_active,
    evaluate_fire,
    evaluate_market,
    kill_switch_active,
)


# ---------------------------------------------------------------------------
# env_flag_active / kill_switch_active
# ---------------------------------------------------------------------------

class TestEnvFlag:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "x", " 1 "])
    def test_truthy(self, val):
        assert env_flag_active(val) is True

    @pytest.mark.parametrize("val", [None, "", "0", "false", "FALSE", "no", "  ", " no "])
    def test_falsey(self, val):
        assert env_flag_active(val) is False

    def test_kill_switch_reads_env(self, monkeypatch):
        monkeypatch.delenv(gate.KILL_SWITCH_ENV, raising=False)
        assert kill_switch_active() is False
        monkeypatch.setenv(gate.KILL_SWITCH_ENV, "1")
        assert kill_switch_active() is True
        monkeypatch.setenv(gate.KILL_SWITCH_ENV, "0")
        assert kill_switch_active() is False


# ---------------------------------------------------------------------------
# evaluate_fire — the fire-level gate
# ---------------------------------------------------------------------------

class TestEvaluateFire:
    def test_all_clear_allows_live(self):
        v = evaluate_fire(kill_switch_on=False, breaker_tripped=False, reconcile_ok=True)
        assert v.live_allowed is True
        assert v.level == OK
        assert v.reason_codes == ()
        assert bool(v) is True

    def test_kill_switch_halts(self):
        v = evaluate_fire(kill_switch_on=True, breaker_tripped=False, reconcile_ok=True)
        assert v.live_allowed is False
        assert v.level == FIRE_HALT
        assert "KILL_SWITCH" in v.reason_codes
        assert bool(v) is False

    def test_breaker_halts(self):
        v = evaluate_fire(kill_switch_on=False, breaker_tripped=True, reconcile_ok=True)
        assert not v.live_allowed
        assert "LOSS_BREAKER" in v.reason_codes

    def test_reconcile_failure_halts(self):
        """Property (c): reconcile not OK (incl. unavailable data) => halt."""
        v = evaluate_fire(kill_switch_on=False, breaker_tripped=False, reconcile_ok=False)
        assert not v.live_allowed
        assert "RECONCILE" in v.reason_codes

    def test_multiple_guards_all_reported(self):
        v = evaluate_fire(kill_switch_on=True, breaker_tripped=True, reconcile_ok=False)
        assert not v.live_allowed
        assert set(v.reason_codes) == {"KILL_SWITCH", "LOSS_BREAKER", "RECONCILE"}

    def test_verdict_is_frozen(self):
        v = evaluate_fire(kill_switch_on=False, breaker_tripped=False, reconcile_ok=True)
        with pytest.raises(Exception):
            v.live_allowed = True  # type: ignore[misc]

    def test_only_all_three_clear_allows(self):
        """Property (a): live is allowed iff EVERY guard is clear."""
        for kill in (True, False):
            for brk in (True, False):
                for rec in (True, False):
                    v = evaluate_fire(
                        kill_switch_on=kill, breaker_tripped=brk, reconcile_ok=rec
                    )
                    expected = (not kill) and (not brk) and rec
                    assert v.live_allowed is expected


# ---------------------------------------------------------------------------
# Forced-drift composition: reconcile() -> evaluate_fire() must HALT.
# This is the Phase-1 "forced-drift must halt" release gate at unit level:
# a position-count mismatch makes reconcile().ok False, which the gate turns
# into FIRE_HALT.
# ---------------------------------------------------------------------------

class TestReconDriftHalts:
    def _gate_from_recon(self, expected, actual, *, capital=49.0, balance=49.0):
        from tradingagents.exchange.reconciliation import reconcile
        recon = reconcile(
            expected_open_positions=expected,
            actual_open_positions=actual,
            intended_capital_usd=capital,
            actual_balance_usd=balance,
        )
        return evaluate_fire(
            kill_switch_on=False, breaker_tripped=False, reconcile_ok=recon.ok
        ), recon

    def test_position_drift_halts(self):
        v, recon = self._gate_from_recon(expected=1, actual=2)
        assert "position_drift" in recon.halt_reasons
        assert not v.live_allowed
        assert "RECONCILE" in v.reason_codes

    def test_positions_unavailable_halts(self):
        """get_open_position_count() returning None (fetch failed) must halt."""
        v, recon = self._gate_from_recon(expected=0, actual=None)
        assert "positions_unavailable" in recon.halt_reasons
        assert not v.live_allowed

    def test_matched_positions_allow(self):
        v, recon = self._gate_from_recon(expected=2, actual=2)
        assert recon.ok
        assert v.live_allowed


# ---------------------------------------------------------------------------
# NOTIONAL_EXPOSURE ceiling (Phase 2) — the real cap for slow-settling
# positions the realized-loss breaker can't see.
# ---------------------------------------------------------------------------

class TestNotionalExposure:
    def test_over_budget_halts(self):
        v = evaluate_fire(
            kill_switch_on=False, breaker_tripped=False, reconcile_ok=True,
            open_exposure_usd=49.0, exposure_budget_usd=49.0,
        )
        assert not v.live_allowed
        assert "NOTIONAL_EXPOSURE" in v.reason_codes

    def test_under_budget_allows(self):
        v = evaluate_fire(
            kill_switch_on=False, breaker_tripped=False, reconcile_ok=True,
            open_exposure_usd=48.99, exposure_budget_usd=49.0,
        )
        assert v.live_allowed

    def test_zero_budget_disables_check(self):
        """Default budget 0 => ceiling disabled, so existing callers/tests are
        unaffected even with huge open exposure."""
        v = evaluate_fire(
            kill_switch_on=False, breaker_tripped=False, reconcile_ok=True,
            open_exposure_usd=10_000.0, exposure_budget_usd=0.0,
        )
        assert v.live_allowed
        assert "NOTIONAL_EXPOSURE" not in v.reason_codes

    def test_exposure_composes_with_other_guards(self):
        v = evaluate_fire(
            kill_switch_on=False, breaker_tripped=True, reconcile_ok=True,
            open_exposure_usd=100.0, exposure_budget_usd=49.0,
        )
        assert not v.live_allowed
        assert {"LOSS_BREAKER", "NOTIONAL_EXPOSURE"} <= set(v.reason_codes)


class TestOpenExposureFromFills:
    def test_sums_open_cost_basis(self):
        from tradingagents.exchange.reconciliation import open_exposure_from_fills
        rows = [
            {"market_id": "m1", "outcome": "FILLED", "filled_usd": 10.0},
            {"market_id": "m2", "filled": True, "status": "ok", "filled_usd": 5.5},
        ]
        assert open_exposure_from_fills(rows) == pytest.approx(15.5)

    def test_excludes_non_positions(self):
        from tradingagents.exchange.reconciliation import open_exposure_from_fills
        rows = [
            {"market_id": "m1", "status": "SKIPPED", "filled_usd": 99.0},
            {"market_id": "m2", "status": "UNFILLED", "filled_usd": 99.0},
            {"market_id": "m3", "outcome": "UNCONFIRMED", "filled_usd": 99.0},
        ]
        assert open_exposure_from_fills(rows) == 0.0

    def test_close_record_zeroes_market(self):
        from tradingagents.exchange.reconciliation import open_exposure_from_fills
        rows = [
            {"market_id": "m1", "outcome": "FILLED", "filled_usd": 20.0},
            {"market_id": "m1", "status": "RESOLVED_WIN"},
        ]
        assert open_exposure_from_fills(rows) == 0.0

    def test_unparseable_amount_contributes_zero(self):
        from tradingagents.exchange.reconciliation import open_exposure_from_fills
        rows = [
            {"market_id": "m1", "outcome": "FILLED", "filled_usd": "abc"},
            {"market_id": "m2", "outcome": "FILLED", "filled_usd": 7.0},
        ]
        assert open_exposure_from_fills(rows) == pytest.approx(7.0)

    def test_empty_is_zero(self):
        from tradingagents.exchange.reconciliation import open_exposure_from_fills
        assert open_exposure_from_fills([]) == 0.0


# ---------------------------------------------------------------------------
# evaluate_market — per-market gate (confidence floor + opt-in cost-aware edge)
# ---------------------------------------------------------------------------

class TestEvaluateMarket:
    def test_hold_vetoed(self):
        v = evaluate_market(direction="HOLD", confidence=0.9, min_confidence=0.85, buy_price=0.3)
        assert not v.allow and v.reason_code == "HOLD"

    def test_below_confidence_vetoed(self):
        v = evaluate_market(direction="BUY_YES", confidence=0.80, min_confidence=0.85, buy_price=0.3)
        assert not v.allow and v.reason_code == "CONFIDENCE_FLOOR"

    def test_edge_gate_disabled_by_default(self):
        """min_edge=0 => no edge gate even when edge is tiny/negative."""
        v = evaluate_market(direction="BUY_YES", confidence=0.86, min_confidence=0.85, buy_price=0.90)
        assert v.allow and v.reason_code == OK

    def test_edge_below_cost_vetoed(self):
        # edge = 0.86 - 0.84 = 0.02 < min_edge 0.03
        v = evaluate_market(
            direction="BUY_YES", confidence=0.86, min_confidence=0.85,
            buy_price=0.84, min_edge=0.03,
        )
        assert not v.allow and v.reason_code == "EDGE_NET_OF_COST"

    def test_edge_above_cost_allows(self):
        v = evaluate_market(
            direction="BUY_YES", confidence=0.95, min_confidence=0.85,
            buy_price=0.50, min_edge=0.03,
        )
        assert v.allow and v.reason_code == OK

    def test_buy_no_uses_passed_buy_price(self):
        v = evaluate_market(
            direction="BUY_NO", confidence=0.90, min_confidence=0.85,
            buy_price=0.20, min_edge=0.03,
        )
        assert v.allow
        assert v.detail["edge"] == pytest.approx(0.70)

    def test_confidence_checked_before_edge(self):
        """Ordered short-circuit: low confidence reports CONFIDENCE_FLOOR even
        if the edge would also fail."""
        v = evaluate_market(
            direction="BUY_YES", confidence=0.10, min_confidence=0.85,
            buy_price=0.99, min_edge=0.03,
        )
        assert v.reason_code == "CONFIDENCE_FLOOR"
