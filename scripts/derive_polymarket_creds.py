"""Derive L2 Polymarket CLOB credentials from L1 private key.

Polymarket's CLOB requires four secrets for live order submission:
  POLYMARKET_PRIVATE_KEY   — L1 (the wallet's EOA private key)
  POLYMARKET_KEY           — L2 API key (UUID-shaped)
  POLYMARKET_SECRET        — L2 API secret
  POLYMARKET_PASSPHRASE    — L2 API passphrase

Only the L1 private key is sensitive at the cryptographic level — the L2
triple is derived deterministically from it via the `auth/derive-api-key`
endpoint (or created on first use via `create_api_key`). This script wraps
`py_clob_client.create_or_derive_api_creds()` and prints the derived L2
credentials in shell-eval form so the routine can `eval $(... )` them.

Reads from env:
  POLYMARKET_PRIVATE_KEY   — required
  POLYMARKET_FUNDER        — required (proxy wallet address, public)

Writes to stdout (shell-eval format):
  export POLYMARKET_KEY="..."
  export POLYMARKET_SECRET="..."
  export POLYMARKET_PASSPHRASE="..."

Exits non-zero on missing input or derivation failure; the routine should
catch this and fall back to paper mode.
"""

from __future__ import annotations

import os
import sys


_HOST = "https://clob.polymarket.com"
_CHAIN_ID = 137  # Polygon


def main() -> int:
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY missing", file=sys.stderr)
        return 2
    if not funder:
        print("ERROR: POLYMARKET_FUNDER missing", file=sys.stderr)
        return 2

    try:
        from py_clob_client.client import ClobClient
    except ImportError as e:
        print(f"ERROR: py_clob_client not installed: {e}", file=sys.stderr)
        return 3

    try:
        # Use signature_type=0 (EOA) — key address == funder address. Matches
        # PolymarketExecutor in tradingagents/exchange/polymarket_executor.py.
        client = ClobClient(
            _HOST,
            key=private_key,
            chain_id=_CHAIN_ID,
            signature_type=0,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: derive_api_creds failed: {e}", file=sys.stderr)
        return 4

    # Shell-eval output. Quote values for safety; the L2 triple is base64-ish
    # so shouldn't contain shell metachars, but quote anyway.
    print(f'export POLYMARKET_KEY="{creds.api_key}"')
    print(f'export POLYMARKET_SECRET="{creds.api_secret}"')
    print(f'export POLYMARKET_PASSPHRASE="{creds.api_passphrase}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
