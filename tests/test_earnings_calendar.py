"""Tests for earnings calendar lookup.

We mock yfinance to keep the test suite hermetic — real Yahoo calls are slow
and flaky in CI.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tradingagents.dataflows.earnings_calendar import (
    EarningsEntry,
    fetch_next_earnings_date,
    find_upcoming_earnings,
)


class _FakeTicker:
    """Stand-in for yf.Ticker — just exposes a calendar dict."""

    def __init__(self, calendar):
        self.calendar = calendar


def _today() -> date:
    return datetime.now(timezone.utc).date()


class TestFetchNextEarningsDate:
    def test_returns_date_when_calendar_has_list(self):
        fake = _FakeTicker({"Earnings Date": [date(2026, 5, 14)]})
        with patch("yfinance.Ticker", return_value=fake):
            assert fetch_next_earnings_date("CSCO") == date(2026, 5, 14)

    def test_returns_date_when_calendar_has_scalar(self):
        fake = _FakeTicker({"Earnings Date": date(2026, 5, 20)})
        with patch("yfinance.Ticker", return_value=fake):
            assert fetch_next_earnings_date("HD") == date(2026, 5, 20)

    def test_returns_date_when_calendar_has_datetime(self):
        fake = _FakeTicker({"Earnings Date": [datetime(2026, 5, 14, 12, 0)]})
        with patch("yfinance.Ticker", return_value=fake):
            assert fetch_next_earnings_date("AAPL") == date(2026, 5, 14)

    def test_returns_none_when_missing(self):
        fake = _FakeTicker({})
        with patch("yfinance.Ticker", return_value=fake):
            assert fetch_next_earnings_date("XYZ") is None

    def test_returns_none_on_empty_list(self):
        fake = _FakeTicker({"Earnings Date": []})
        with patch("yfinance.Ticker", return_value=fake):
            assert fetch_next_earnings_date("XYZ") is None

    def test_returns_none_on_exception(self):
        def raiser(_t):
            raise RuntimeError("network down")
        with patch("yfinance.Ticker", side_effect=raiser):
            assert fetch_next_earnings_date("XYZ") is None

    def test_returns_none_when_calendar_is_none(self):
        with patch("yfinance.Ticker", return_value=_FakeTicker(None)):
            assert fetch_next_earnings_date("XYZ") is None


class TestFindUpcomingEarnings:
    def test_filters_to_window(self):
        today = _today()
        cal_map = {
            "INWINDOW": {"Earnings Date": [today + timedelta(days=3)]},
            "FAROUT": {"Earnings Date": [today + timedelta(days=30)]},
            "PAST": {"Earnings Date": [today - timedelta(days=5)]},
            "NONE": {},
        }

        def fake_ticker(t):
            return _FakeTicker(cal_map.get(t, {}))

        with patch("yfinance.Ticker", side_effect=fake_ticker):
            results = find_upcoming_earnings(
                ["INWINDOW", "FAROUT", "PAST", "NONE"],
                window_days=10,
                max_workers=2,
            )

        tickers = {r.ticker for r in results}
        assert tickers == {"INWINDOW"}
        assert results[0].days_out == 3

    def test_sorted_by_date(self):
        today = _today()
        cal_map = {
            "LATER":  {"Earnings Date": [today + timedelta(days=8)]},
            "SOONER": {"Earnings Date": [today + timedelta(days=2)]},
            "MID":    {"Earnings Date": [today + timedelta(days=5)]},
        }
        with patch("yfinance.Ticker", side_effect=lambda t: _FakeTicker(cal_map[t])):
            results = find_upcoming_earnings(
                ["LATER", "SOONER", "MID"], window_days=14, max_workers=2,
            )
        assert [r.ticker for r in results] == ["SOONER", "MID", "LATER"]

    def test_dedupes_candidates(self):
        today = _today()
        with patch("yfinance.Ticker", return_value=_FakeTicker({"Earnings Date": [today + timedelta(days=3)]})):
            results = find_upcoming_earnings(["AAPL", "AAPL", "AAPL"], window_days=10, max_workers=2)
        assert len(results) == 1

    def test_respects_min_days(self):
        today = _today()
        cal_map = {
            "TODAY": {"Earnings Date": [today]},
            "TWODAYS": {"Earnings Date": [today + timedelta(days=2)]},
        }
        with patch("yfinance.Ticker", side_effect=lambda t: _FakeTicker(cal_map[t])):
            results = find_upcoming_earnings(
                ["TODAY", "TWODAYS"], window_days=10, min_days=1, max_workers=2,
            )
        assert {r.ticker for r in results} == {"TWODAYS"}

    def test_empty_input(self):
        results = find_upcoming_earnings([], window_days=10)
        assert results == []
