"""Execution-quality metrics over the raw Polymarket fill log.

Separate from analyze_performance's ROI/Brier (which measure whether the
STRATEGY has edge): these measure whether EXECUTION is sane — the one thing the
small-money live pilot actually exists to learn (PLAN.md §4): do real fills
differ from paper assumptions? Pure functions over fill-log row dicts; no I/O.
"""

from __future__ import annotations


def _safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def _median(xs: list[float]) -> float | None:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def compute_execution_metrics(fills: list[dict]) -> dict:
    """Fill-rate + slippage from the fill log (paper and live rows).

    - paper_fill_rate: of paper ATTEMPTS (non-SKIPPED/ERROR rows that carry a
      'filled' bool and are not live), the fraction that filled.
    - live_fill_rate: of live attempts (live=True, outcome in
      FILLED/UNFILLED/UNCONFIRMED), the fraction FILLED. This is the
      execution-feasibility number the pilot watches (PLAN.md §4: fill-rate
      abort if live FILLED rate < 50% of attempts).
    - mean/median_slippage_pp: paper slippage (vwap - best_ask, in pp) as the
      realized-vs-quoted proxy.

    live_realized_vs_quoted_bps is reported as None: capturing the real matched
    price requires the executor to record it (Phase 3). We do NOT fabricate it.
    """
    paper_attempts = [
        f for f in fills
        if not f.get("live")
        and f.get("status") not in ("SKIPPED", "ERROR")
        and "filled" in f
    ]
    live_attempts = [
        f for f in fills
        if f.get("live") is True
        and f.get("outcome") in ("FILLED", "UNFILLED", "UNCONFIRMED")
    ]
    n_paper = len(paper_attempts)
    n_paper_filled = sum(1 for f in paper_attempts if f.get("filled") is True)
    n_live = len(live_attempts)
    n_live_filled = sum(1 for f in live_attempts if f.get("outcome") == "FILLED")

    slips = [
        f["slippage_pp"] for f in paper_attempts
        if isinstance(f.get("slippage_pp"), (int, float))
    ]
    return {
        "paper_attempts": n_paper,
        "paper_filled": n_paper_filled,
        "paper_fill_rate": _safe_div(n_paper_filled, n_paper),
        "live_attempts": n_live,
        "live_filled": n_live_filled,
        "live_fill_rate": _safe_div(n_live_filled, n_live),
        "mean_slippage_pp": (sum(slips) / len(slips)) if slips else None,
        "median_slippage_pp": _median(slips),
        # Phase 3: needs the executor to record the matched price per live fill.
        "live_realized_vs_quoted_bps": None,
    }
