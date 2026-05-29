"""Operational alerting for unattended trading.

A bot that fails SILENTLY in a cloud routine log is the worst failure mode:
you find out days later, after the damage. This pushes structured alerts to
a Slack incoming webhook on the events a human must know about:

  - a live order FILLED (money moved)
  - a circuit breaker TRIPPED (loss/drawdown limit hit)
  - a reconciliation HALT (balance/position drift)
  - an UNCONFIRMED order (needs manual reconciliation)
  - a fatal error in the fire

Design:
  - stdlib only (urllib) — no new dependency.
  - FAIL-SAFE: alerting must NEVER crash or block the trading loop. Any send
    error is swallowed + logged. A broken notifier degrades to silence, not
    to a crashed fire.
  - NO-OP when unconfigured: if TRADINGAGENTS_ALERT_WEBHOOK is unset, send()
    is a logged no-op. Safe to call unconditionally from paper/test runs.
  - Severity prefix so a human can eyeball urgency at a glance.

Config: TRADINGAGENTS_ALERT_WEBHOOK = Slack incoming-webhook URL.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

ENV_WEBHOOK = "TRADINGAGENTS_ALERT_WEBHOOK"

# Severity → emoji prefix for at-a-glance triage in the channel.
_SEV = {
    "info": ":information_source:",
    "warn": ":warning:",
    "critical": ":rotating_light:",
}


class Notifier:
    """Send fire-and-forget operational alerts. Never raises."""

    def __init__(self, webhook_url: str | None = None, timeout: float = 5.0) -> None:
        self._url = webhook_url if webhook_url is not None else os.environ.get(ENV_WEBHOOK)
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def send(self, message: str, severity: str = "info") -> bool:
        """Post a message. Returns True if sent, False if no-op/failed.

        NEVER raises — a notifier failure must not abort a trading fire.
        """
        prefix = _SEV.get(severity, "")
        text = f"{prefix} {message}".strip()
        if not self._url:
            logger.info("[alert:%s no-webhook] %s", severity, message)
            return False
        try:
            payload = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(
                self._url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=self._timeout)  # noqa: S310 (trusted webhook URL)
            return True
        except (urllib.error.URLError, ValueError, OSError) as e:
            logger.warning("alert send failed (swallowed): %s", e)
            return False
        except Exception as e:  # noqa: BLE001 — alerting must never crash the loop
            logger.warning("alert send unexpected error (swallowed): %s", e)
            return False

    # --- convenience helpers for the events that matter ---

    def order_filled(self, market: str, direction: str, usd: float, order_id: str) -> bool:
        return self.send(
            f"FILLED {direction} ${usd:.2f} on '{market[:60]}' (id={order_id})",
            severity="info",
        )

    def breaker_tripped(self, reasons: list[str], detail: dict) -> bool:
        return self.send(
            f"LOSS BREAKER TRIPPED {reasons} — {detail}. Trading downgraded to paper.",
            severity="critical",
        )

    def reconciliation_halt(self, reasons: list[str], detail: dict) -> bool:
        return self.send(
            f"RECONCILIATION HALT {reasons} — {detail}. Trading downgraded to paper.",
            severity="critical",
        )

    def unconfirmed_order(self, market: str, order_id: str, raw_status: str) -> bool:
        return self.send(
            f"UNCONFIRMED order on '{market[:60]}' (id={order_id}, status={raw_status}) "
            f"— RECONCILE MANUALLY.",
            severity="warn",
        )

    def fatal(self, context: str, error: str) -> bool:
        return self.send(f"FATAL in {context}: {error}", severity="critical")
