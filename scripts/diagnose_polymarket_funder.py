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

Nothing here submits an order and no funds move. Caveat: deriving L2 creds
(create_or_derive_api_creds) will CREATE API credentials on Polymarket's
backend the first time it runs for a key (idempotent after). That's an
account-init side effect, not a trade — fine for an account you own.
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
        # NOTE (PR #39 review): create_or_derive_api_creds() is NOT purely
        # read-only — on a first-time key it CREATES L2 API credentials on
        # Polymarket's backend (idempotent thereafter). It places no ORDERS and
        # moves no funds, but it is an account-init side effect. Acceptable for
        # an account you own; documented so it's not a surprise.
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

    funded = []  # (sig_type, funder, balance) for every combo with balance > 0
    for sig_type in (0, 1, 2):
        for funder in candidates:
            bal, note = _read_balance(key, funder, sig_type)
            shown = f"${bal:.2f}" if bal is not None else f"— ({note})"
            flag = ""
            if bal is not None and bal > 0:
                flag = "  <== HAS BALANCE"
                funded.append((sig_type, funder, bal))
            print(f"{sig_type:>8}  {funder:<44}  {shown}{flag}")

    print()
    if len(funded) == 1:
        st, fn, bal = funded[0]
        print(f"CORRECT COMBO: POLYMARKET_SIGNATURE_TYPE={st}  "
              f"POLYMARKET_FUNDER={fn}  (balance ${bal:.2f})")
        print("Set those two env vars on the routine. The funder!=signer guard "
              "in PolymarketExecutor will confirm consistency at startup.")
    elif len(funded) > 1:
        # PR #39 review: do NOT silently pick the highest balance. Multiple
        # funded combos means the human must disambiguate against the UI.
        print("WARNING: MORE THAN ONE combo reports a balance. Do NOT assume the "
              "largest is correct — pick the one whose funder address matches what "
              "Polymarket's Deposit-on-Polygon flow shows for YOUR account:")
        for st, fn, bal in funded:
            print(f"  - SIGNATURE_TYPE={st}  FUNDER={fn}  (${bal:.2f})")
    else:
        print("No combo reported a positive balance. Either the candidate "
              "funder addresses are wrong (get the Polygon deposit address from "
              "the Polymarket UI's deposit-on-Polygon flow), or the account "
              "holds no USDC on Polygon yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
