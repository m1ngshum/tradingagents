"""Per-ticker earnings date lookup via yfinance.

Used by the stock watchlist builder to rotate the daily analysis pool toward
companies with imminent earnings catalysts — the bull/bear pipeline performs
best when there's a hard, dated event for the model to anchor on.

yfinance's per-ticker `calendar` attribute returns a dict containing
'Earnings Date' (list of dates).  We batch these in a thread pool because
each call hits Yahoo's servers and takes ~0.3-1.0s.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EarningsEntry:
    ticker: str
    earnings_date: date
    days_out: int  # days from today to earnings (today = 0)


def fetch_next_earnings_date(ticker: str) -> Optional[date]:
    """Return the next earnings date for `ticker`, or None on miss.

    yfinance's calendar field can be missing or stale; we tolerate either by
    returning None rather than raising.  Caller should treat absent dates as
    "no known catalyst".
    """
    try:
        cal = yf.Ticker(ticker).calendar
    except Exception as exc:  # noqa: BLE001 — yfinance throws many shapes
        logger.warning("earnings lookup failed for %s: %s", ticker, exc)
        return None

    if not cal:
        return None

    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not dates:
        return None

    # `dates` can be a single date or a list of dates
    if isinstance(dates, (list, tuple)):
        if not dates:
            return None
        dates = dates[0]

    if isinstance(dates, datetime):
        return dates.date()
    if isinstance(dates, date):
        return dates
    return None


def find_upcoming_earnings(
    candidates: Iterable[str],
    *,
    window_days: int = 10,
    min_days: int = 0,
    max_workers: int = 16,
) -> list[EarningsEntry]:
    """Return tickers in `candidates` with earnings within `window_days`.

    Args:
        candidates: Ticker symbols to check.
        window_days: Maximum days from today for earnings to count.
        min_days: Minimum days from today (default 0 — includes today).
        max_workers: Concurrent yfinance lookups.

    Tickers whose earnings date is missing or outside the window are filtered
    out — only the matching subset is returned.  Results are sorted by
    `earnings_date` ascending.
    """
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=window_days)

    matches: list[EarningsEntry] = []

    candidates = list(dict.fromkeys(candidates))  # dedupe, preserve order

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_next_earnings_date, t): t for t in candidates}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                eday = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if eday is None:
                continue
            days_out = (eday - today).days
            if days_out < min_days or days_out > window_days:
                continue
            if eday > horizon:
                continue
            matches.append(EarningsEntry(ticker=ticker, earnings_date=eday, days_out=days_out))

    matches.sort(key=lambda e: e.earnings_date)
    return matches
