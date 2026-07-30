from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from futures_fund.contracts import SentimentBatch, SentimentReport, SentimentSource


def _utc(h=0):
    return datetime(2026, 6, 1, h, 0, tzinfo=UTC)


def test_sentiment_report_accepts_valid_score():
    r = SentimentReport(symbol="BTC/USDT:USDT", level="positive", s=0.5,
                        confidence=0.8, as_of_ts=_utc(12))
    assert r.s == 0.5
    assert r.decayed_s is None
    assert r.sources == []


def test_sentiment_report_rejects_out_of_range_score():
    with pytest.raises(ValidationError):
        SentimentReport(symbol="BTC/USDT:USDT", level="positive", s=1.5,
                        confidence=0.8, as_of_ts=_utc(12))


def test_sentiment_report_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        SentimentReport(symbol="BTC/USDT:USDT", level="neutral", s=0.0,
                        confidence=1.2, as_of_ts=_utc(12))


def test_sentiment_source_carries_published_ts():
    src = SentimentSource(url="https://x/y", published_ts=_utc(6), title="t", feed="news_rss")
    r = SentimentReport(symbol="MARKET", level="neutral", s=0.0, confidence=0.3,
                        sources=[src], as_of_ts=_utc(12))
    assert r.sources[0].feed == "news_rss"
    assert r.sources[0].published_ts < r.as_of_ts


def test_sentiment_batch_holds_reports():
    batch = SentimentBatch(reports=[
        SentimentReport(symbol="BTC/USDT:USDT", level="very_positive", s=1.0,
                        confidence=0.9, as_of_ts=_utc(12)),
    ])
    assert len(batch.reports) == 1
    assert batch.reports[0].level == "very_positive"


def test_market_report_uses_market_symbol():
    r = SentimentReport(symbol="MARKET", level="negative", s=-0.5, confidence=0.6,
                        as_of_ts=_utc(12) + timedelta(hours=1))
    assert r.symbol == "MARKET" and r.s == -0.5
