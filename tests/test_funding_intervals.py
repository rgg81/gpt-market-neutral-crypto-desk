from datetime import UTC

import pytest

from futures_fund.funding_intervals import (
    _MAJORS,
    MAJOR_CAP,
    PER_SYMBOL_CAP_DEFAULT,
    bounded_apr,
    clamp_funding_rate,
    funding_apr,
    funding_cap,
    funding_interval_hours,
    intervals_per_year,
    realized_funding,
)


class _FakeFundingInfo:
    """Stand-in for market_data.FundingInfo — only the `.interval_hours: float` field is read by
    funding_interval_hours (see test_funding_interval_consumes_fundinginfo_interval_hours, which
    ties this to the CONCRETE FundingInfo produced by exchange.funding() in Task 7)."""
    def __init__(self, interval_hours):
        self.interval_hours = interval_hours


class _FakeExchange:
    def __init__(self, hours):
        self._hours = hours

    def funding(self, symbol):
        if self._hours is None:
            raise RuntimeError("no funding info")
        return _FakeFundingInfo(self._hours)


def test_majors_set_contains_btc_and_eth():
    assert _MAJORS == frozenset({"BTC/USDT:USDT", "ETH/USDT:USDT"})


def test_interval_sourced_per_symbol():
    assert funding_interval_hours("SOL/USDT:USDT", _FakeExchange(4.0)) == pytest.approx(4.0)


def test_interval_defaults_to_8h_on_miss():
    assert funding_interval_hours("SOL/USDT:USDT", _FakeExchange(None)) == pytest.approx(8.0)


def test_funding_interval_consumes_fundinginfo_interval_hours():
    # END-TO-END WIRING (spec §11 / contract §2.3 "replace hardcoded 8h, source per-symbol"):
    # the interval funding_intervals consumes is EXACTLY the float on the FundingInfo that the
    # real exchange.funding() returns. Build a real FundingInfo (as parse_funding/exchange.funding
    # do) and feed it through a tiny exchange shim — funding_interval_hours reads .interval_hours.
    from datetime import datetime

    from futures_fund.market_data import FundingInfo

    info = FundingInfo(symbol="SOL/USDT:USDT", current_rate=0.0002,
                       next_funding_ts=datetime(2026, 6, 1, tzinfo=UTC),
                       interval_hours=4.0, mark_price=150.0, index_price=149.9)

    class _RealInfoExchange:
        def funding(self, symbol):
            return info

    assert funding_interval_hours("SOL/USDT:USDT", _RealInfoExchange()) == pytest.approx(4.0)


def test_funding_cap_majors_vs_alts():
    assert funding_cap("BTC/USDT:USDT") == pytest.approx(MAJOR_CAP) == pytest.approx(0.003)
    assert funding_cap("ETH/USDT:USDT") == pytest.approx(0.003)
    assert funding_cap("SOL/USDT:USDT") == pytest.approx(PER_SYMBOL_CAP_DEFAULT) == \
        pytest.approx(0.02)


def test_clamp_is_sign_preserving_and_bounded():
    # alt rate beyond +2% cap -> clamped to +0.02, sign kept
    assert clamp_funding_rate("SOL/USDT:USDT", 0.05) == pytest.approx(0.02)
    # negative beyond -2% -> -0.02
    assert clamp_funding_rate("SOL/USDT:USDT", -0.05) == pytest.approx(-0.02)
    # BTC small negative rate stays signed, never zeroed
    assert clamp_funding_rate("BTC/USDT:USDT", -0.0001) == pytest.approx(-0.0001)
    # BTC beyond +0.30% -> +0.003
    assert clamp_funding_rate("BTC/USDT:USDT", 0.01) == pytest.approx(0.003)


def test_intervals_per_year_for_8h_is_1095():
    assert intervals_per_year(8.0) == pytest.approx(24.0 / 8.0 * 365.0)


def test_funding_apr_is_signed_annualized():
    # +0.01% per 8h -> APR = 0.0001 * 1095 = 0.1095
    assert funding_apr(0.0001, 8.0) == pytest.approx(0.0001 * 1095.0)
    # negative rate -> negative APR (signed carry)
    assert funding_apr(-0.0001, 8.0) == pytest.approx(-0.0001 * 1095.0)


def test_realized_funding_short_receives_positive_rate():
    # short with positive rate RECEIVES: -side*mark*qty*rate, side(short)=-1 -> positive credit
    # contribution to BALANCE is positive (credit)
    bal = realized_funding(notional_signed=-10_000.0, mark=100.0, qty=100.0,
                           rate=0.0001, direction="short")
    assert bal == pytest.approx(+1.0)


def test_realized_funding_long_pays_positive_rate():
    bal = realized_funding(notional_signed=10_000.0, mark=100.0, qty=100.0,
                           rate=0.0001, direction="long")
    assert bal == pytest.approx(-1.0)


def test_realized_funding_ignores_notional_signed():
    # §11 dead-arg pin: notional_signed is accepted for call-site symmetry but UNUSED — the
    # contribution is derived from mark*qty. A deliberately-wrong notional_signed=0.0 must NOT
    # change the result (still -side*mark*qty*rate), so a caller cannot desync funding_amount.
    bal = realized_funding(notional_signed=0.0, mark=100.0, qty=100.0,
                           rate=0.0001, direction="short")
    assert bal == pytest.approx(+1.0)


def test_clamp_then_realized_composition_for_an_alt():
    # §11 / contract §2.3 ORDERING: cap the RATE upstream, then realized consumes the SIGNED,
    # clamped rate and stays signed. SOL raw +0.05 exceeds the +0.02 alt cap -> clamped to +0.02;
    # a SHORT then RECEIVES a credit on the clamped rate: -(-1)*mark*qty*0.02 = +mark*qty*0.02.
    raw_rate = 0.05
    clamped = clamp_funding_rate("SOL/USDT:USDT", raw_rate)
    assert clamped == pytest.approx(0.02)
    bal = realized_funding(notional_signed=-15_000.0, mark=150.0, qty=100.0,
                           rate=clamped, direction="short")
    assert bal == pytest.approx(+150.0 * 100.0 * 0.02)   # +300.0 credit, signed, on clamped rate
    # and the realized contribution is NOT the raw (unclamped) rate's value:
    assert bal != pytest.approx(150.0 * 100.0 * raw_rate)


def test_bounded_apr_sign_preserving_clamp():
    assert bounded_apr(20.0, 2.0) == 2.0
    assert bounded_apr(-20.0, 2.0) == -2.0
    assert bounded_apr(1.5, 2.0) == 1.5     # inside the band: unchanged
    assert bounded_apr(-1.5, 2.0) == -1.5


def test_bounded_apr_none_cap_is_unbounded():
    assert bounded_apr(20.0, None) == 20.0
    assert bounded_apr(-20.0, None) == -20.0
