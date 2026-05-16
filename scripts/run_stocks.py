#!/usr/bin/env python3
"""Run the stock research pipeline over a watchlist and paper-trade decisions.

Usage:
    python scripts/run_stocks.py [--tickers AAPL MSFT TSLA] [--capital 10000]
                                  [--model anthropic/claude-sonnet-4-6]
                                  [--no-fill] [--min-confidence 0.55]

Writes one JSON line per decision to:
    ~/.tradingagents/stocks/decisions-YYYY-MM-DD.jsonl
Paper orders (when --no-fill is not set) are logged to:
    ~/.tradingagents/stocks/paper-orders-YYYY-MM-DD.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "XOM", "UNH",
]

STOCKS_OUTPUT_DIR = Path.home() / ".tradingagents" / "stocks"


def _decision_log_path(now: datetime) -> Path:
    return STOCKS_OUTPUT_DIR / f"decisions-{now.strftime('%Y-%m-%d')}.jsonl"


def _order_log_path(now: datetime) -> Path:
    return STOCKS_OUTPUT_DIR / f"paper-orders-{now.strftime('%Y-%m-%d')}.jsonl"


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", nargs="+", default=DEFAULT_TICKERS,
        help="Ticker symbols to analyse",
    )
    parser.add_argument(
        "--capital", type=float, default=10000.0,
        help="Total paper capital in USD (default: 10000)",
    )
    parser.add_argument(
        "--model", default="anthropic/claude-sonnet-4-6",
        help="OpenRouter model id",
    )
    parser.add_argument(
        "--no-fill", action="store_true",
        help="Skip order submission; log decisions only",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.55,
        help="Minimum confidence to place order (default: 0.55)",
    )
    parser.add_argument(
        "--daily-budget-usd",
        type=float,
        default=float(os.environ.get("TRADINGAGENTS_STOCKS_DAILY_BUDGET_USD", "5.0")),
        help=(
            "Hard ceiling on today's LLM spend for the stocks routine. When "
            "today's decisions-*.jsonl sums to >= this value, further tickers "
            "are SKIPPED with reason 'daily_budget_exceeded'. Default $5 "
            "(env: TRADINGAGENTS_STOCKS_DAILY_BUDGET_USD). Pass 0 to disable. "
            "Per-routine pool — does NOT share with the polymarket routine."
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    import math
    if not math.isfinite(args.min_confidence) or not (0.0 <= args.min_confidence <= 1.0):
        parser.error(
            f"--min-confidence must be a finite number in [0.0, 1.0], "
            f"got {args.min_confidence}"
        )
    if not math.isfinite(args.daily_budget_usd) or args.daily_budget_usd < 0:
        parser.error(
            f"--daily-budget-usd must be a finite non-negative number, "
            f"got {args.daily_budget_usd}"
        )

    if not os.environ.get("EXA_API_KEY"):
        print("ERROR: EXA_API_KEY not set", file=sys.stderr)
        return 2
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
        return 2

    import yfinance as yf
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.agents.schemas import StockDirection
    from tradingagents.exchange.alpaca_executor import AlpacaExecutor, AlpacaExecutionDisabled

    config = {
        **DEFAULT_CONFIG,
        "llm_provider": "openrouter",
        "deep_think_llm": args.model,
        "quick_think_llm": args.model,
    }

    executor: AlpacaExecutor | None = None
    if not args.no_fill:
        try:
            executor = AlpacaExecutor()
        except AlpacaExecutionDisabled as e:
            print(
                f"WARNING: Alpaca not configured ({e}). Running in --no-fill mode.",
                file=sys.stderr,
            )

    ta = TradingAgentsGraph(config=config)
    now = datetime.now(timezone.utc)
    log_path = _decision_log_path(now)
    order_log_path = _order_log_path(now)

    from tradingagents.exchange.cost_tracker import CostTracker
    cost_tracker = CostTracker(decision_log_path=log_path, budget_usd=args.daily_budget_usd)

    tickers = [t.upper() for t in args.tickers]

    if not args.quiet:
        print(f"=== Analysing {len(tickers)} tickers with model={args.model} ===")
        print(f"  Decisions  -> {log_path}")
        if executor:
            print(f"  Orders     -> {order_log_path}  (capital=${args.capital:,.0f})")

    for i, ticker in enumerate(tickers, 1):
        # Pre-flight budget check. Fires BEFORE the LLM call so we actually
        # save money when exhausted. See PLAN-research-capture-and-cluster-cap.md F11.
        if cost_tracker.is_exhausted():
            status = cost_tracker.status()
            skip_record = {
                "ts": now.isoformat(),
                "ticker": ticker.upper(),
                "direction": "SKIPPED",
                "reason": "daily_budget_exceeded",
                "spent_today_usd": status["spent_today_usd"],
                "budget_usd": status["budget_usd"],
            }
            _append_jsonl(log_path, skip_record)
            if not args.quiet:
                print(
                    f"  [{i}/{len(tickers)}] {ticker}: SKIP — daily_budget_exceeded "
                    f"(spent ${status['spent_today_usd']:.2f} / ${status['budget_usd']:.2f})"
                )
            continue

        try:
            info = yf.Ticker(ticker).fast_info
            price = float(info.last_price)
        except Exception as exc:
            print(f"  [{i}/{len(tickers)}] {ticker}: price fetch failed ({exc}), skipping")
            continue

        if not args.quiet:
            print(f"\n--- [{i}/{len(tickers)}] {ticker}")
            print(f"    price=${price:.2f}")

        def _step(label: str) -> None:
            if not args.quiet:
                print(f"    .. {label}")

        _, decision = ta.propagate_stock(ticker, price=price, on_step=_step)

        direction_str = decision.direction.value
        if not args.quiet:
            print(f"    -> {direction_str} (conf {decision.confidence:.2f})")
            print(f"       {decision.rationale[:120]}")

        record = {"ts": now.isoformat(), **decision.model_dump()}
        _append_jsonl(log_path, record)

        if executor and decision.direction != StockDirection.HOLD:
            result = executor.place_order(decision, capital_usd=args.capital)
            if not args.quiet:
                status = result["status"]
                if status == "SUBMITTED":
                    print(
                        f"    order: {status} id={result['order_id']} "
                        f"${result['notional_usd']:.2f} {result['side']}"
                    )
                else:
                    print(f"    order: {status} ({result.get('reason', '')})")
            _append_jsonl(order_log_path, {**record, "order": result})

    if not args.quiet:
        print(f"\nDone. Decisions written to {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
