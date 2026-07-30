from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from futures_fund import models
from futures_fund.contracts import (
    CoinGeometry,
    GeometryBundle,
    Pair,
    SentimentBatch,
    SentimentReport,
    SentimentSource,
    SleeveSignal,
    SleeveTilt,
    Spread,
)

_NOW = datetime(2026, 6, 11, 0, 0, tzinfo=UTC)


def test_sleeve_name_alias_values():
    assert set(typing.get_args(models.SleeveName)) == {
        "carry", "pairs", "factor", "sentiment", "discretionary"}


def test_sentiment_level_alias_values():
    assert set(typing.get_args(models.SentimentLevel)) == {
        "very_positive", "positive", "neutral", "negative", "very_negative",
    }


def test_spread_state_alias_values():
    assert set(typing.get_args(models.SpreadState)) == {
        "flat", "long_spread", "short_spread", "stop",
    }


def test_pair_test_method_alias_values():
    assert set(typing.get_args(models.PairTestMethod)) == {"engle_granger", "johansen"}


def test_cadence_alias_values():
    assert set(typing.get_args(models.Cadence)) == {"weekly", "daily", "rebal"}


def test_sentiment_report_valid():
    r = SentimentReport(
        symbol="BTC/USDT:USDT",
        level="positive",
        s=0.5,
        confidence=0.8,
        sources=[SentimentSource(url="http://x", published_ts=_NOW - timedelta(hours=2))],
        rationale="ETF inflows",
        as_of_ts=_NOW,
    )
    assert r.s == 0.5
    assert r.decayed_s is None
    assert r.sources[0].feed == ""


def test_sentiment_report_s_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SentimentReport(symbol="BTC/USDT:USDT", level="positive", s=1.5,
                        confidence=0.8, as_of_ts=_NOW)


def test_sentiment_report_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SentimentReport(symbol="BTC/USDT:USDT", level="neutral", s=0.0,
                        confidence=1.5, as_of_ts=_NOW)


def test_sentiment_batch_defaults_empty():
    b = SentimentBatch()
    assert b.reports == []


def test_sentiment_models_strict_by_default():
    # Canonical contract (PART 1): these models are "strict-by-default (no extra='allow')",
    # i.e. unexpected/typo'd fields must be REJECTED, not silently dropped.
    with pytest.raises(ValidationError):
        SentimentSource(url="http://x", published_ts=_NOW, bogus=1)
    with pytest.raises(ValidationError):
        SentimentReport(symbol="BTC/USDT:USDT", level="neutral", s=0.0,
                        confidence=0.0, as_of_ts=_NOW, typo_field=1)
    with pytest.raises(ValidationError):
        SentimentBatch(reports=[], extra_field=1)


def test_coin_geometry_defaults():
    g = CoinGeometry(symbol="ETH/USDT:USDT", mark=3000.0)
    assert g.beta_btc == 1.0
    assert g.beta_lookback_days == 45
    assert g.funding_interval_hours == 8.0
    assert g.funding_cap == 0.02
    assert g.in_pair is False
    assert g.pair_id is None
    assert g.sentiment_score == 0.0
    assert g.sentiment_conf == 0.0
    assert g.spec is None


def test_coin_geometry_sentiment_range_enforced():
    with pytest.raises(ValidationError):
        CoinGeometry(symbol="ETH/USDT:USDT", mark=3000.0, sentiment_score=2.0)


def test_geometry_bundle_holds_geometries():
    b = GeometryBundle(
        geometries=[CoinGeometry(symbol="BTC/USDT:USDT", mark=60000.0)],
        as_of_ts=_NOW,
    )
    assert b.geometries[0].symbol == "BTC/USDT:USDT"


def _pair() -> Pair:
    return Pair(
        pair_id="BTCUSDT__ETHUSDT",
        symbol_y="BTC/USDT:USDT",
        symbol_x="ETH/USDT:USDT",
        hedge_ratio=15.0,
        method="engle_granger",
        adf_pvalue=0.01,
        half_life=5.0,
        theta=0.139,
        mu=0.0,
        sigma_eq=200.0,
        formed_cycle=3,
    )


def test_pair_defaults():
    p = _pair()
    assert p.cointegrated is True
    assert p.adf_pvalue_adj is None
    assert p.johansen_trace_stat is None


def test_spread_defaults():
    s = Spread(pair_id="BTCUSDT__ETHUSDT", spread_value=400.0, zscore=2.0, state="long_spread")
    assert s.entry_z == 2.0
    assert s.exit_z == 0.0
    assert s.stop_z == 3.0
    assert s.realized_pnl == 0.0
    assert s.qty_y == 0.0


def test_sleeve_tilt_defaults():
    t = SleeveTilt(symbol="BTC/USDT:USDT", direction="long", target_weight=0.2)
    assert t.raw_score == 0.0
    assert t.pair_id is None


def test_sleeve_signal_valid():
    sig = SleeveSignal(
        sleeve="carry",
        tilts=[SleeveTilt(symbol="BTC/USDT:USDT", direction="short", target_weight=-0.3)],
        risk_budget_frac=0.25,
        as_of_ts=_NOW,
    )
    assert sig.sleeve == "carry"
    assert sig.tilts[0].direction == "short"
    assert sig.diagnostics == {}


def test_sleeve_signal_risk_budget_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SleeveSignal(sleeve="carry", risk_budget_frac=1.5, as_of_ts=_NOW)
