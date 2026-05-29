"""Tests for CLOB order-response reconciliation.

Safety-critical: the bot must never assume it holds a position it didn't
confirm. classify_order_response is the fail-safe that maps a raw CLOB
response to FILLED / UNFILLED / UNKNOWN, defaulting to UNKNOWN (never FILLED)
on anything ambiguous.
"""

from __future__ import annotations

from tradingagents.exchange.polymarket_executor import classify_order_response


def test_matched_is_filled():
    r = classify_order_response({"status": "matched", "makingAmount": "20.0"})
    assert r["outcome"] == "FILLED"
    assert r["filled_usd"] == 20.0


def test_unmatched_is_unfilled():
    r = classify_order_response({"status": "unmatched"})
    assert r["outcome"] == "UNFILLED"
    assert r["filled_usd"] == 0.0


def test_cancelled_is_unfilled():
    assert classify_order_response({"status": "cancelled"})["outcome"] == "UNFILLED"
    assert classify_order_response({"status": "canceled"})["outcome"] == "UNFILLED"


def test_success_false_is_unfilled_regardless_of_status():
    r = classify_order_response({"success": False, "status": "matched"})
    assert r["outcome"] == "UNFILLED"  # success=False overrides
    assert r["filled_usd"] == 0.0


def test_unknown_status_is_unknown_not_filled():
    """THE safety property: an unrecognised status must NOT be treated as a fill."""
    r = classify_order_response({"status": "some_new_status_we_dont_know"})
    assert r["outcome"] == "UNKNOWN"
    assert r["filled_usd"] == 0.0


def test_missing_status_is_unknown():
    r = classify_order_response({"orderID": "abc"})
    assert r["outcome"] == "UNKNOWN"


def test_empty_dict_is_unknown():
    assert classify_order_response({})["outcome"] == "UNKNOWN"


def test_non_dict_is_unknown():
    assert classify_order_response(None)["outcome"] == "UNKNOWN"  # type: ignore[arg-type]
    assert classify_order_response("matched")["outcome"] == "UNKNOWN"  # type: ignore[arg-type]


def test_filled_usd_from_alternate_keys():
    assert classify_order_response({"status": "matched", "making_amount": "12.5"})["filled_usd"] == 12.5
    assert classify_order_response({"status": "matched", "matchedAmount": "7.0"})["filled_usd"] == 7.0


def test_filled_with_unparseable_amount_still_filled_zero_usd():
    """A fill we can't size is still a fill, but filled_usd falls back to 0 here;
    the executor then substitutes intended size. Must not crash."""
    r = classify_order_response({"status": "matched", "makingAmount": "not-a-number"})
    assert r["outcome"] == "FILLED"
    assert r["filled_usd"] == 0.0


def test_status_case_insensitive():
    assert classify_order_response({"status": "MATCHED"})["outcome"] == "FILLED"
    assert classify_order_response({"status": "Unmatched"})["outcome"] == "UNFILLED"
