from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from futures_fund.evidence import EvidencePack, build_evidence

NOW = datetime(2026, 7, 7, tzinfo=UTC)
BTC = "BTC/USDT:USDT"


class _FakeEx:
    """Fake exchange whose columns match the REAL parse_* schemas (oi_value / long_short_ratio),
    so a test can never mask an evidence column-name bug again."""

    def mark_price(self, s):
        return 100.0

    def ohlcv(self, s, timeframe="1h", limit=200):
        close = 100.0 * np.exp(np.cumsum(np.full(60, 0.001)))  # gently trending
        return pd.DataFrame({"close": close})

    def funding(self, s):
        from futures_fund.market_data import FundingInfo

        return FundingInfo(
            symbol=s,
            current_rate=0.0001,
            next_funding_ts=NOW,
            interval_hours=8.0,
            mark_price=100.0,
            index_price=99.9,
        )

    def open_interest_history(self, s, **k):
        return pd.DataFrame({"timestamp": [1, 2, 3], "oi_amount": [1e4, 1.1e4, 1.2e4],
                             "oi_value": [1e6, 1.1e6, 1.2e6]})

    def long_short_ratio(self, s, **k):
        return pd.DataFrame({"timestamp": [1, 2], "long_short_ratio": [1.0, 1.2],
                             "long_account": [0.6, 0.55], "short_account": [0.4, 0.45]})

    def depth(self, s):
        return {
            "bids": [(99.9, 1_000_000.0)],
            "asks": [(100.1, 1_000_000.0)],
        }


def _by_symbol(packs):
    return {p.symbol: p for p in packs}


def test_build_evidence_assembles_per_coin_fields():
    packs = build_evidence(_FakeEx(), ["SOL/USDT:USDT"], now=NOW, btc_symbol=BTC)
    # BTC is always included (hedge mark + beta reference), so SOL + BTC.
    by = _by_symbol(packs)
    assert set(by) == {"SOL/USDT:USDT", BTC}
    p = by["SOL/USDT:USDT"]
    assert isinstance(p, EvidencePack)
    assert p.mark == 100.0
    assert p.funding_rate == 0.0001
    assert p.momentum_pct > 0                      # trending up
    assert p.realized_vol >= 0.0
    assert p.open_interest == 1.2e6                 # latest oi_value
    assert p.oi_change_pct > 0                      # OI rose
    assert p.long_short_ratio == 1.2               # latest long_short_ratio
    assert p.beta_btc == pytest.approx(1.0)        # identical series -> beta 1.0
    assert p.liquidity_mid == pytest.approx(100.0)


def test_build_evidence_is_fail_soft_per_coin():
    class _Broken(_FakeEx):
        def funding(self, s):
            raise RuntimeError("boom")

    packs = build_evidence(_Broken(), ["X/USDT:USDT"], now=NOW, btc_symbol=BTC)
    by = _by_symbol(packs)
    assert by["X/USDT:USDT"].funding_rate == 0.0    # missing datum -> neutral default, not a crash


def test_slippage_curve_floored_at_half_spread():
    """Cycle-17 regression: a deep book gives ~0 depth-walk impact, but crossing the book still
    costs the HALF-SPREAD. The curve must never read below it, else break-even math prices a
    rotation ~5x too cheap (ADA: 0.0 walk, 6.08bps spread -> real fill cost $20+ on $4,300)."""
    from futures_fund.evidence import _slip_bps_at

    # a very deep single level right at the mark -> depth-walk cost ~0
    deep_asks = [(1.0, 10_000_000.0)]
    no_spread = _slip_bps_at("X/USDT:USDT", 1.0, deep_asks, 4300.0, half_spread_bps=0.0)
    floored = _slip_bps_at("X/USDT:USDT", 1.0, deep_asks, 4300.0, half_spread_bps=3.042)
    assert no_spread < 0.5                          # deep book -> negligible walk cost
    assert floored >= 3.042                         # ...but never below the half-spread
    # empty-book fallback still returns the floor, not 0
    empty = _slip_bps_at("X/USDT:USDT", 1.0, [], 4300.0, half_spread_bps=3.042)
    assert empty == pytest.approx(3.042)


def test_liquidity_curve_uses_same_book_mid_not_an_earlier_funding_mark():
    """Cycle-3 regression: an earlier mark must not make a deep fresh book look illiquid.

    The decision/funding mark is 90, while the L2 book is 99.9/100.1. Liquidity is the cost of
    crossing that book from its own 100 midpoint: 10bps, not the ~1,122bps distance from 90.
    """

    class _EarlierFundingMark(_FakeEx):
        def funding(self, s):
            from futures_fund.market_data import FundingInfo

            return FundingInfo(
                symbol=s,
                current_rate=0.0001,
                next_funding_ts=NOW,
                interval_hours=8.0,
                mark_price=90.0,
                index_price=90.0,
            )

    packs = build_evidence(
        _EarlierFundingMark(),
        ["SOL/USDT:USDT"],
        now=NOW,
        btc_symbol=BTC,
    )
    row = _by_symbol(packs)["SOL/USDT:USDT"]
    assert row.mark == pytest.approx(90.0)
    assert row.liquidity_mid == pytest.approx(100.0)
    assert row.spread_bps == pytest.approx(20.0)
    assert row.est_slippage_bps_2k == pytest.approx(10.0)
    assert row.slippage_curve_bps == {"2k": 10.0, "5k": 10.0, "10k": 10.0}


def test_liquidity_curve_uses_worse_crossing_side():
    from futures_fund.evidence import _depth_fields

    class _AsymmetricBook:
        def depth(self, s):
            return {
                "bids": [(99.9, 1_000_000.0)],
                "asks": [(100.1, 1.0), (101.0, 1_000_000.0)],
            }

    _, _, mid, _, _, curve = _depth_fields(_AsymmetricBook(), "X/USDT:USDT")
    assert mid == pytest.approx(100.0)
    assert curve["2k"] > 10.0


def test_build_evidence_derives_mark_from_funding_without_mark_price():
    """Dedup (rate-limit fix): funding carries the mark, so a valid mark must be produced even if
    the dedicated mark_price() endpoint is unavailable — proving the redundant call was removed."""
    class _NoMarkPrice(_FakeEx):
        def mark_price(self, s):  # must NOT be needed when funding already carries the mark
            raise RuntimeError("mark_price endpoint should not be called")

    packs = build_evidence(_NoMarkPrice(), ["SOL/USDT:USDT"], now=NOW, btc_symbol=BTC)
    by = _by_symbol(packs)
    assert by["SOL/USDT:USDT"].mark == pytest.approx(100.0)  # taken from funding's mark_price
