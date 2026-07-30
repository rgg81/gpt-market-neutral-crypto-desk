import math

import pytest

from futures_fund.slippage import (
    DEFAULT_K,
    depth_slippage,
    estimate_slippage,
    fallback_slippage,
    slippage_bps,
)

# Representative BTC depth proxy: with the config default k=0.1 and half_spread=1.0 bps, this
# adv_usd makes the fallback yield EXACTLY 1.25 bps @ $1M (spec §11 BTC anchor):
#   1.25 = 1.0 + 0.1*sqrt(1e6/adv)*1e4  ->  sqrt(1e6/adv) = 2.5e-4  ->  adv = 1.6e13
_BTC_ADV_USD = 1.6e13
_BTC_HALF_SPREAD_BPS = 1.0


def test_default_k_is_point_one():
    assert DEFAULT_K == pytest.approx(0.1)


def test_depth_slippage_matches_vwap_gap():
    # buying 1.5 units: 1.0 @ 100, 0.5 @ 101 -> vwap = 100.333..., ref = 100
    levels = [(100.0, 1.0), (101.0, 1.0)]
    cost = depth_slippage(levels, qty=1.5, reference_price=100.0)
    vwap = (100.0 * 1.0 + 101.0 * 0.5) / 1.5
    assert cost == pytest.approx(1.5 * abs(vwap - 100.0))


def test_fallback_slippage_half_spread_plus_sqrt_impact():
    # notional = 1e6, adv = 1e9, half_spread = 1 bps, k = 0.1
    # cost_bps = 1.0 + 0.1*sqrt(1e6/1e9)*1e4 = 1.0 + 0.1*0.0316...*1e4
    notional, adv, hs_bps = 1_000_000.0, 1_000_000_000.0, 1.0
    cost = fallback_slippage(notional, adv, hs_bps, k=0.1)
    expected_bps = hs_bps + 0.1 * math.sqrt(notional / adv) * 1e4
    assert cost == pytest.approx(expected_bps / 1e4 * notional)


def test_fallback_zero_adv_returns_half_spread_only():
    cost = fallback_slippage(1_000_000.0, 0.0, 1.0, k=0.1)
    assert cost == pytest.approx(1.0 / 1e4 * 1_000_000.0)


def test_fallback_btc_1m_anchor_is_about_1_25_bps():
    # §11 BTC anchor PINNED: ~1.25 bps @ $1M against the representative BTC depth proxy + k=0.1.
    cost = fallback_slippage(1_000_000.0, _BTC_ADV_USD, _BTC_HALF_SPREAD_BPS, k=DEFAULT_K)
    assert slippage_bps(cost, 1_000_000.0) == pytest.approx(1.25, rel=1e-6)


def test_fallback_is_strictly_monotone_in_notional():
    # §11 requires impact to GROW with size (a larger clip costs more bps) — pin monotonicity at
    # the $1M and $5M points instead of asserting the unsatisfiable second anchor exactly.
    bps_1m = slippage_bps(
        fallback_slippage(1_000_000.0, _BTC_ADV_USD, _BTC_HALF_SPREAD_BPS, k=DEFAULT_K),
        1_000_000.0)
    bps_5m = slippage_bps(
        fallback_slippage(5_000_000.0, _BTC_ADV_USD, _BTC_HALF_SPREAD_BPS, k=DEFAULT_K),
        5_000_000.0)
    assert bps_5m > bps_1m  # $5M strictly costlier per-bp than $1M


def test_estimate_prefers_depth_when_present():
    levels = [(100.0, 100.0)]  # deep enough to fill at the ref price -> ~0 slippage
    cost = estimate_slippage("BTC/USDT:USDT", qty=1.0, reference_price=100.0,
                             depth=levels, adv_usd=1e9, half_spread_bps=1.0)
    assert cost == pytest.approx(0.0)


def test_estimate_falls_back_when_no_depth():
    cost = estimate_slippage("SOL/USDT:USDT", qty=10_000.0, reference_price=100.0,
                             depth=None, adv_usd=1e9, half_spread_bps=1.0, k=0.1)
    notional = 10_000.0 * 100.0
    assert cost == pytest.approx(fallback_slippage(notional, 1e9, 1.0, k=0.1))


def test_estimate_never_flat_two_bps():
    # a tiny fill against a deep book is essentially free, NOT a flat 2bps
    levels = [(100.0, 1_000_000.0)]
    cost = estimate_slippage("BTC/USDT:USDT", qty=1.0, reference_price=100.0,
                             depth=levels, adv_usd=1e12, half_spread_bps=1.0)
    assert cost < 0.02 * 100.0  # far below a flat 2bps of the 100 USDT notional


def test_slippage_bps_converts_cost_to_bps():
    assert slippage_bps(cost_usdt=125.0, notional=1_000_000.0) == pytest.approx(1.25)
    assert slippage_bps(cost_usdt=10.0, notional=0.0) == 0.0
