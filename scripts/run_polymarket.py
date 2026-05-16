"""Run the Polymarket research engine over N live open markets.

Usage:
    python scripts/run_polymarket.py [--limit 5] [--model openai/gpt-4o-mini]

Writes one JSON line per decision to:
    ~/.tradingagents/polymarket/decisions-YYYY-MM-DD.jsonl

This is the file the future backtesting harness (TODOS.md item 1) will read.
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

# Resolve paths before importing tradingagents so `.env` is loaded first.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from collections import Counter

from tradingagents.dataflows.market_classifier import classify_market
from tradingagents.dataflows.polymarket_data import (
    CLOBAPIError,
    GammaAPIError,
    get_open_markets,
    get_order_book,
    resolve_cluster_id,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.exchange.cost_tracker import CostTracker
from tradingagents.exchange.io_utils import POLYMARKET_OUTPUT_DIR, append_jsonl
from tradingagents.exchange.paper_fill import is_economic_when_correct, simulate_fill
from tradingagents.exchange.polymarket_executor import (
    PolymarketExecutionDisabled,
    PolymarketExecutor,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)


def _decision_log_path(now: datetime) -> Path:
    return POLYMARKET_OUTPUT_DIR / f"decisions-{now.strftime('%Y-%m-%d')}.jsonl"


def _fill_log_path(now: datetime) -> Path:
    return POLYMARKET_OUTPUT_DIR / f"paper-fills-{now.strftime('%Y-%m-%d')}.jsonl"


def _cluster_counts_today(fill_log_path: Path) -> Counter:
    """Count BUY positions per cluster_id in today's fills.

    SKIPPED rows and rows without cluster_id are excluded. Tolerates corrupted
    JSONL lines via the shared `load_fills_jsonl` helper.
    """
    from tradingagents.exchange.scoring import load_fills_jsonl
    if not fill_log_path.exists():
        return Counter()
    fills = load_fills_jsonl(
        fill_log_path.parent,
        date=fill_log_path.stem.removeprefix("paper-fills-"),
    )
    counts: Counter = Counter()
    for f in fills:
        # Only count actually-placed BUY positions, not SKIPPED rows.
        if f.get("status") in ("SKIPPED", "ERROR"):
            continue
        cid = f.get("cluster_id")
        if cid:
            counts[cid] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=3, help="Number of markets to analyse")
    parser.add_argument(
        "--model",
        default="openai/gpt-4o-mini",
        help="OpenRouter model id (default: openai/gpt-4o-mini)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=100.0,
        help="USDC budget per non-HOLD decision for paper fill (default: 100.0)",
    )
    parser.add_argument(
        "--no-fill",
        action="store_true",
        help="Skip the paper-fill step; persist decisions only",
    )
    parser.add_argument(
        "--days-until-close",
        type=int,
        default=None,
        help=(
            "Filter to markets closing within N days. Sorts by closest end date "
            "first. Useful for fast feedback: markets closing soon resolve fast."
        ),
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=0.0,
        help=(
            "Skip markets with liquidity below this USDC threshold. "
            "Recommended >= 5000 to avoid lottery-ticket markets the bot "
            "tends to misprice. Default: 0 (no filter)."
        ),
    )
    parser.add_argument(
        "--exclude-categories",
        default="election",
        help=(
            "Comma-separated market_classifier categories to drop after fetch. "
            "Default 'election' (live accuracy on electoral markets is unproven; "
            "see TODOS.md). Pass empty string to disable. Categories: election, "
            "tournament_participation, concrete_event, geopolitical, regulatory, "
            "appointment_outcome, talent_show_winner, individual_sport_game, "
            "sport_team_game, esports_game, crypto_price, stock_price, "
            "commodity_price, weather, celebrity_move, short_term_price, other."
        ),
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_CONFIG["polymarket_min_confidence"],
        help=(
            "Skip filling BUY_YES/BUY_NO decisions below this confidence. The "
            "decision row is logged to decisions-*.jsonl, and a SKIPPED row is "
            "logged to paper-fills-*.jsonl with reason 'below_min_confidence'. "
            f"Default {DEFAULT_CONFIG['polymarket_min_confidence']} (calibration-derived; "
            "see docs/PLAN-research-capture-and-cluster-cap.md). Pass 0.0 to disable."
        ),
    )
    parser.add_argument(
        "--daily-budget-usd",
        type=float,
        default=float(os.environ.get("TRADINGAGENTS_POLYMARKET_DAILY_BUDGET_USD", "15.0")),
        help=(
            "Hard ceiling on today's LLM spend for this routine. When today's "
            "decisions-*.jsonl sums to >= this value, further markets are "
            "SKIPPED with reason 'daily_budget_exceeded' and the routine "
            "completes gracefully (state still pushed). Default $15 "
            "(env: TRADINGAGENTS_POLYMARKET_DAILY_BUDGET_USD). Pass 0 to disable."
        ),
    )
    parser.add_argument(
        "--max-per-cluster",
        type=int,
        default=1,
        help=(
            "Maximum BUY positions to take in any single cluster (Polymarket "
            "negRisk sibling group OR shared event). Default 1 (most "
            "conservative — addresses the 2026-05-14 Trump-Xi 7-of-7 trap). "
            "Pass 0 to disable. Skipped trades are logged with reason "
            "'cluster_full' or 'cluster_unknown' to paper-fills-*.jsonl."
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="Print only the JSONL path")
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Submit real orders via Polymarket CLOB instead of paper-filling. "
            "Requires POLYMARKET_PRIVATE_KEY, POLYMARKET_KEY, POLYMARKET_SECRET, "
            "POLYMARKET_PASSPHRASE, POLYMARKET_FUNDER in environment."
        ),
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=1000.0,
        help="Total capital in USDC for Kelly sizing when --live is set (default: 1000.0)",
    )
    args = parser.parse_args()

    # Validate --min-confidence. `float("nan")` parses cleanly but breaks the
    # gate silently (nan comparisons always return False), and values outside
    # [0, 1] silently block-all or never-fire. Both are unsafe in --live mode.
    import math
    if (
        not math.isfinite(args.min_confidence)
        or not (0.0 <= args.min_confidence <= 1.0)
    ):
        parser.error(
            f"--min-confidence must be a finite number in [0.0, 1.0], "
            f"got {args.min_confidence}"
        )

    if not os.environ.get("EXA_API_KEY"):
        print("ERROR: EXA_API_KEY not set in environment or .env", file=sys.stderr)
        return 2
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set in environment or .env", file=sys.stderr)
        return 2

    live_executor: PolymarketExecutor | None = None
    if args.live:
        try:
            live_executor = PolymarketExecutor()
            print(f"LIVE MODE — orders will be submitted to Polymarket (capital=${args.capital:,.0f})")
        except PolymarketExecutionDisabled as e:
            print(f"ERROR: cannot enable --live: {e}", file=sys.stderr)
            return 2

    market_kwargs: dict = {"limit": args.limit}
    if args.days_until_close is not None:
        today = datetime.now(timezone.utc).date()
        tomorrow = today + timedelta(days=1)
        market_kwargs["order"] = "endDate"
        market_kwargs["ascending"] = True
        market_kwargs["end_date_min"] = tomorrow.isoformat()
        market_kwargs["end_date_max"] = (
            today + timedelta(days=args.days_until_close)
        ).isoformat()

    excluded_categories = {
        c.strip() for c in args.exclude_categories.split(",") if c.strip()
    }

    # Over-fetch when filtering client-side so we still end up with --limit
    # markets after low-liquidity / excluded-category ones are dropped.
    # When --days-until-close sorts by end date, high-liquidity markets are
    # spread across the entire window (not clustered at the near end), so we
    # need a much larger fetch.
    fetch_limit = args.limit
    if args.min_liquidity > 0 or excluded_categories:
        if args.days_until_close is not None:
            fetch_limit = max(args.limit * 20, 600)
        else:
            fetch_limit = max(args.limit * 5, 25)
    market_kwargs["limit"] = fetch_limit

    try:
        markets = get_open_markets(**market_kwargs)
    except GammaAPIError as e:
        print(f"ERROR: Gamma fetch failed: {e}", file=sys.stderr)
        return 3

    if args.min_liquidity > 0:
        before = len(markets)
        markets = [m for m in markets if m.get("liquidity", 0) >= args.min_liquidity]
        if not args.quiet:
            print(
                f"Filtered to {len(markets)} of {before} markets "
                f"(liquidity >= ${args.min_liquidity:,.0f})"
            )

    if excluded_categories:
        before = len(markets)
        kept = []
        for m in markets:
            cat = classify_market(m.get("question") or "").category
            if cat not in excluded_categories:
                kept.append(m)
        markets = kept
        if not args.quiet:
            print(
                f"Filtered to {len(markets)} of {before} markets "
                f"(excluded categories: {sorted(excluded_categories)})"
            )

    markets = markets[: args.limit]

    if not markets:
        print(
            "ERROR: zero markets matched filters (try lowering --min-liquidity "
            "or widening --days-until-close)",
            file=sys.stderr,
        )
        return 4

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "openrouter"
    config["quick_think_llm"] = args.model
    config["deep_think_llm"] = args.model

    ta = TradingAgentsGraph(config=config)
    now = datetime.now(timezone.utc)
    log_path = _decision_log_path(now)
    fill_log_path = _fill_log_path(now)
    cost_tracker = CostTracker(decision_log_path=log_path, budget_usd=args.daily_budget_usd)

    if not args.quiet:
        print(f"=== Analysing {len(markets)} markets with model={args.model} ===")
        print(f"  Decisions  -> {log_path}")
        if not args.no_fill:
            if live_executor is not None:
                print(f"  Live orders -> {fill_log_path}  (capital=${args.capital:,.0f})")
            else:
                print(f"  Paper fills -> {fill_log_path}  (budget=${args.budget:.0f}/decision)")
        print()

    for i, m in enumerate(markets, start=1):
        question = m.get("question") or "(no question)"
        if not args.quiet:
            print(f"--- [{i}/{len(markets)}] {question[:80]}")
            print(f"    yes_price={m['yes_price']:.3f}  end={m.get('end_date')}")

        # Pre-flight budget check. Must fire BEFORE the LLM call to actually
        # save money. Logs a decision-row stub so the audit trail shows what
        # we skipped and why.
        if cost_tracker.is_exhausted():
            status = cost_tracker.status()
            skip_payload = {
                "ts": now.isoformat(),
                "model": args.model,
                "market_id": m["id"],
                "question": question,
                "direction": "SKIPPED",
                "reason": "daily_budget_exceeded",
                "spent_today_usd": status["spent_today_usd"],
                "budget_usd": status["budget_usd"],
            }
            append_jsonl(log_path, skip_payload)
            if not args.quiet:
                print(
                    f"    SKIP — daily_budget_exceeded "
                    f"(spent ${status['spent_today_usd']:.2f} / ${status['budget_usd']:.2f})\n"
                )
            continue

        def _on_step(label: str) -> None:
            if not args.quiet:
                print(f"    .. {label}", flush=True)

        try:
            _, decision = ta.propagate_market(
                market_id=m["id"],
                question=question,
                yes_price=m["yes_price"],
                resolution_date=m.get("end_date") or "",
                on_step=_on_step,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("propagate_market failed for %s", m["id"])
            print(f"    FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            continue

        payload = {
            "ts": now.isoformat(),
            "model": args.model,
            **decision.model_dump(mode="json"),
        }
        append_jsonl(log_path, payload)

        if not args.quiet:
            print(f"    -> {decision.direction.value} (conf {decision.confidence:.2f})")
            print(f"       {decision.rationale[:200]}")

        # Fill: live order (--live) or paper simulation (default). Skip HOLD / --no-fill.
        if args.no_fill or decision.direction.value == "HOLD":
            if not args.quiet:
                print()
            continue

        # Confidence gate. Calibration: conf<0.85 wins ~33% on n=15;
        # conf>=0.9 wins 100% on n=2. The gate keeps the routine running but
        # suppresses low-conviction fills. Logs a SKIPPED row to the fill JSONL
        # so the audit trail is complete — the decision file shows what the
        # bot decided, the fill file shows what actually executed (or didn't).
        if decision.confidence < args.min_confidence:
            skip_payload = {
                "ts": now.isoformat(),
                "market_id": m["id"],
                "question": question,
                "direction": decision.direction.value,
                "confidence": decision.confidence,
                "yes_price_at_analysis": decision.yes_price_at_analysis,
                "status": "SKIPPED",
                "reason": "below_min_confidence",
                "min_confidence": args.min_confidence,
            }
            append_jsonl(fill_log_path, skip_payload)
            if not args.quiet:
                print(
                    f"    fill: SKIP — confidence {decision.confidence:.2f} "
                    f"< --min-confidence {args.min_confidence:.2f}\n"
                )
            continue

        # Cluster cap. Groups correlated markets via Polymarket's negRisk
        # sibling ID or shared event, with a synthetic base-slug fallback.
        # See PLAN-research-capture-and-cluster-cap.md F8 — default is fail-safe:
        # markets that can't be grouped are REFUSED, not allowed through.
        cluster_id = resolve_cluster_id(m) if args.max_per_cluster > 0 else None
        if args.max_per_cluster > 0:
            if cluster_id is None:
                skip_payload = {
                    "ts": now.isoformat(),
                    "market_id": m["id"],
                    "question": question,
                    "direction": decision.direction.value,
                    "status": "SKIPPED",
                    "reason": "cluster_unknown",
                    "slug": m.get("slug"),
                }
                append_jsonl(fill_log_path, skip_payload)
                if not args.quiet:
                    print(f"    fill: SKIP — cluster_unknown (cannot group market safely)\n")
                continue
            current_counts = _cluster_counts_today(fill_log_path)
            if current_counts.get(cluster_id, 0) >= args.max_per_cluster:
                skip_payload = {
                    "ts": now.isoformat(),
                    "market_id": m["id"],
                    "question": question,
                    "direction": decision.direction.value,
                    "cluster_id": cluster_id,
                    "status": "SKIPPED",
                    "reason": "cluster_full",
                    "max_per_cluster": args.max_per_cluster,
                }
                append_jsonl(fill_log_path, skip_payload)
                if not args.quiet:
                    print(
                        f"    fill: SKIP — cluster_full "
                        f"({current_counts[cluster_id]}/{args.max_per_cluster} in cluster {cluster_id})\n"
                    )
                continue

        token_id = m.get("yes_token_id") if decision.direction.value == "BUY_YES" else m.get("no_token_id")
        if not token_id:
            if not args.quiet:
                print(f"    fill: SKIP — no token id available\n")
            continue

        if live_executor is not None:
            # --- Live execution path ---
            result = live_executor.place_order(decision, token_id, args.capital)
            fill_payload = {
                "ts": now.isoformat(),
                "market_id": m["id"],
                "question": question,
                "direction": decision.direction.value,
                "yes_price_at_analysis": decision.yes_price_at_analysis,
                "capital_usd": args.capital,
                "cluster_id": cluster_id,
                "live": True,
                **result,
            }
            append_jsonl(fill_log_path, fill_payload)
            if not args.quiet:
                if result["status"] == "SUBMITTED":
                    print(
                        f"    order: SUBMITTED id={result['order_id']}  "
                        f"${result['usd']:.2f} {result['direction']}"
                    )
                else:
                    print(f"    order: {result['status']} — {result.get('reason', '')}")
                print()
            continue

        # --- Paper simulation path ---
        try:
            book = get_order_book(token_id)
        except CLOBAPIError as e:
            if not args.quiet:
                print(f"    fill: SKIP — CLOB error: {e}\n")
            continue

        fill = simulate_fill(book["asks"], budget_usd=args.budget)

        # Negative-EV guard: skip if position loses money even when correct.
        if fill["filled"] and not is_economic_when_correct(fill):
            net_if_win = (
                fill["contracts"] * 1.0
                - fill["filled_usd"]
                - fill["fee_estimate_if_win"]
            )
            if not args.quiet:
                print(
                    f"    fill: BLOCKED — NEGATIVE_EV "
                    f"(vwap {fill['vwap']:.3f}, would yield ${net_if_win:+.2f} even if correct)"
                )
                print()
            continue

        fill_payload = {
            "ts": now.isoformat(),
            "market_id": m["id"],
            "question": question,
            "direction": decision.direction.value,
            "yes_price_at_analysis": decision.yes_price_at_analysis,
            "budget_usd": args.budget,
            "cluster_id": cluster_id,
            **fill,
        }
        append_jsonl(fill_log_path, fill_payload)

        if not args.quiet:
            if fill["filled"]:
                print(
                    f"    fill: {fill['contracts']:.1f} contracts @ vwap {fill['vwap']:.3f}  "
                    f"slippage {fill['slippage_pp']:.2f}pp  fee_if_win ${fill['fee_estimate_if_win']:.2f}"
                )
            else:
                print(f"    fill: UNFILLED — empty/thin book")
            print()

    if args.quiet:
        print(str(log_path))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
