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

    # Kill switch — same out-of-band stop as the polymarket routine. Any truthy
    # value forces --no-fill regardless of Alpaca config.
    kill_switch = os.environ.get("TRADINGAGENTS_AUTOTRADE_KILL_SWITCH", "").strip()
    if executor is not None and kill_switch and kill_switch.lower() not in ("0", "false", "no", ""):
        print(
            f"AUTOTRADE KILL SWITCH ACTIVE ({kill_switch!r}) — skipping order submission.",
            file=sys.stderr,
        )
        executor = None

    ta = TradingAgentsGraph(config=config)
    now = datetime.now(timezone.utc)
    log_path = _decision_log_path(now)
    order_log_path = _order_log_path(now)

    from tradingagents.exchange.cost_tracker import CostTracker
    from tradingagents.exchange.loss_breaker import LossBreaker
    from tradingagents.exchange.notifier import Notifier
    from tradingagents.exchange.reconciliation import (
        reconcile, count_open_positions_from_fills,
    )
    cost_tracker = CostTracker(decision_log_path=log_path, budget_usd=args.daily_budget_usd)
    notifier = Notifier()
    # Per-instrument loss breaker state (independent of polymarket's).
    loss_breaker = LossBreaker(STOCKS_OUTPUT_DIR / "loss_breaker.json")

    # Pre-fire safety gates (only when about to place real/paper orders).
    if executor is not None:
        # 1) Loss breaker — daily realized loss / drawdown. Fails closed.
        if loss_breaker.is_tripped():
            st = loss_breaker.status()
            print(
                f"LOSS BREAKER TRIPPED {st['reasons']} — "
                f"daily_pnl=${st['daily_realized_pnl']} drawdown=${st['drawdown']}. "
                f"Skipping order submission this fire.",
                file=sys.stderr,
            )
            notifier.breaker_tripped(st["reasons"], {
                "daily_realized_pnl": st["daily_realized_pnl"],
                "drawdown": st["drawdown"],
            })
            executor = None

    if executor is not None:
        # 2) Reconciliation — Alpaca gives REAL equity + position count, so this
        # is a genuine balance AND position-drift check (better than polymarket).
        prior_orders = []
        op = order_log_path
        if op.exists():
            for line in op.read_text().splitlines():
                if line.strip():
                    try:
                        prior_orders.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        expected_positions = count_open_positions_from_fills(
            [o.get("order", {}) | {"market_id": o.get("ticker")} for o in prior_orders]
        )
        equity = executor.get_account_equity()
        # Seed the breaker's equity tracker so drawdown is measured from real
        # account equity going forward.
        if equity is not None:
            loss_breaker.set_equity(equity)
        recon = reconcile(
            expected_open_positions=expected_positions,
            actual_open_positions=executor.count_open_positions(),
            intended_capital_usd=args.capital,
            actual_balance_usd=equity,
        )
        if not recon.ok:
            print(
                f"RECONCILIATION HALT {list(recon.halt_reasons)} — {recon.detail}. "
                f"Skipping order submission; reconcile manually.",
                file=sys.stderr,
            )
            notifier.reconciliation_halt(list(recon.halt_reasons), recon.detail)
            executor = None

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
            status = result["status"]
            if status == "SUBMITTED":
                # Real (or paper) order placed — alert + record a provisional
                # 0 realized P&L so the breaker's equity tracker stays current.
                notifier.order_filled(
                    ticker, result["side"],
                    result.get("notional_usd", 0.0), result.get("order_id", "?"),
                )
                loss_breaker.record_realized_pnl(0.0)
            elif status == "ERROR":
                notifier.send(
                    f"Alpaca order ERROR on {ticker}: {result.get('reason', '')}",
                    severity="warn",
                )
            if not args.quiet:
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
