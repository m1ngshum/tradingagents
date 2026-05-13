"""Build the daily stock analysis watchlist, biased toward earnings catalysts.

The bot's bull/bear pipeline performs best when there's a hard near-term
catalyst (earnings, FDA, regulatory).  Mega-caps without imminent catalysts
tend to return HOLD at low confidence.  This script:

    1. Always includes the CORE_WATCHLIST (mega-caps for context).
    2. Adds any name from CATALYST_CANDIDATES with earnings in the next
       `--window-days` trading days.
    3. Caps total size at `--max-tickers` to keep daily runs bounded.

Output: space-separated tickers, one line, to stdout.  Intended as a shell
substitution in `run_stocks_daily.sh`.

Usage:
    .venv/bin/python scripts/build_stock_watchlist.py [--window-days 10] \\
        [--max-tickers 20] [--no-core] [--verbose]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tradingagents.dataflows.earnings_calendar import find_upcoming_earnings

# Always-include core: well-known large caps the bot has news context for.
# Kept small so most slots are reserved for catalyst-driven picks.
CORE_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
]

# Candidate pool: large/mid caps across sectors with regular earnings cycles
# and broad news coverage.  Refreshed manually as IPOs/delistings change.
CATALYST_CANDIDATES = [
    # Tech (beyond core)
    "META", "TSLA", "AMD", "INTC", "CSCO", "ORCL", "ADBE", "CRM", "NOW",
    "AVGO", "QCOM", "TXN", "MU", "PYPL", "INTU", "AMAT", "LRCX", "KLAC",
    "SNOW", "PLTR", "DDOG", "SHOP", "UBER", "NET",

    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "V", "MA", "BLK", "SCHW",
    "COF",

    # Healthcare
    "UNH", "JNJ", "PFE", "LLY", "MRK", "ABBV", "TMO", "ABT", "DHR", "BMY",
    "GILD", "VRTX", "REGN",

    # Consumer discretionary
    "HD", "MCD", "NKE", "SBUX", "LOW", "TGT", "BKNG", "DIS", "ABNB",

    # Consumer staples
    "PG", "KO", "PEP", "COST", "WMT", "CL", "EL",

    # Industrials
    "BA", "CAT", "GE", "HON", "UPS", "FDX", "LMT", "RTX", "DE",

    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB",

    # Communications
    "T", "VZ", "TMUS", "NFLX", "CMCSA",

    # Materials / Utilities / Real estate
    "LIN", "APD", "SHW", "NEE", "DUK", "SO", "PLD", "AMT", "EQIX",

    # China ADRs with active US-tradable earnings catalysts
    "BABA", "JD", "BIDU", "PDD",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days", type=int, default=10,
        help="Include candidates with earnings within N days (default 10)",
    )
    parser.add_argument(
        "--max-tickers", type=int, default=20,
        help="Cap total watchlist size (default 20)",
    )
    parser.add_argument(
        "--no-core", action="store_true",
        help="Skip the always-include core watchlist",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print earnings dates to stderr",
    )
    args = parser.parse_args()

    if args.verbose:
        print(
            f"Scanning {len(CATALYST_CANDIDATES)} candidates for earnings "
            f"within {args.window_days} days...",
            file=sys.stderr,
        )

    earnings = find_upcoming_earnings(
        CATALYST_CANDIDATES,
        window_days=args.window_days,
        min_days=0,
    )

    if args.verbose:
        print(f"Found {len(earnings)} candidates with upcoming earnings:", file=sys.stderr)
        for e in earnings:
            print(f"  {e.ticker:6s}  in {e.days_out:2d} days ({e.earnings_date})",
                  file=sys.stderr)

    core = [] if args.no_core else list(CORE_WATCHLIST)
    catalyst = [e.ticker for e in earnings]

    # Combine, dedupe (preserve core ordering), cap.
    seen: set[str] = set()
    final: list[str] = []
    for t in core + catalyst:
        if t not in seen:
            seen.add(t)
            final.append(t)
        if len(final) >= args.max_tickers:
            break

    print(" ".join(final))

    if args.verbose:
        print(
            f"Final watchlist ({len(final)}): {' '.join(final)}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
