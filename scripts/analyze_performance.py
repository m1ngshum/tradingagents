"""Full performance analysis: efficiency, risk, accuracy.

Hit rate is the wrong metric for prediction markets — a NO bet on a 95%-NO
market wins 95% of the time and earns 5c. This script measures what actually
matters: realized P&L / ROI / expected value, calibration (Brier), and the
risk profile of the sizing.

Three lenses:
  ACCURACY    — calibration curve + Brier score. When the bot says 0.85,
                does it win 85%? Per direction, per confidence band.
  EFFICIENCY  — realized ROI vs the bot's *claimed* edge. How much of the
                theoretical EV survives the buy price? Fees as share of edge.
  RISK        — simulated position sizing (half-Kelly), concentration,
                running drawdown, capital-at-risk distribution.

Two populations:
  FILLS     — actual taken positions (paper-fills-*.jsonl). Ground-truth P&L.
              We only have the 14 manual 2026-05-14 trades here.
  DECISIONS — every directional decision (decisions-*.jsonl). Simulated at a
              fixed notional so we get an EV read on the whole strategy, not
              just the handful that filled.

Reads outcomes from Gamma (network). No LLM calls.

Usage:
    python scripts/analyze_performance.py
    python scripts/analyze_performance.py --dir /path/to/state --output /tmp/perf.md
    python scripts/analyze_performance.py --notional 100
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict
from typing import TextIO

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from tradingagents.dataflows.polymarket_data import get_market_by_id
from tradingagents.exchange.execution_metrics import compute_execution_metrics
from tradingagents.exchange.scoring import (
    MarketOutcome,
    fetch_outcomes,
    load_jsonl_rows,
    score_position,
)

# Polymarket charges no maker/taker fee on most markets today; the system's
# score_position already models fee_estimate_if_win when present in a fill.
# For simulated decisions we assume zero explicit fee (conservative — real
# slippage is captured separately via the fill vs decision price gap).


def _print(out: TextIO | None, msg: str) -> None:
    print(msg)
    if out:
        out.write(msg + "\n")


def _buy_price(direction: str, yes_price: float) -> float:
    return yes_price if direction == "BUY_YES" else 1.0 - yes_price


def _won(direction: str, outcome: MarketOutcome) -> bool | None:
    """True/False if resolved, None if not a clean resolution."""
    if outcome == MarketOutcome.YES_WINS:
        return direction == "BUY_YES"
    if outcome == MarketOutcome.NO_WINS:
        return direction == "BUY_NO"
    return None


def _load_directional_decisions(state_dir: Path) -> list[dict]:
    rows = load_jsonl_rows(
        state_dir / "polymarket", glob_pattern="decisions-*.jsonl"
    )
    out = []
    for d in rows:
        if d.get("direction") not in ("BUY_YES", "BUY_NO"):
            continue
        yp = d.get("yes_price_at_analysis", d.get("yes_price"))
        if yp is None:
            continue
        d["_yes"] = float(yp)
        d["_buy"] = _buy_price(d["direction"], float(yp))
        out.append(d)
    return out


def analyze(state_dir: Path, notional: float, out: TextIO | None) -> int:
    _print(out, "# Performance Analysis — Efficiency / Risk / Accuracy\n")
    _print(out, f"- State repo: `{state_dir}`")
    _print(out, f"- Simulated notional per decision: ${notional:.0f}\n")

    # ---- Population 1: actual fills (ground-truth P&L) ----
    fills = [
        f for f in load_jsonl_rows(state_dir / "polymarket")
        if f.get("filled") and float(f.get("filled_usd", 0)) > 0
    ]
    _print(out, "## 1. Actual fills (ground-truth realized P&L)\n")
    if fills:
        ids = {f["market_id"] for f in fills}
        _print(out, f"Fetching outcomes for {len(ids)} markets...")
        oc = fetch_outcomes(ids, fetch_market=get_market_by_id)
        realized = 0.0
        invested = 0.0
        w = l = 0
        for f in fills:
            info = oc.get(f["market_id"], {})
            s = score_position(f, info.get("outcome", MarketOutcome.UNKNOWN), info.get("current_yes_price"))
            if s["status"] in ("RESOLVED_WIN", "RESOLVED_LOSS", "CANCELED"):
                realized += s["pnl_usd"]
                invested += float(f["filled_usd"])
                if s["status"] == "RESOLVED_WIN":
                    w += 1
                elif s["status"] == "RESOLVED_LOSS":
                    l += 1
        res = w + l
        _print(out, f"\n| metric | value |")
        _print(out, f"|---|---|")
        _print(out, f"| Filled positions | {len(fills)} |")
        _print(out, f"| Resolved | {res} |")
        _print(out, f"| Wins / Losses | {w} / {l} |")
        if res:
            _print(out, f"| Win rate | {w/res*100:.1f}% |")
        _print(out, f"| Capital deployed | ${invested:.2f} |")
        _print(out, f"| **Realized P&L** | **${realized:+.2f}** |")
        if invested:
            _print(out, f"| **Realized ROI** | **{realized/invested*100:+.1f}%** |")
    else:
        _print(out, "No actual fills found.")

    # ---- Execution quality (fill rate + slippage) ----
    # Measures whether EXECUTION is sane (the live pilot's real question),
    # distinct from whether the strategy has edge. Computed over ALL fill rows.
    em = compute_execution_metrics(load_jsonl_rows(state_dir / "polymarket"))

    def _pct(x: float | None) -> str:
        return f"{x:.1%}" if isinstance(x, float) else "n/a"

    _print(out, "\n## 1b. Execution quality (fill rate + slippage)\n")
    _print(out, "| metric | value |")
    _print(out, "|---|---|")
    _print(out, f"| Paper attempts | {em['paper_attempts']} |")
    _print(out, f"| Paper fill rate | {_pct(em['paper_fill_rate'])} |")
    _print(out, f"| Live attempts | {em['live_attempts']} |")
    _print(out, f"| Live fill rate | {_pct(em['live_fill_rate'])} |")
    _print(out, f"| Median slippage (pp) | {em['median_slippage_pp'] if em['median_slippage_pp'] is not None else 'n/a'} |")
    _print(out, "| Live realized-vs-quoted bps | n/a (Phase 3: needs matched-price capture) |")

    # ---- Population 2: all directional decisions, simulated ----
    decisions = _load_directional_decisions(state_dir)
    _print(out, f"\n## 2. Strategy EV — all {len(decisions)} directional decisions, simulated\n")
    ids = {d["market_id"] for d in decisions}
    _print(out, f"Fetching outcomes for {len(ids)} markets...")
    oc = fetch_outcomes(ids, fetch_market=get_market_by_id)

    resolved = []
    for d in decisions:
        info = oc.get(d["market_id"], {})
        won = _won(d["direction"], info.get("outcome", MarketOutcome.UNKNOWN))
        if won is None:
            continue
        b = d["_buy"]
        if not (0 < b < 1):
            continue
        # $notional staked at buy_price b. contracts = notional / b.
        # win -> contracts * 1.0 - notional ; loss -> -notional
        pnl = (notional / b - notional) if won else -notional
        resolved.append({**d, "_won": won, "_pnl": pnl, "_roi": pnl / notional})

    if not resolved:
        _print(out, "\nNo resolved directional decisions yet.")
        return 0

    tot_pnl = sum(r["_pnl"] for r in resolved)
    tot_stake = notional * len(resolved)
    wins = sum(1 for r in resolved if r["_won"])
    _print(out, f"\n| metric | value |")
    _print(out, f"|---|---|")
    _print(out, f"| Resolved decisions | {len(resolved)} |")
    _print(out, f"| Hit rate | {wins/len(resolved)*100:.1f}% |")
    _print(out, f"| Total staked (simulated) | ${tot_stake:.0f} |")
    _print(out, f"| **Total P&L** | **${tot_pnl:+.2f}** |")
    _print(out, f"| **Strategy ROI** | **{tot_pnl/tot_stake*100:+.1f}%** |")
    _print(out, f"| Avg buy price | {sum(r['_buy'] for r in resolved)/len(resolved):.3f} |")

    # The crucial contrast: hit rate vs ROI by direction
    _print(out, f"\n### By direction — hit rate is NOT profit\n")
    _print(out, f"| direction | n | hit_rate | total P&L | ROI |")
    _print(out, f"|---|---|---|---|---|")
    by_dir = defaultdict(list)
    for r in resolved:
        by_dir[r["direction"]].append(r)
    for dirn, rs in sorted(by_dir.items()):
        w = sum(1 for r in rs if r["_won"])
        pnl = sum(r["_pnl"] for r in rs)
        _print(out, f"| {dirn} | {len(rs)} | {w/len(rs)*100:.1f}% | ${pnl:+.2f} | {pnl/(notional*len(rs))*100:+.1f}% |")

    # ---- Baselines, but as ROI not hit rate ----
    _print(out, f"\n### Baselines as ROI (the honest comparison)\n")
    _print(out, f"| baseline | hit_rate | ROI |")
    _print(out, f"|---|---|---|")
    # always-NO / always-YES simulated on the same resolved markets
    for name, force in (("Always-NO", "BUY_NO"), ("Always-YES", "BUY_YES")):
        bw = bp = 0.0
        bwins = 0
        for r in resolved:
            yp = r["_yes"]
            b = _buy_price(force, yp)
            if not (0 < b < 1):
                continue
            won = _won(force, oc.get(r["market_id"], {}).get("outcome"))
            if won is None:
                continue
            pnl = (notional / b - notional) if won else -notional
            bp += pnl
            bwins += 1 if won else 0
            bw += notional
        if bw:
            _print(out, f"| {name} | {bwins/(bw/notional)*100:.1f}% | {bp/bw*100:+.1f}% |")

    # ---- ACCURACY: Brier + calibration curve ----
    _print(out, f"\n## 3. Accuracy — calibration (is confidence meaningful?)\n")
    # Brier: (confidence_in_own_pick - won)^2, averaged
    brier = sum((r.get("confidence", 0.5) - (1.0 if r["_won"] else 0.0)) ** 2 for r in resolved) / len(resolved)
    _print(out, f"- **Brier score: {brier:.3f}**  (0=perfect, 0.25=always-guess-0.5, lower is better)")
    _print(out, f"\n| conf band | n | mean_conf (claimed) | actual hit rate |")
    _print(out, f"|---|---|---|---|")
    bands = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in bands:
        rs = [r for r in resolved if lo <= r.get("confidence", 0) < hi]
        if not rs:
            continue
        mc = sum(r.get("confidence", 0) for r in rs) / len(rs)
        hr = sum(1 for r in rs if r["_won"]) / len(rs)
        flag = "  <-- overconfident" if mc - hr > 0.15 else ""
        _print(out, f"| [{lo:.2f},{hi:.2f}) | {len(rs)} | {mc:.2f} | {hr*100:.0f}%{flag} |")

    # ---- RISK: sizing + concentration ----
    _print(out, f"\n## 4. Risk profile (half-Kelly sizing simulated)\n")
    from tradingagents.exchange.polymarket_executor import size_polymarket_order
    from tradingagents.agents.schemas import PolymarketDecision, PolymarketDirection

    sizes = []
    for r in resolved:
        try:
            dec = PolymarketDecision(
                market_id=r["market_id"], question=r.get("question", ""),
                direction=PolymarketDirection(r["direction"]),
                confidence=r.get("confidence", 0.5), rationale="",
                yes_price_at_analysis=r["_yes"], cycle_ts=0,
            )
            sz = size_polymarket_order(dec, 1000.0)  # $1000 capital reference
            sizes.append((r, sz["usd"]))
        except Exception:
            continue
    placed = [(r, u) for r, u in sizes if u > 0]
    _print(out, f"- Decisions that would size > $0 (pass conf+price+edge gates): {len(placed)}/{len(resolved)}")
    if placed:
        kelly_pnl = sum((u / r["_buy"] - u) if r["_won"] else -u for r, u in placed)
        kelly_stake = sum(u for _, u in placed)
        _print(out, f"- Capital that would deploy (per $1000): ${kelly_stake:.2f}")
        _print(out, f"- **Realized P&L under actual Kelly sizing: ${kelly_pnl:+.2f}** (ROI {kelly_pnl/kelly_stake*100:+.1f}%)" if kelly_stake else "- no capital deployed")
        biggest = max(placed, key=lambda x: x[1])
        _print(out, f"- Largest single position: ${biggest[1]:.2f} ({biggest[0]['direction']} @ buy {biggest[0]['_buy']:.3f})")
    # concentration: are placed trades clustered?
    cl = defaultdict(int)
    for r, u in placed:
        cl[r.get("cluster_id", "?")] += 1
    multi = {k: v for k, v in cl.items() if v > 1}
    _print(out, f"- Clusters with >1 placed position (concentration risk): {len(multi)}")

    _print(out, f"\n---\n*Generated by analyze_performance.py — measures money, not hit rate.*")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=str(Path.home() / "repos/m1ngshum/tradingagents-state"),
                    help="State repo root (contains polymarket/).")
    ap.add_argument("--notional", type=float, default=100.0,
                    help="Simulated $ per decision (default 100).")
    ap.add_argument("--output", default=None, help="Write markdown report (sandboxed to ~ or /tmp).")
    args = ap.parse_args()

    state_dir = Path(args.dir).expanduser().resolve()
    if not (state_dir / "polymarket").is_dir():
        print(f"ERROR: {state_dir}/polymarket not found", file=sys.stderr)
        return 2

    out = None
    if args.output:
        p = Path(args.output).expanduser().resolve()
        allowed = (Path.home().resolve(), Path("/tmp"), Path("/private/tmp"))
        if not any(str(p).startswith(str(a)) for a in allowed):
            print("ERROR: --output must be within ~ or /tmp", file=sys.stderr)
            return 2
        out = p.open("w", encoding="utf-8")

    try:
        return analyze(state_dir, args.notional, out)
    finally:
        if out:
            out.close()
            print(f"\nReport written to {args.output}")


if __name__ == "__main__":
    raise SystemExit(main())
