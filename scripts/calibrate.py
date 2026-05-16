"""Calibration analysis on accumulated decisions + fills.

Usage:
    python scripts/calibrate.py                       # all data, default dir
    python scripts/calibrate.py --dir /path/to/state  # custom state-repo path
    python scripts/calibrate.py --output report.md    # write to file

Computes:
  - Win rate overall (+ split: UMA-finalized only vs incl. proposed)
  - Win rate by confidence band, by direction, by category, by cluster
  - Baseline comparisons (always-NO, always-YES, market-implied)
  - Sample sizes (with "insufficient data" floor)

Reads:
  - {dir}/polymarket/paper-fills-*.jsonl  (positions taken)
  - {dir}/polymarket/decisions-*.jsonl    (all decisions including HOLDs)
  - Gamma /markets API                    (current outcomes per fill)

No LLM calls. Safe to run alongside routine fires.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from tradingagents.dataflows.market_classifier import classify_market
from tradingagents.dataflows.polymarket_data import get_market_by_id
from tradingagents.exchange.scoring import (
    MarketOutcome,
    fetch_outcomes,
    load_jsonl_rows,
)


CONF_BANDS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]


def _print(out: TextIO, *args: str) -> None:
    print(*args, file=out)


def _bucket_band(conf: float) -> str:
    for lo, hi in CONF_BANDS:
        if lo <= conf < hi:
            return f"[{lo:.2f},{hi:.2f})"
    return "[unknown]"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path.home() / "repos" / "m1ngshum" / "tradingagents-state",
        help="State repo root (must contain polymarket/ subdir). Default: ~/repos/m1ngshum/tradingagents-state",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write report to file instead of stdout (will overwrite). Markdown.",
    )
    parser.add_argument(
        "--min-resolved-for-recommendation",
        type=int,
        default=10,
        help="Below this number of resolved trades, suppress the verdict line (default 10).",
    )
    args = parser.parse_args()

    polymarket_dir = args.dir / "polymarket"
    if not polymarket_dir.exists():
        print(f"ERROR: {polymarket_dir} does not exist", file=sys.stderr)
        return 2

    # Sandbox --output to the user's home dir or /tmp. Prevents accidentally
    # (or via a piped-in arg) clobbering system paths like /etc/crontab.
    if args.output is not None:
        resolved = args.output.resolve()
        allowed_roots = (Path.home().resolve(), Path("/tmp").resolve())
        if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
            print(
                f"ERROR: --output must be within home or /tmp, got {resolved}",
                file=sys.stderr,
            )
            return 2

    out: TextIO = open(args.output, "w") if args.output else sys.stdout

    # ---------- Load decisions (what the bot DECIDED to do) ----------
    # We read decisions rather than fills because:
    # (1) decisions carry `confidence`, fills don't,
    # (2) high-conviction BUYs sometimes get suppressed by executor guards
    #     (extreme-price, no-token-id, cluster cap, budget cap, min-confidence)
    #     — those represent real signal the bot had, just not actionable trades.
    # The is-it-actionable question lives in score_fills.py; the does-the-bot-
    # have-edge question lives here.
    decisions = load_jsonl_rows(
        polymarket_dir,
        glob_pattern="decisions-*.jsonl",
    )
    # Filter: only directional decisions (BUY_YES/BUY_NO) with a market_id
    decisions = [
        d for d in decisions
        if d.get("direction") in ("BUY_YES", "BUY_NO") and d.get("market_id")
    ]
    if not decisions:
        _print(out, f"No directional decisions found in {polymarket_dir}")
        if out is not sys.stdout:
            out.close()
        return 1

    # Dedupe by market_id (same market may appear across manual + routine files)
    seen, deduped = set(), []
    for d in decisions:
        mid = d["market_id"]
        if mid in seen:
            continue
        seen.add(mid)
        deduped.append(d)

    _print(out, f"# Calibration Report")
    _print(out, f"")
    _print(out, f"- State repo: `{args.dir}`")
    _print(out, f"- Directional decisions loaded: {len(decisions)} ({len(deduped)} unique markets)")

    # ---------- Resolve outcomes ----------
    _print(out, f"- Fetching outcomes via Gamma...")
    outcomes = fetch_outcomes(
        (f["market_id"] for f in deduped),
        fetch_market=get_market_by_id,
    )

    # Annotate each fill with outcome + result
    for f in deduped:
        info = outcomes.get(f["market_id"], {})
        oc = info.get("outcome", MarketOutcome.UNKNOWN)
        f["_outcome"] = oc
        f["_is_final"] = info.get("is_finalized", False)
        direction = f.get("direction")
        if oc == MarketOutcome.YES_WINS:
            f["_result"] = "WIN" if direction == "BUY_YES" else "LOSS"
        elif oc == MarketOutcome.NO_WINS:
            f["_result"] = "WIN" if direction == "BUY_NO" else "LOSS"
        elif oc == MarketOutcome.CANCELED:
            f["_result"] = "CANCELED"
        else:
            f["_result"] = "PENDING"

    resolved = [f for f in deduped if f["_result"] in ("WIN", "LOSS")]
    final_only = [f for f in resolved if f["_is_final"]]
    wins = sum(1 for f in resolved if f["_result"] == "WIN")

    # ---------- Overall ----------
    _print(out, f"")
    _print(out, f"## Overall")
    _print(out, f"")
    _print(out, f"| metric | value |")
    _print(out, f"|---|---|")
    _print(out, f"| Unique markets | {len(deduped)} |")
    _print(out, f"| Pending | {sum(1 for f in deduped if f['_result'] == 'PENDING')} |")
    _print(out, f"| Resolved (incl. proposed) | {len(resolved)} |")
    _print(out, f"| UMA-finalized only | {len(final_only)} |")
    _print(out, f"| Wins | {wins} |")
    _print(out, f"| Losses | {len(resolved) - wins} |")
    if resolved:
        _print(out, f"| **Win rate (proposed+final)** | **{wins/len(resolved)*100:.1f}%** |")
    if final_only:
        f_wins = sum(1 for f in final_only if f["_result"] == "WIN")
        _print(out, f"| **Win rate (final only)** | **{f_wins/len(final_only)*100:.1f}%** |")
    else:
        _print(out, f"| Win rate (final only) | n/a (none finalized) |")

    # ---------- By confidence band ----------
    _print(out, f"")
    _print(out, f"## By confidence band")
    _print(out, f"")
    _print(out, f"| band | n | wins | losses | win_rate | mean_conf |")
    _print(out, f"|---|---|---|---|---|---|")
    for lo, hi in CONF_BANDS:
        sub = [f for f in resolved if lo <= f.get("confidence", 0) < hi]
        if not sub:
            _print(out, f"| [{lo:.2f},{hi:.2f}) | 0 | — | — | — | — |")
            continue
        w = sum(1 for f in sub if f["_result"] == "WIN")
        mc = statistics.mean(f.get("confidence", 0) for f in sub)
        _print(out, f"| [{lo:.2f},{hi:.2f}) | {len(sub)} | {w} | {len(sub)-w} | {w/len(sub)*100:.1f}% | {mc:.2f} |")

    # ---------- By direction ----------
    _print(out, f"")
    _print(out, f"## By direction")
    _print(out, f"")
    _print(out, f"| direction | n | wins | win_rate |")
    _print(out, f"|---|---|---|---|")
    for side in ("BUY_YES", "BUY_NO"):
        sub = [f for f in resolved if f.get("direction") == side]
        if not sub:
            _print(out, f"| {side} | 0 | — | — |")
            continue
        w = sum(1 for f in sub if f["_result"] == "WIN")
        _print(out, f"| {side} | {len(sub)} | {w} | {w/len(sub)*100:.1f}% |")

    # ---------- By category ----------
    _print(out, f"")
    _print(out, f"## By market category")
    _print(out, f"")
    by_cat: dict[str, list] = defaultdict(list)
    for f in resolved:
        cat = classify_market(f.get("question") or "").category
        by_cat[cat].append(f)
    _print(out, f"| category | n | wins | win_rate |")
    _print(out, f"|---|---|---|---|")
    for cat in sorted(by_cat, key=lambda k: -len(by_cat[k])):
        sub = by_cat[cat]
        w = sum(1 for f in sub if f["_result"] == "WIN")
        _print(out, f"| {cat} | {len(sub)} | {w} | {w/len(sub)*100:.1f}% |")

    # ---------- By cluster (newly-captured field) ----------
    by_cluster: dict[str, list] = defaultdict(list)
    for f in resolved:
        cid = f.get("cluster_id")
        if cid:
            by_cluster[cid].append(f)
    if by_cluster:
        _print(out, f"")
        _print(out, f"## By cluster (negRisk siblings / shared event)")
        _print(out, f"")
        _print(out, f"| cluster_id | n | wins | win_rate |")
        _print(out, f"|---|---|---|---|")
        for cid in sorted(by_cluster, key=lambda k: -len(by_cluster[k])):
            sub = by_cluster[cid]
            w = sum(1 for f in sub if f["_result"] == "WIN")
            _print(out, f"| `{cid}` | {len(sub)} | {w} | {w/len(sub)*100:.1f}% |")

    # ---------- By model ----------
    by_model: dict[str, list] = defaultdict(list)
    for f in resolved:
        # Fill rows don't always carry model; fall back to "unknown"
        by_model[f.get("model") or "unknown"].append(f)
    if len(by_model) > 1:
        _print(out, f"")
        _print(out, f"## By model")
        _print(out, f"")
        _print(out, f"| model | n | wins | win_rate |")
        _print(out, f"|---|---|---|---|")
        for model, sub in by_model.items():
            w = sum(1 for f in sub if f["_result"] == "WIN")
            _print(out, f"| {model} | {len(sub)} | {w} | {w/len(sub)*100:.1f}% |")

    # ---------- Baseline comparisons ----------
    _print(out, f"")
    _print(out, f"## Baselines (on the same {len(resolved)} resolved markets)")
    _print(out, f"")
    if resolved:
        always_no_wins = sum(1 for f in resolved if f["_outcome"] == MarketOutcome.NO_WINS)
        always_yes_wins = sum(1 for f in resolved if f["_outcome"] == MarketOutcome.YES_WINS)
        mi_wins = 0
        for f in resolved:
            yes_pr = f.get("yes_price_at_analysis", 0.5)
            market_side = MarketOutcome.YES_WINS if yes_pr > 0.5 else MarketOutcome.NO_WINS
            if market_side == f["_outcome"]:
                mi_wins += 1
        _print(out, f"| baseline | win_rate |")
        _print(out, f"|---|---|")
        _print(out, f"| **Bot (actual)** | **{wins/len(resolved)*100:.1f}%** |")
        _print(out, f"| Always-NO | {always_no_wins/len(resolved)*100:.1f}% |")
        _print(out, f"| Always-YES | {always_yes_wins/len(resolved)*100:.1f}% |")
        _print(out, f"| Market-implied (>0.5) | {mi_wins/len(resolved)*100:.1f}% |")
        _print(out, f"| Coin flip (expected) | 50.0% |")

    # ---------- Recommendation ----------
    _print(out, f"")
    _print(out, f"## Recommendation")
    _print(out, f"")
    if not resolved:
        _print(out, f"INSUFFICIENT DATA — no resolved decisions yet.")
        if out is not sys.stdout:
            out.close()
        return 0
    if len(resolved) < args.min_resolved_for_recommendation:
        _print(out, f"INSUFFICIENT DATA — only {len(resolved)} resolved trades "
              f"(need >= {args.min_resolved_for_recommendation} for a verdict).")
        if out is not sys.stdout:
            out.close()
        return 0
    bot_wr = wins / len(resolved)
    best_baseline = max(
        always_no_wins / len(resolved),
        always_yes_wins / len(resolved),
        mi_wins / len(resolved),
    )
    if bot_wr <= best_baseline - 0.05:
        verdict = f"**NO EDGE** — bot underperforms best baseline by >5pp ({bot_wr*100:.1f}% vs {best_baseline*100:.1f}%). Halt routines until prompt-level fix."
    elif bot_wr <= best_baseline + 0.05:
        verdict = f"**NO MEASURABLE EDGE** — within noise of best baseline ({bot_wr*100:.1f}% vs {best_baseline*100:.1f}%). Suspend new bets, fix calibration."
    else:
        verdict = f"**POSSIBLE EDGE** (+{(bot_wr-best_baseline)*100:.1f}pp over best baseline)."
    _print(out, verdict)
    high_conf = [f for f in resolved if f.get("confidence", 0) >= 0.85]
    if high_conf:
        hc_w = sum(1 for f in high_conf if f["_result"] == "WIN")
        _print(out, f"")
        _print(out, f"At conf >= 0.85 ({len(high_conf)} trades): **{hc_w/len(high_conf)*100:.1f}%** win rate")

    if out is not sys.stdout:
        out.close()
        print(f"Report written to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
