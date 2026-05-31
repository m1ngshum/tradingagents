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
from tradingagents.exchange.gate import evaluate_fire, evaluate_market, kill_switch_active
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)


def _decision_log_path(now: datetime) -> Path:
    return POLYMARKET_OUTPUT_DIR / f"decisions-{now.strftime('%Y-%m-%d')}.jsonl"


def _fill_log_path(now: datetime) -> Path:
    return POLYMARKET_OUTPUT_DIR / f"paper-fills-{now.strftime('%Y-%m-%d')}.jsonl"


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
    parser.add_argument(
        "--max-orders-per-fire",
        type=int,
        default=5,
        help=(
            "Defense-in-depth cap on submitted live orders in a single fire. "
            "Default 5. Combined with --max-per-cluster=1, bounds daily exposure "
            "even if the model produces many high-confidence decisions in one day. "
            "Skipped trades log reason='max_orders_per_fire'. Only counts SUBMITTED "
            "orders, not SKIPPED/ERROR. Pass 0 to disable."
        ),
    )
    args = parser.parse_args()

    # Hard kill-switch. Setting this env var to anything truthy immediately
    # short-circuits live mode regardless of CLI flags. Provides an
    # out-of-band way to halt autotrade without editing the routine UI.
    kill_on = kill_switch_active()
    if args.live and kill_on:
        print(
            "AUTOTRADE KILL SWITCH ACTIVE (TRADINGAGENTS_AUTOTRADE_KILL_SWITCH) "
            "— --live disabled, falling back to paper mode.",
            file=sys.stderr,
        )
        args.live = False

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
    # Same finite-check on --daily-budget-usd. NaN would let unlimited spend
    # through silently; negative / inf are nonsensical.
    if (
        not math.isfinite(args.daily_budget_usd)
        or args.daily_budget_usd < 0
    ):
        parser.error(
            f"--daily-budget-usd must be a finite non-negative number, "
            f"got {args.daily_budget_usd}"
        )
    if not math.isfinite(args.exposure_budget) or args.exposure_budget < 0:
        parser.error(
            f"--exposure-budget must be a finite non-negative number, "
            f"got {args.exposure_budget}"
        )
    if not math.isfinite(args.min_edge) or not (0.0 <= args.min_edge <= 1.0):
        parser.error(
            f"--min-edge must be a finite number in [0.0, 1.0], got {args.min_edge}"
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

    # Realized-loss circuit breaker. Checked once before the market loop in
    # live mode: if today's losses or total drawdown breached a limit, refuse
    # to trade at all this fire. Fails CLOSED on corrupt state. Per-instrument
    # state file so polymarket and stocks breakers are independent.
    from tradingagents.exchange.loss_breaker import LossBreaker
    from tradingagents.exchange.notifier import Notifier
    notifier = Notifier()
    loss_breaker = LossBreaker(POLYMARKET_OUTPUT_DIR / "loss_breaker.json")

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

    # Seed in-process cluster counts ONCE from today's existing fills, then
    # increment in lockstep with new placed fills. Avoids per-market disk
    # re-scan inside the loop. See PR #18 review (efficiency H1).
    from tradingagents.exchange.scoring import count_fills_by_cluster
    cluster_counts = Counter(
        count_fills_by_cluster(POLYMARKET_OUTPUT_DIR, date=now.strftime("%Y-%m-%d"))
    )

    # Defense-in-depth: count SUBMITTED live orders in this fire so we can
    # halt at --max-orders-per-fire even if many markets clear all gates.
    live_orders_submitted = 0

    # Pre-fire safety gate: loss breaker + balance/position reconciliation,
    # evaluated ONCE here and folded into a single live->paper downgrade via
    # gate.evaluate_fire. Fails closed — any tripped guard disables live for the
    # whole fire, and nothing downstream can re-enable it.
    #
    # Position reconciliation compares the bot's ALL-TIME open-position count
    # (count_open_positions_from_fills over the full fill history) against the
    # exchange-side count the funder wallet actually holds (Polymarket Data API
    # via get_open_position_count). Both legs are dimensionally the same
    # (all-time open), so genuine drift — a recorded fill that never settled, or
    # an exchange position the bot never logged — trips RECONCILE.
    if live_executor is not None:
        from tradingagents.exchange.reconciliation import (
            reconcile, count_open_positions_from_fills, open_exposure_from_fills,
        )
        from tradingagents.exchange.scoring import load_jsonl_rows
        all_fills = load_jsonl_rows(POLYMARKET_OUTPUT_DIR)  # no date => all days
        expected_positions = count_open_positions_from_fills(all_fills)
        open_exposure = open_exposure_from_fills(all_fills)
        recon = reconcile(
            expected_open_positions=expected_positions,
            actual_open_positions=live_executor.get_open_position_count(),
            intended_capital_usd=args.capital,
            actual_balance_usd=live_executor.get_usdc_balance(),
        )
        breaker_tripped = loss_breaker.is_tripped()
        verdict = evaluate_fire(
            kill_switch_on=kill_on,
            breaker_tripped=breaker_tripped,
            reconcile_ok=recon.ok,
            open_exposure_usd=open_exposure,
            exposure_budget_usd=args.exposure_budget,
        )
        if not verdict.live_allowed:
            print(
                f"FIRE_HALT {list(verdict.reason_codes)} — downgrading to paper "
                f"this fire. open_exposure=${open_exposure:.2f} "
                f"budget=${args.exposure_budget:.2f} recon={recon.detail}",
                file=sys.stderr,
            )
            if breaker_tripped:
                st = loss_breaker.status()
                notifier.breaker_tripped(st["reasons"], {
                    "daily_realized_pnl": st["daily_realized_pnl"],
                    "drawdown": st["drawdown"],
                })
            if not recon.ok:
                notifier.reconciliation_halt(list(recon.halt_reasons), recon.detail)
            if "NOTIONAL_EXPOSURE" in verdict.reason_codes:
                notifier.send(
                    f"NOTIONAL_EXPOSURE halt: open ${open_exposure:.2f} >= "
                    f"budget ${args.exposure_budget:.2f}; fire downgraded to paper.",
                    severity="warn",
                )
            live_executor = None

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

        def _skip_fill(reason: str, extra: dict | None = None) -> None:
            """Write a SKIPPED row to the fill JSONL with consistent base fields.

            All fill-time skips share these base fields so the audit-trail schema
            stays consistent. Per-reason fields are added via `extra`.
            """
            payload = {
                "ts": now.isoformat(),
                "market_id": m["id"],
                "question": question,
                "status": "SKIPPED",
                "reason": reason,
                "slug": m.get("slug"),
                **(extra or {}),
            }
            append_jsonl(fill_log_path, payload)
        if not args.quiet:
            print(f"--- [{i}/{len(markets)}] {question[:80]}")
            print(f"    yes_price={m['yes_price']:.3f}  end={m.get('end_date')}")

        # Pre-flight gate 1: daily LLM budget. Must fire BEFORE the LLM call.
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

        # Pre-flight gate 2: cluster cap. Fires BEFORE the LLM call so we
        # don't waste budget analysing a market we cannot trade anyway. The
        # 7-of-7 Trump-Xi loop would have called the LLM 7 times before;
        # now it calls 1, saves ~$0.20/cluster at current Sonnet pricing.
        # See PR #18 review (quality H1).
        cluster_id: str | None = None
        if args.max_per_cluster > 0:
            cluster_id = resolve_cluster_id(m)
            if cluster_id is None:
                _skip_fill("cluster_unknown")
                if not args.quiet:
                    print(f"    fill: SKIP — cluster_unknown (cannot group market safely)\n")
                continue
            if cluster_counts.get(cluster_id, 0) >= args.max_per_cluster:
                _skip_fill("cluster_full", {
                    "cluster_id": cluster_id,
                    "max_per_cluster": args.max_per_cluster,
                })
                if not args.quiet:
                    print(
                        f"    fill: SKIP — cluster_full "
                        f"({cluster_counts[cluster_id]}/{args.max_per_cluster} in cluster {cluster_id})\n"
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
        # Update the in-process cost cache so the next iteration's
        # is_exhausted() check sees this decision's spend.
        cost_tracker.record(getattr(decision, "cost_usd", None) or payload.get("cost_usd"))

        if not args.quiet:
            print(f"    -> {decision.direction.value} (conf {decision.confidence:.2f})")
            print(f"       {decision.rationale[:200]}")

        # Post-LLM gates fire when direction-or-confidence info is needed.

        # Skip HOLD / --no-fill outright (no fill row written — HOLD is its
        # own decision, the decision log already captured it).
        if args.no_fill or decision.direction.value == "HOLD":
            if not args.quiet:
                print()
            continue

        # Gate 3: per-market gate — confidence floor + (opt-in) cost-aware edge.
        # Confidence calibration: conf<0.85 wins ~33% vs 100% at conf>=0.9 (n=17).
        # min_edge defaults to 0 (edge gate off), so default behavior is unchanged.
        buy_price = (
            decision.yes_price_at_analysis
            if decision.direction.value == "BUY_YES"
            else 1.0 - decision.yes_price_at_analysis
        )
        mverdict = evaluate_market(
            direction=decision.direction.value,
            confidence=decision.confidence,
            min_confidence=args.min_confidence,
            buy_price=buy_price,
            min_edge=args.min_edge,
        )
        if not mverdict.allow:
            _reason = {
                "CONFIDENCE_FLOOR": "below_min_confidence",
                "EDGE_NET_OF_COST": "edge_below_cost",
            }.get(mverdict.reason_code, mverdict.reason_code.lower())
            _skip_fill(_reason, {
                "direction": decision.direction.value,
                "confidence": decision.confidence,
                "yes_price_at_analysis": decision.yes_price_at_analysis,
                "min_confidence": args.min_confidence,
                "min_edge": args.min_edge,
                "cluster_id": cluster_id,
                **mverdict.detail,
            })
            if not args.quiet:
                print(
                    f"    fill: SKIP — {mverdict.reason_code} ({_reason}); "
                    f"conf {decision.confidence:.2f} buy_price {buy_price:.3f}\n"
                )
            continue

        token_id = m.get("yes_token_id") if decision.direction.value == "BUY_YES" else m.get("no_token_id")
        if not token_id:
            _skip_fill("no_token_id", {
                "direction": decision.direction.value,
                "cluster_id": cluster_id,
            })
            if not args.quiet:
                print(f"    fill: SKIP — no token id available\n")
            continue

        if live_executor is not None:
            # Per-fire order cap (defense in depth). Cluster cap bounds
            # exposure per group; this bounds the total per fire even if
            # we somehow have N high-conviction decisions across N clusters.
            if (
                args.max_orders_per_fire > 0
                and live_orders_submitted >= args.max_orders_per_fire
            ):
                _skip_fill("max_orders_per_fire", {
                    "direction": decision.direction.value,
                    "confidence": decision.confidence,
                    "cluster_id": cluster_id,
                    "max_orders_per_fire": args.max_orders_per_fire,
                    "live_orders_submitted_so_far": live_orders_submitted,
                })
                if not args.quiet:
                    print(
                        f"    fill: SKIP — max_orders_per_fire "
                        f"({live_orders_submitted}/{args.max_orders_per_fire} already submitted)\n"
                    )
                continue

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
            # Reconciliation-aware accounting. Only a CONFIRMED FILL counts as a
            # real position against the cluster cap + per-fire counter + loss
            # breaker. UNFILLED is a clean no-op (FOK killed). UNCONFIRMED means
            # the API didn't confirm — we do NOT assume a fill, but we DO flag
            # it for human reconciliation (the dangerous middle case).
            outcome = result.get("outcome")
            if outcome == "FILLED":
                if cluster_id:
                    cluster_counts[cluster_id] += 1
                live_orders_submitted += 1
                # Record the at-risk exposure as a provisional realized loss of
                # 0 (position open); breaker tracks settled P&L via score_fills.
                loss_breaker.record_realized_pnl(0.0)
                notifier.order_filled(
                    question, decision.direction.value,
                    result.get("filled_usd", 0.0), result.get("order_id", "?"),
                )
            elif outcome == "UNCONFIRMED":
                notifier.unconfirmed_order(
                    question, result.get("order_id", "?"),
                    result.get("order_status", "?"),
                )
            if not args.quiet:
                if outcome == "FILLED":
                    print(
                        f"    order: FILLED id={result['order_id']}  "
                        f"${result.get('filled_usd', 0):.2f} {result['direction']}  "
                        f"(fire total: {live_orders_submitted}/{args.max_orders_per_fire or '∞'})"
                    )
                elif outcome == "UNCONFIRMED":
                    print(
                        f"    order: UNCONFIRMED id={result.get('order_id')} "
                        f"raw_status={result.get('order_status')} — RECONCILE MANUALLY"
                    )
                else:
                    print(f"    order: {result.get('status')} — {result.get('reason', result.get('order_status', ''))}")
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
        # Update in-process cluster cache for next iteration's cap check.
        if cluster_id and fill.get("filled"):
            cluster_counts[cluster_id] += 1

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
