"""READ-ONLY diagnostic: find the correct (signature_type, funder) for a
Polymarket account. Places NO orders, moves NO money.

The problem this solves: email/Magic Polymarket accounts use a proxy wallet
that holds the USDC, separate from the signer EOA. The CLOB client does NOT
auto-derive that proxy address — you must pass it as `funder`. Guess wrong and
orders sign against an empty wallet. This script confirms the right combo
empirically: it constructs a read-only client for each plausible
(signature_type, funder) pair and queries the collateral balance. The combo
that reports your real balance (e.g. the ~$49 you see in the UI) is the
correct one to put in the routine env.

Usage (run LOCALLY, key stays in YOUR env — never in chat):

    export POLYMARKET_PRIVATE_KEY=0x...        # your exported key
    # candidate funder addresses to test (space-separated): the signer itself,
    # plus any proxy/deposit addresses you can find in the Polymarket UI.
    export POLYMARKET_FUNDER_CANDIDATES="0xSIGNER 0xPROXY1 0xPROXY2"
    python scripts/diagnose_polymarket_funder.py

Output: a table of (signature_type, funder) -> balance. Pick the row whose
balance matches your account. Set POLYMARKET_SIGNATURE_TYPE + POLYMARKET_FUNDER
to that combo.

Nothing here submits an order. The only network calls are balance reads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_HOST = "https://clob.polymarket.com"
_CHAIN_ID = 137  # Polygon


def _signer_address(private_key: str) -> str | None:
    try:
        from eth_account import Account
        return Account.from_key(private_key).address
    except Exception as e:  # noqa: BLE001
        print(f"  (could not derive signer address: {e})", file=sys.stderr)
        return None


def _read_balance(private_key: str, funder: str, sig_type: int) -> tuple[float | None, str]:
    """Construct a read-only client for (funder, sig_type) and read collateral
    balance. Returns (usdc_balance_or_None, note). No orders, no writes."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import (
            ApiCreds, BalanceAllowanceParams, AssetType,
        )
    except ImportError as e:
        return None, f"py-clob-client missing: {e}"

    try:
        # Derive L2 creds from the key (read path; creates-or-derives, no order).
        boot = ClobClient(_HOST, key=private_key, chain_id=_CHAIN_ID,
                          signature_type=sig_type, funder=funder)
        creds = boot.create_or_derive_api_creds()
        client = ClobClient(
            _HOST, key=private_key, chain_id=_CHAIN_ID, creds=creds,
            signature_type=sig_type, funder=funder,
        )
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = client.get_balance_allowance(params)
        raw = resp.get("balance") if isinstance(resp, dict) else None
        if raw is None:
            return None, "no balance field in response"
        return float(raw) / 1_000_000.0, "ok"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:80]}"


def main() -> int:
    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not key:
        print("ERROR: set POLYMARKET_PRIVATE_KEY in your env (locally, not in chat).",
              file=sys.stderr)
        return 2

    signer = _signer_address(key)
    print(f"Signer address (from key): {signer}")
    print()

    candidates_raw = os.environ.get("POLYMARKET_FUNDER_CANDIDATES", "").split()
    # Always include the signer itself as a candidate (correct for EOA / type 0).
    candidates = []
    if signer:
        candidates.append(signer)
    for c in candidates_raw:
        if c and c not in candidates:
            candidates.append(c)
    if not candidates:
        print("ERROR: no funder candidates. Set POLYMARKET_FUNDER_CANDIDATES "
              "to space-separated addresses (signer + any proxy/deposit addrs).",
              file=sys.stderr)
        return 2

    print("Probing (signature_type, funder) combos for collateral balance.")
    print("READ-ONLY — no orders are placed.\n")
    print(f"{'sig_type':>8}  {'funder':<44}  balance / note")
    print("-" * 80)

    best = None
    for sig_type in (0, 1, 2):
        for funder in candidates:
            bal, note = _read_balance(key, funder, sig_type)
            shown = f"${bal:.2f}" if bal is not None else f"— ({note})"
            flag = ""
            if bal is not None and bal > 0:
                flag = "  <== HAS BALANCE"
                if best is None or bal > best[2]:
                    best = (sig_type, funder, bal)
            print(f"{sig_type:>8}  {funder:<44}  {shown}{flag}")

    print()
    if best:
        st, fn, bal = best
        print(f"CORRECT COMBO: POLYMARKET_SIGNATURE_TYPE={st}  "
              f"POLYMARKET_FUNDER={fn}  (balance ${bal:.2f})")
        print("Set those two env vars on the routine. The funder!=signer guard "
              "in PolymarketExecutor will confirm consistency at startup.")
    else:
        print("No combo reported a positive balance. Either the candidate "
              "funder addresses are wrong (get the Polygon deposit address from "
              "the Polymarket UI's deposit-on-Polygon flow), or the account "
              "holds no USDC on Polygon yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
