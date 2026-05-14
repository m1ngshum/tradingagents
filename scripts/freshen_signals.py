"""Re-score recent Polymarket discoveries against current prices.

The discovery cron analyses markets at one point in time (e.g. 8am HKT)
and saves the bot's decision + Kelly edge.  By the time you read the
output and want to trade, the market price has often moved — meaning
the edge the bot reported is stale.

This script reads recent discoveries, fetches each market's CURRENT
yes_price from gamma-api, and recomputes the realizable Kelly edge.
Stale signals (edge gone) drop out; fresh ones surface clearly.

Usage:
    .venv/bin/python scripts/freshen_signals.py [--date 2026-05-14] \\
        [--min-edge 0.10] [--max-age-hours 24] [--verbose]

If no --date is given, scans all `discoveries-*.jsonl` files modified
within --max-age-hours.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tradingagents.exchange.io_utils import POLYMARKET_OUTPUT_DIR

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _kelly_edge(yes_price: float, direction: str, confidence: float) -> float:
    """Same formula as discover_polymarket._kelly_edge — kept local to avoid
    coupling. Returns 0 for HOLD or non-positive edge.
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


def _fetch_current_price(market_id: str, session: requests.Session) -> float | None:
    """Return current yes_price (0.0-1.0) for the given market_id, or None on miss."""
    try:
        r = session.get(f"{GAMMA_BASE}/markets/{market_id}", timeout=10)
        if r.status_code != 200:
            return None
        m = r.json()
        if m.get("closed") or not m.get("active"):
            return None
        prices_str = m.get("outcomePrices") or "[]"
        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
        if not prices:
            return None
        return float(prices[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("price fetch failed for %s: %s", market_id, exc)
        return None


def _iter_discovery_files(date: str | None, max_age_hours: float) -> Iterable[Path]:
    if date:
        p = POLYMARKET_OUTPUT_DIR / f"discoveries-{date}.jsonl"
        if p.exists():
            yield p
        return

    now_ts = time.time()
    cutoff = now_ts - max_age_hours * 3600
    for p in sorted(POLYMARKET_OUTPUT_DIR.glob("discoveries-*.jsonl")):
        if p.stat().st_mtime >= cutoff:
            yield p


def _load_signals(files: Iterable[Path]) -> list[dict]:
    seen_market_ids: set[str] = set()
    signals: list[dict] = []
    for path in files:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("direction") == "HOLD":
                    continue
                if d.get("kelly_edge", 0) <= 0:
                    continue
                mid = str(d.get("market_id", ""))
                if mid in seen_market_ids:
                    continue
                seen_market_ids.add(mid)
                signals.append(d)
    return signals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default=None,
        help="Discovery date to refresh (YYYY-MM-DD). Default: scan recent files",
    )
    parser.add_argument(
        "--min-edge", type=float, default=0.10,
        help="Minimum fresh Kelly edge to surface (default 0.10)",
    )
    parser.add_argument(
        "--max-age-hours", type=float, default=48.0,
        help="When --date is omitted, scan files modified within N hours (default 48)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    files = list(_iter_discovery_files(args.date, args.max_age_hours))
    if not files:
        target = args.date or f"last {args.max_age_hours}h"
        print(f"No discovery files found for {target}", file=sys.stderr)
        return 1

    signals = _load_signals(files)
    if not signals:
        print("No actionable signals (all HOLD or zero-edge) in scanned files",
              file=sys.stderr)
        return 0

    print(f"Refreshing {len(signals)} signals from {len(files)} discovery file(s)...")
    print()

    session = requests.Session()
    rows: list[dict] = []
    for sig in signals:
        mid = str(sig["market_id"])
        direction = sig["direction"]
        confidence = float(sig["confidence"])
        analysis_yes = float(sig["yes_price_at_analysis"])
        analysis_edge = float(sig.get("kelly_edge", 0))

        current_yes = _fetch_current_price(mid, session)
        if current_yes is None:
            if args.verbose:
                print(f"  SKIP {mid}: market closed or fetch failed")
            continue

        fresh_edge = _kelly_edge(current_yes, direction, confidence)
        moved_pp = (current_yes - analysis_yes) * 100
        rows.append({
            "question": sig["question"],
            "direction": direction,
            "confidence": confidence,
            "analysis_yes": analysis_yes,
            "analysis_edge": analysis_edge,
            "current_yes": current_yes,
            "fresh_edge": fresh_edge,
            "moved_pp": moved_pp,
            "category": sig.get("category", "?"),
            "end_date": (sig.get("end_date") or "")[:10],
            "market_id": mid,
        })

    rows.sort(key=lambda r: r["fresh_edge"], reverse=True)

    print(f"{'STATUS':8}  {'DIR':7}  {'CONF':5}  {'ANL_YES':7}  {'NOW_YES':7}  "
          f"{'MOVED':6}  {'OLD_EDGE':8}  {'FRESH':6}  RESOLVES   QUESTION")
    print("-" * 130)
    fresh_count = 0
    for r in rows:
        status = "FRESH" if r["fresh_edge"] >= args.min_edge else "stale"
        if status == "FRESH":
            fresh_count += 1
        print(
            f"{status:8}  {r['direction']:7}  {r['confidence']:.2f}   "
            f"{r['analysis_yes']:.3f}    {r['current_yes']:.3f}    "
            f"{r['moved_pp']:+5.1f}pp  {r['analysis_edge']:.3f}    {r['fresh_edge']:.3f}  "
            f"{r['end_date']}  \"{r['question'][:55]}\""
        )

    print()
    print(f"=== {fresh_count} of {len(rows)} signals still actionable "
          f"at edge >= {args.min_edge:.2f} ===")
    if fresh_count > 0:
        print()
        print("Fresh signal URLs (open on Polymarket to trade):")
        for r in rows:
            if r["fresh_edge"] < args.min_edge:
                continue
            url = _trade_url(r["market_id"], session)
            print(f"  [{r['direction']}] {url}")
            print(f"      edge={r['fresh_edge']:.3f}  buy at "
                  f"~${(1-r['current_yes']) if r['direction']=='BUY_NO' else r['current_yes']:.3f}")

    return 0


def _trade_url(market_id: str, session: requests.Session) -> str:
    """Best-effort URL: prefer the parent event that actually contains this market_id.

    Approach: use public-search with the FULL question text, then verify each
    candidate event's `markets[]` actually contains this market_id.  This
    avoids the generic-event false-match (a Senate primary market routing to
    "which-party-will-win-the-senate-in-2026").
    """
    try:
        r = session.get(f"{GAMMA_BASE}/markets/{market_id}", timeout=10)
        m = r.json() if r.status_code == 200 else {}
        question = m.get("question", "")
        slug = m.get("slug", "")

        # If standalone (no group), the market's own slug works directly.
        if not m.get("groupItemTitle") and slug:
            return f"https://polymarket.com/event/{slug}"

        # Otherwise search for the parent event that contains this market_id.
        # Try the most specific query first (full question, then groupItemTitle).
        candidates: list[str] = [question]
        gt = m.get("groupItemTitle")
        if gt:
            # Combine groupItemTitle with the most distinctive non-stopword from
            # the question to disambiguate (e.g. "Jamie Davis Louisiana").
            words = [w for w in question.split() if len(w) > 4][:4]
            candidates.insert(0, gt + " " + " ".join(words))

        for q in candidates:
            r2 = session.get(f"{GAMMA_BASE}/public-search",
                             params={"q": q, "limit": 5}, timeout=10)
            if r2.status_code != 200:
                continue
            for ev in r2.json().get("events", []):
                ev_markets = ev.get("markets", [])
                if any(str(em.get("id")) == market_id for em in ev_markets):
                    return f"https://polymarket.com/event/{ev['slug']}"

        if slug:
            return f"https://polymarket.com/event/{slug}"
    except Exception:  # noqa: BLE001
        pass
    return f"https://polymarket.com/market/{market_id}"


if __name__ == "__main__":
    sys.exit(main())
