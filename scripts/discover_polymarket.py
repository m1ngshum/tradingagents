"""Discover high-edge Polymarket trading opportunities.

Two-stage funnel:
    1. Cheap filter: scan many markets via gamma-api, apply pattern-based
       category filter to skip random walks, sports, weather, etc.  Score the
       remaining markets for tradeable-edge candidates.
    2. Expensive analysis: run the full bull/bear/trader LLM pipeline on the
       top-N filtered candidates and rank by confidence × edge.

Output: JSON Lines of analysed decisions, ordered by Kelly edge desc.

Usage:
    python scripts/discover_polymarket.py [--fetch 500] [--analyse 10]
        [--model anthropic/claude-sonnet-4-6]
        [--min-liquidity 5000] [--include-bad-fit]

Writes one JSON line per analysed candidate to:
    ~/.tradingagents/polymarket/discoveries-YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing tradingagents modules so OPENROUTER_API_KEY is set.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from tradingagents.dataflows.market_classifier import (
    classify_market,
    score_market_for_discovery,
)
from tradingagents.dataflows.polymarket_data import GammaAPIError, get_open_markets
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.exchange.io_utils import POLYMARKET_OUTPUT_DIR, append_jsonl
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)


def _discovery_log_path(now: datetime) -> Path:
    return POLYMARKET_OUTPUT_DIR / f"discoveries-{now.strftime('%Y-%m-%d')}.jsonl"


def _kelly_edge(yes_price: float, direction: str, confidence: float) -> float:
    """Compute Kelly edge = (confidence - buy_price) / (1 - buy_price).

    Positive edge means the bot's confidence exceeds the market's implied
    probability — the larger the edge, the more attractive the bet.
    """
    if direction == "BUY_YES":
        buy_price = yes_price
    elif direction == "BUY_NO":
        buy_price = 1.0 - yes_price
    else:
        return 0.0

    if buy_price >= 0.99 or buy_price <= 0.01:
        return 0.0

    edge = (confidence - buy_price) / (1.0 - buy_price)
    return max(edge, 0.0)


def _score_pre_analysis(market: dict, today: str) -> float:
    """Score a market for discovery ranking BEFORE running LLM analysis.

    Markets closer to 0.50 have more potential edge; markets ending sooner
    resolve faster (good for fast accuracy feedback). Score in [0, 1].
    """
    yes_price = float(market.get("yes_price", 0.5) or 0.5)
    midpoint_distance = 1.0 - 2 * abs(yes_price - 0.5)  # closest to 0.5 → 1.0

    end = (market.get("end_date") or market.get("endDate") or "")[:10]
    days_to_resolve = 365
    if end and end > today:
        try:
            end_dt = datetime.fromisoformat(end)
            today_dt = datetime.fromisoformat(today)
            days_to_resolve = max((end_dt - today_dt).days, 1)
        except ValueError:
            pass

    # Liquidity matters but caps out: $10k is fine, $100k isn't 10x better.
    liq = float(market.get("liquidity", 0) or 0)
    liq_score = min(liq / 10_000.0, 5.0) / 5.0

    # Resolves within 60 days = max time score; longer = less attractive
    # (need accuracy feedback before too long).
    time_score = max(0.0, 1.0 - days_to_resolve / 365.0)

    return 0.5 * midpoint_distance + 0.3 * liq_score + 0.2 * time_score


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch", type=int, default=500,
        help="Number of markets to fetch from gamma-api (default 500)",
    )
    parser.add_argument(
        "--analyse", type=int, default=10,
        help="Number of top filtered candidates to run through LLM (default 10)",
    )
    parser.add_argument(
        "--model", default="anthropic/claude-sonnet-4-6",
        help="OpenRouter model id",
    )
    parser.add_argument(
        "--min-liquidity", type=float, default=5000.0,
        help="Liquidity floor in USDC (default 5000)",
    )
    parser.add_argument(
        "--days-until-close", type=int, default=120,
        help="Only include markets closing within N days (default 120)",
    )
    parser.add_argument(
        "--include-bad-fit", action="store_true",
        help="Don't skip pattern-classified bad-fit markets",
    )
    parser.add_argument(
        "--no-analysis", action="store_true",
        help="Stop after filtering: print candidates, don't run LLM",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("EXA_API_KEY") and not args.no_analysis:
        print("ERROR: EXA_API_KEY not set", file=sys.stderr)
        return 2
    if not os.environ.get("OPENROUTER_API_KEY") and not args.no_analysis:
        print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
        return 2

    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()
    end_max_str = (today + timedelta(days=args.days_until_close)).isoformat()

    if not args.quiet:
        print(f"=== Discovery: fetching {args.fetch} markets ===")

    try:
        markets = get_open_markets(
            limit=args.fetch,
            order="liquidity",
            ascending=False,
        )
    except GammaAPIError as e:
        print(f"ERROR: Gamma fetch failed: {e}", file=sys.stderr)
        return 3

    # Filter date window client-side (gamma's date filter mixes poorly with
    # liquidity sort — we want the most liquid markets *within* our date range).
    def _in_window(m: dict) -> bool:
        end = (m.get("end_date") or m.get("endDate") or "")[:10]
        if not end:
            return False
        return tomorrow_str <= end <= end_max_str

    markets = [m for m in markets if _in_window(m)]

    if not args.quiet:
        print(f"  fetched: {len(markets)} markets in window "
              f"({tomorrow_str} - {end_max_str})")

    # Stage 1: pattern filter + liquidity + price gates
    candidates = []
    skipped = {"liquidity": 0, "extreme_price": 0, "bad_category": 0}
    for m in markets:
        q = m.get("question") or ""
        yes_price = float(m.get("yes_price", 0.5) or 0.5)
        liq = float(m.get("liquidity", 0) or 0)

        ok, reason = score_market_for_discovery(
            q, yes_price, liq,
            min_liquidity=args.min_liquidity,
            skip_bad_fit=not args.include_bad_fit,
        )
        if not ok:
            if "liquidity" in reason:
                skipped["liquidity"] += 1
            elif "extreme" in reason:
                skipped["extreme_price"] += 1
            else:
                skipped["bad_category"] += 1
            continue

        cls = classify_market(q)
        score = _score_pre_analysis(m, today_str)
        candidates.append((score, m, cls))

    candidates.sort(key=lambda x: x[0], reverse=True)

    if not args.quiet:
        print(f"  filtered: {len(candidates)} candidates pass discovery filter")
        print(f"    skipped: liquidity={skipped['liquidity']}, "
              f"extreme_price={skipped['extreme_price']}, "
              f"bad_category={skipped['bad_category']}")
        print()
        print(f"=== Top {min(args.analyse, len(candidates))} candidates (pre-analysis ranking) ===")
        for score, m, cls in candidates[:args.analyse]:
            print(f"  score={score:.3f} liq=${float(m.get('liquidity', 0)):,.0f} "
                  f"yes={float(m.get('yes_price', 0.5)):.3f} "
                  f"end={(m.get('end_date') or '')[:10]} "
                  f"[{cls.category}] \"{m.get('question', '')[:55]}\"")
        print()

    if args.no_analysis:
        return 0

    if not candidates:
        print("No candidates to analyse — try lowering --min-liquidity or "
              "widening --days-until-close", file=sys.stderr)
        return 4

    # Stage 2: run the full LLM pipeline on top candidates
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "openrouter"
    config["quick_think_llm"] = args.model
    config["deep_think_llm"] = args.model
    ta = TradingAgentsGraph(config=config)

    now = datetime.now(timezone.utc)
    log_path = _discovery_log_path(now)

    if not args.quiet:
        print(f"=== Running bull/bear/trader analysis ===")
        print(f"  Output -> {log_path}")
        print()

    analysed = []
    for i, (pre_score, m, cls) in enumerate(candidates[:args.analyse], start=1):
        q = m.get("question") or ""
        yes_price = float(m.get("yes_price", 0.5) or 0.5)
        if not args.quiet:
            print(f"--- [{i}/{min(args.analyse, len(candidates))}] {q[:75]}")
            print(f"    [{cls.category}/{cls.bot_fit}] yes={yes_price:.3f} "
                  f"liq=${float(m.get('liquidity', 0)):,.0f}")

        def _on_step(label: str) -> None:
            if not args.quiet:
                print(f"    .. {label}", flush=True)

        try:
            _, decision = ta.propagate_market(
                market_id=m["id"], question=q,
                yes_price=yes_price,
                resolution_date=m.get("end_date") or "",
                on_step=_on_step,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("propagate_market failed for %s", m["id"])
            print(f"    FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            continue

        edge = _kelly_edge(yes_price, decision.direction.value, decision.confidence)
        payload = {
            "ts": now.isoformat(),
            "model": args.model,
            "pre_score": round(pre_score, 4),
            "category": cls.category,
            "bot_fit": cls.bot_fit,
            "kelly_edge": round(edge, 4),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "end_date": m.get("end_date") or "",
            **decision.model_dump(mode="json"),
        }
        append_jsonl(log_path, payload)
        analysed.append((edge, decision, m, cls))

        if not args.quiet:
            print(f"    -> {decision.direction.value} conf={decision.confidence:.2f} "
                  f"kelly_edge={edge:.3f}")

    # Final ranked output
    analysed.sort(key=lambda x: x[0], reverse=True)
    if not args.quiet:
        print()
        print(f"=== Ranked opportunities ({len(analysed)} analysed) ===")
        actionable = [a for a in analysed if a[0] > 0 and a[1].direction.value != "HOLD"]
        if not actionable:
            print("  No actionable BUY signals (all HOLD or zero-edge).")
        for edge, decision, m, cls in actionable[:10]:
            print(f"  edge={edge:.3f} conf={decision.confidence:.2f} "
                  f"{decision.direction.value:7s} [{cls.category}] "
                  f"\"{m.get('question', '')[:55]}\"")

    return 0


if __name__ == "__main__":
    sys.exit(main())
