#!/usr/bin/env python3
"""Mark-to-market open Alpaca paper stock positions.

Usage:
    python scripts/score_stocks.py [--date YYYY-MM-DD]

Reads from:
    ~/.tradingagents/stocks/paper-orders-YYYY-MM-DD.jsonl
Fetches current prices via yfinance and prints P&L summary.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

STOCKS_OUTPUT_DIR = Path.home() / ".tradingagents" / "stocks"


def _load_orders(date_str: str) -> list[dict]:
    path = STOCKS_OUTPUT_DIR / f"paper-orders-{date_str}.jsonl"
    if not path.exists():
        return []
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return [r for r in records if r.get("order", {}).get("status") == "SUBMITTED"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="Date of the order log to score (default: today UTC)",
    )
    args = parser.parse_args()

    import yfinance as yf

    orders = _load_orders(args.date)
    if not orders:
        print(f"No submitted orders found for {args.date}")
        print(f"  (looked in {STOCKS_OUTPUT_DIR / f'paper-orders-{args.date}.jsonl'})")
        return 0

    now = datetime.now(timezone.utc)
    total_invested = 0.0
    total_mtm_pnl = 0.0
    rows = []

    for rec in orders:
        ticker = rec.get("ticker", "?")
        entry_price = float(rec.get("price_at_analysis", 0))
        notional = float(rec.get("order", {}).get("notional_usd", 0))
        side = rec.get("order", {}).get("side", "buy")
        horizon_days = int(rec.get("horizon_days", 5))
        ts_str = rec.get("ts", "")

        try:
            current_price = float(yf.Ticker(ticker).fast_info.last_price)
        except Exception:
            current_price = entry_price

        if entry_price <= 0:
            continue

        shares = notional / entry_price
        if side == "buy":
            pnl = (current_price - entry_price) * shares
        else:
            pnl = (entry_price - current_price) * shares

        try:
            entry_dt = datetime.fromisoformat(ts_str)
            days_held = (now - entry_dt).days
            status = "RESOLVED" if days_held >= horizon_days else "OPEN"
        except Exception:
            days_held = 0
            status = "OPEN"

        total_invested += notional
        total_mtm_pnl += pnl
        rows.append({
            "ticker": ticker,
            "side": side,
            "notional": notional,
            "entry": entry_price,
            "current": current_price,
            "pnl": pnl,
            "days_held": days_held,
            "horizon": horizon_days,
            "status": status,
        })

    print(f"\n{'='*72}")
    print(f"STOCK PORTFOLIO SUMMARY  ({len(rows)} positions, ${total_invested:,.2f} invested)")
    print(f"{'='*72}")
    if total_invested:
        print(
            f"  MTM P&L:   ${total_mtm_pnl:+,.2f}  "
            f"({total_mtm_pnl / total_invested * 100:+.1f}%)"
        )
    else:
        print("  No positions.")
    print(f"\nPER-POSITION DETAIL")
    print(f"{'─'*72}")
    print(
        f"{'Status':<10} {'Side':<6} {'$In':>8} {'P&L':>8} "
        f"{'Entry':>8} {'Now':>8} {'Days':>5}  Ticker"
    )
    for r in rows:
        print(
            f"{r['status']:<10} {r['side']:<6} ${r['notional']:>7,.0f} "
            f"${r['pnl']:>+7,.2f} ${r['entry']:>7.2f} ${r['current']:>7.2f} "
            f"{r['days_held']:>4}/{r['horizon']}  {r['ticker']}"
        )
    print(f"{'─'*72}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
