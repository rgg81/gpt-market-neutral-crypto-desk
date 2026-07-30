import pytest

from futures_fund.config import Settings
from futures_fund.exchange import FuturesExchange, build_ccxt, default_symbol_spec
from futures_fund.market_data import FundingInfo


class _FakeClient:
    markets = {"BTC/USDT:USDT": {"id": "BTCUSDT"}}
    markets_by_id = {"BTCUSDT": {"symbol": "BTC/USDT:USDT", "id": "BTCUSDT"}}

    def market(self, symbol):
        return {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT",
                "precision": {"price": 0.1, "amount": 0.001},
                "limits": {"cost": {"min": 5.0}},
                "info": {"filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"}]}}

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        return [[1700000000000, 70000, 70100, 69900, 70050, 12.0]]

    def fetch_funding_rate(self, symbol):
        return {"symbol": "BTC/USDT:USDT", "fundingRate": "0.0001",
                "fundingTimestamp": 1700000000000, "markPrice": "70050", "indexPrice": "70040"}

    def fetch_funding_interval(self, symbol):
        return {"info": {"fundingIntervalHours": 8}}

    def fetch_order_book(self, symbol, limit):
        return {"bids": [[70040.0, 1.5], [70030.0, 2.0]],
                "asks": [[70060.0, 1.2], [70070.0, 3.0]]}


def test_ccxt_client_is_always_public_even_when_credentials_exist(monkeypatch):
    monkeypatch.setenv("BINANCE_KEY", "must-not-be-used")
    monkeypatch.setenv("BINANCE_SECRET", "must-not-be-used")
    client = build_ccxt(Settings())
    assert not client.apiKey
    assert not client.secret


def test_default_symbol_spec_from_public_filters():
    spec = default_symbol_spec(_FakeClient().market("BTC/USDT:USDT"))
    assert spec.symbol == "BTCUSDT"
    assert spec.tick_size == pytest.approx(0.1)
    assert spec.min_notional == pytest.approx(5.0)
    assert spec.mmr_brackets[0].max_leverage == pytest.approx(20.0)  # conservative paper bracket


def test_keyless_symbol_spec_uses_default_bracket():
    ex = FuturesExchange(_FakeClient(), keyless=True)
    spec = ex.symbol_spec("BTC/USDT:USDT")
    assert len(spec.mmr_brackets) == 1 and spec.mmr_brackets[0].mmr == pytest.approx(0.05)


def test_ohlcv_returns_parsed_frame():
    ex = FuturesExchange(_FakeClient(), keyless=True)
    df = ex.ohlcv("BTC/USDT:USDT", timeframe="4h", limit=1)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df["close"].iloc[0] == pytest.approx(70050.0)


def test_funding_returns_concrete_fundinginfo_with_float_interval():
    ex = FuturesExchange(_FakeClient(), keyless=True)
    info = ex.funding("BTC/USDT:USDT")
    # the per-symbol-interval CONTRACT: exchange.funding() yields a real FundingInfo whose
    # interval_hours (float) is exactly what funding_intervals.funding_interval_hours consumes.
    assert isinstance(info, FundingInfo)
    assert isinstance(info.interval_hours, float)
    assert info.interval_hours == pytest.approx(8.0)
    assert info.current_rate == pytest.approx(0.0001)
    assert ex.mark_price("BTC/USDT:USDT") == pytest.approx(70050.0)


def test_funding_interval_hours_reads_exchange_funding_end_to_end():
    # END-TO-END: funding_intervals.funding_interval_hours pulls the interval straight off the
    # FundingInfo that THIS exchange.funding() produces (no stand-in) — spec §11 wiring proven.
    from futures_fund.funding_intervals import funding_interval_hours
    ex = FuturesExchange(_FakeClient(), keyless=True)
    assert funding_interval_hours("BTC/USDT:USDT", ex) == pytest.approx(8.0)


def test_depth_returns_ask_and_bid_levels():
    ex = FuturesExchange(_FakeClient(), keyless=True)
    book = ex.depth("BTC/USDT:USDT", limit=20)
    # asks (crossing side for a buy) ascending, bids (crossing side for a sell) descending
    assert book["asks"][0] == (70060.0, 1.2)
    assert book["bids"][0] == (70040.0, 1.5)


def test_depth_levels_are_price_qty_tuples():
    ex = FuturesExchange(_FakeClient(), keyless=True)
    book = ex.depth("BTC/USDT:USDT")
    for px, qty in book["asks"] + book["bids"]:
        assert isinstance(px, float) and isinstance(qty, float)


def test_call_retries_transient_ratelimit_then_succeeds():
    # A momentary 429 weight-limit spike must NOT crash the build — _call backs off and retries.
    import ccxt
    ex = FuturesExchange(_FakeClient(), keyless=True)
    ex._retry_base_delay = 0.0  # no real sleeping in tests
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ccxt.RateLimitExceeded("429")
        return "ok"

    assert ex._call(flaky) == "ok"
    assert calls["n"] == 3  # failed twice, succeeded on the third attempt


def test_call_does_not_retry_ddos_ban():
    # A HTTP 418 IP ban must be re-raised IMMEDIATELY — retrying it during the ban window extends
    # the ban (observed live). The orchestrator HALTs and waits it out instead.
    import ccxt
    ex = FuturesExchange(_FakeClient(), keyless=True)
    ex._retry_base_delay = 0.0
    calls = {"n": 0}

    def banned():
        calls["n"] += 1
        raise ccxt.DDoSProtection("418 banned")

    with pytest.raises(ccxt.DDoSProtection):
        ex._call(banned)
    assert calls["n"] == 1  # a ban is never retried


def test_call_gives_up_after_max_transient_retries():
    import ccxt
    ex = FuturesExchange(_FakeClient(), keyless=True)
    ex._retry_base_delay = 0.0
    calls = {"n": 0}

    def always_429():
        calls["n"] += 1
        raise ccxt.RateLimitExceeded("429")

    with pytest.raises(ccxt.RateLimitExceeded):
        ex._call(always_429)
    assert calls["n"] == ex._retry_attempts  # exhausted the bounded budget, then re-raised
