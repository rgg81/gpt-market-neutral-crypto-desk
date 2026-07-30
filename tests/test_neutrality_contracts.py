from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from futures_fund.contracts import (
    CoinGeometry,
    GeometryBundle,
    SleeveSignal,
    SleeveTilt,
    TargetWeights,
    WeightLeg,
)

NOW = datetime(2026, 6, 11, tzinfo=UTC)


def test_coin_geometry_defaults_and_sentiment_bounds():
    g = CoinGeometry(symbol="BTC/USDT:USDT", mark=60000.0)
    assert g.beta_btc == 1.0
    assert g.beta_lookback_days == 45
    assert g.funding_interval_hours == 8.0
    assert g.sentiment_score == 0.0
    assert g.sentiment_conf == 0.0
    assert g.in_pair is False


def test_coin_geometry_rejects_out_of_range_sentiment():
    with pytest.raises(ValidationError):
        CoinGeometry(symbol="ETH/USDT:USDT", mark=3000.0, sentiment_score=1.5)
    with pytest.raises(ValidationError):
        CoinGeometry(symbol="ETH/USDT:USDT", mark=3000.0, sentiment_conf=-0.1)


def test_geometry_bundle_holds_list():
    b = GeometryBundle(
        geometries=[CoinGeometry(symbol="BTC/USDT:USDT", mark=60000.0)], as_of_ts=NOW
    )
    assert len(b.geometries) == 1
    assert b.as_of_ts == NOW


def test_sleeve_signal_budget_bounds():
    tilt = SleeveTilt(symbol="ETH/USDT:USDT", direction="short", target_weight=-0.2)
    s = SleeveSignal(sleeve="carry", tilts=[tilt], risk_budget_frac=0.25, as_of_ts=NOW)
    assert s.sleeve == "carry"
    assert s.tilts[0].direction == "short"
    with pytest.raises(ValidationError):
        SleeveSignal(sleeve="carry", risk_budget_frac=1.5, as_of_ts=NOW)


def test_target_weights_assembles_residual_fields():
    leg = WeightLeg(
        symbol="BTC/USDT:USDT",
        direction="long",
        weight=0.45,
        target_notional=9000.0,
        beta_btc=1.0,
        sleeve="factor",
    )
    tw = TargetWeights(
        legs=[leg],
        btc_hedge_notional=-500.0,
        dollar_residual=0.0,
        dollar_residual_frac=0.0,
        beta_residual=0.01,
        gross_long=9000.0,
        gross_short=9000.0,
        deploy_long_frac=0.9,
        deploy_short_frac=0.9,
        gross_notional=18000.0,
        as_of_ts=NOW,
    )
    assert tw.feasible is True
    assert tw.turnover_l1 == 0.0
    assert tw.legs[0].sleeve == "factor"
    assert tw.btc_hedge_notional == -500.0


def test_weight_leg_allows_hedge_sleeve_literal():
    leg = WeightLeg(
        symbol="BTC/USDT:USDT",
        direction="short",
        weight=-0.05,
        target_notional=-1000.0,
        beta_btc=1.0,
        sleeve="hedge",
    )
    assert leg.sleeve == "hedge"
