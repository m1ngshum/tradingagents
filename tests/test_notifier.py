"""Tests for the operational notifier.

Safety property: the notifier must NEVER raise and must be a clean no-op when
unconfigured. We don't hit a real network — we patch urlopen.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from tradingagents.exchange.notifier import Notifier


def test_unconfigured_is_noop():
    n = Notifier(webhook_url=None)
    assert n.enabled is False
    assert n.send("hello") is False  # no-op, returns False, no raise


def test_configured_sends():
    n = Notifier(webhook_url="https://hooks.slack.test/xxx")
    assert n.enabled is True
    with patch("urllib.request.urlopen", return_value=MagicMock()) as mock:
        assert n.send("hello", severity="info") is True
        assert mock.called
        # payload is JSON with a text field carrying the severity prefix
        req = mock.call_args[0][0]
        body = req.data.decode("utf-8")
        assert "hello" in body
        assert "information_source" in body  # info emoji


def test_send_never_raises_on_network_error():
    n = Notifier(webhook_url="https://hooks.slack.test/xxx")
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        assert n.send("hello") is False  # swallowed, no raise


def test_send_never_raises_on_unexpected_error():
    n = Notifier(webhook_url="https://hooks.slack.test/xxx")
    with patch("urllib.request.urlopen", side_effect=RuntimeError("weird")):
        assert n.send("hello") is False


def test_severity_prefixes():
    n = Notifier(webhook_url="https://hooks.slack.test/xxx")
    with patch("urllib.request.urlopen", return_value=MagicMock()) as mock:
        n.send("x", severity="critical")
        body = mock.call_args[0][0].data.decode("utf-8")
        assert "rotating_light" in body


def test_convenience_helpers_send():
    n = Notifier(webhook_url="https://hooks.slack.test/xxx")
    with patch("urllib.request.urlopen", return_value=MagicMock()) as mock:
        assert n.order_filled("Will X happen?", "BUY_NO", 20.0, "ord123") is True
        assert n.breaker_tripped(["daily_loss_limit"], {"daily_realized_pnl": -30}) is True
        assert n.reconciliation_halt(["insufficient_balance"], {"shortfall": 40}) is True
        assert n.unconfirmed_order("Mkt", "ord9", "delayed") is True
        assert n.fatal("fire", "boom") is True
        assert mock.call_count == 5


def test_helpers_noop_when_unconfigured():
    n = Notifier(webhook_url=None)
    # All helpers must be safe no-ops, never raise
    assert n.order_filled("m", "BUY_YES", 1.0, "id") is False
    assert n.breaker_tripped([], {}) is False
    assert n.fatal("ctx", "err") is False


def test_env_var_picked_up(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_ALERT_WEBHOOK", "https://hooks.slack.test/env")
    n = Notifier()
    assert n.enabled is True
