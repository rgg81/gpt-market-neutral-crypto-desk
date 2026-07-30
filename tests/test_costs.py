from datetime import UTC, datetime

import pytest

from futures_fund.costs import (
    MAKER_RATE,
    TAKER_RATE,
    count_funding_events,
    project_funding,
    round_trip_fee,
    slippage_cost,
    trade_fee,
    vwap_fill,
)


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_fee_rates_are_5bps_taker_2bps_maker():
    assert TAKER_RATE == pytest.approx(0.0005)
    assert MAKER_RATE == pytest.approx(0.0002)


def test_taker_fee_is_5bps_of_notional():
    assert trade_fee(notional=10_000.0, maker=False) == pytest.approx(5.0)


def test_maker_fee_is_2bps_of_notional():
    assert trade_fee(notional=10_000.0, maker=True) == pytest.approx(2.0)


def test_round_trip_taker_in_and_out():
    assert round_trip_fee(10_000.0, maker_entry=False, maker_exit=False) == pytest.approx(10.0)


def test_count_funding_events_crossing_two_boundaries():
    n = count_funding_events(_utc(2026, 5, 29, 7, 0), _utc(2026, 5, 29, 17, 0))
    assert n == 2


def test_project_funding_short_receives_positive_rate():
    # short with a positive funding rate RECEIVES funding -> negative cost (a credit)
    cost = project_funding(notional=10_000.0, funding_rate=0.0001, direction="short", n_events=3)
    assert cost == pytest.approx(-3.0)


def test_project_funding_long_pays_positive_rate():
    cost = project_funding(notional=10_000.0, funding_rate=0.0001, direction="long", n_events=3)
    assert cost == pytest.approx(3.0)


def test_vwap_fill_walks_the_book():
    filled, vwap = vwap_fill([(100.0, 1.0), (101.0, 1.0)], qty=1.5)
    assert filled == pytest.approx(1.5)
    assert vwap == pytest.approx((100.0 * 1.0 + 101.0 * 0.5) / 1.5)


def test_slippage_cost_is_filled_times_abs_vwap_gap():
    cost = slippage_cost([(100.0, 1.0), (101.0, 1.0)], qty=1.5, reference_price=100.0)
    filled, vwap = vwap_fill([(100.0, 1.0), (101.0, 1.0)], qty=1.5)
    assert cost == pytest.approx(filled * abs(vwap - 100.0))


def test_vwap_fill_empty_book_is_zero():
    assert vwap_fill([], 1.0) == (0.0, 0.0)


def test_vwap_fill_zero_qty_is_zero():
    assert vwap_fill([(100.0, 1.0)], 0) == (0.0, 0.0)


def test_slippage_cost_empty_book_is_zero():
    assert slippage_cost([], 1.0, 100.0) == 0.0


def test_vwap_fill_partial_when_qty_exceeds_book_depth():
    # request 5 units against a book holding only 1.0 + 2.0 = 3.0 total -> partial fill
    levels = [(100.0, 1.0), (101.0, 2.0)]
    filled, vwap = vwap_fill(levels, qty=5.0)
    assert filled == pytest.approx(3.0)   # capped at total available depth
    assert filled < 5.0                   # strictly less than requested
    # vwap over the fully-consumed book: (100*1 + 101*2) / 3
    assert vwap == pytest.approx((100.0 * 1.0 + 101.0 * 2.0) / 3.0)
