import pytest

from futures_fund.market_data import (
    is_crypto_perp,
    liquidity_floor,
    parse_funding,
    parse_ohlcv,
    parse_onboard_date_ms,
    parse_symbol_spec,
    scan_universe,
)

_MARKETS = {
    "BTC/USDT:USDT": {"info": {"underlyingType": "COIN", "contractType": "PERPETUAL"}},
    "DOGE/USDT:USDT": {"info": {"underlyingType": "COIN", "contractType": "PERPETUAL"}},
    "GOLD/USDT:USDT": {"info": {"underlyingType": "COMMODITY",
                                "contractType": "TRADIFI_PERPETUAL"}},
    "SOL/USDT:USDT": {"info": {"underlyingType": "COIN", "contractType": "PERPETUAL"}},
}


class _TickerClient:
    markets = _MARKETS

    def fetch_tickers(self):
        return {
            "BTC/USDT:USDT": {"quoteVolume": 1e10, "percentage": 0.1, "last": 70000.0},
            "GOLD/USDT:USDT": {"quoteVolume": 8e9, "percentage": 0.5, "last": 2300.0},  # TradFi
            "SOL/USDT:USDT": {"quoteVolume": 9e9, "percentage": -1.0, "last": 150.0},
            "DOGE/USDT:USDT": {"quoteVolume": 3e7, "percentage": -2.0, "last": 0.1},  # thin
            "ETH/USDT:USD": {"quoteVolume": 9e9, "percentage": 0.0, "last": 2000.0},  # not perp
        }


# Every non-crypto TradFi wrapper the desk must exclude, one realistic fixture per excluded
# contractType/underlyingType the code enumerates (market_data.py:~61-64). Each ranks HIGH by
# 24h volume on Binance USD-M but must NEVER enter a crypto-only book.
_TRADFI_WRAPPERS = {
    "GOLD/USDT:USDT": {"info": {"underlyingType": "COMMODITY",
                                "contractType": "TRADIFI_PERPETUAL"}},
    "AAPL/USDT:USDT": {"info": {"underlyingType": "EQUITY",
                                "contractType": "TRADIFI_PERPETUAL"}},
    "SAMSUNG/USDT:USDT": {"info": {"underlyingType": "KR_EQUITY",
                                   "contractType": "TRADIFI_PERPETUAL"}},
    "PREIPO/USDT:USDT": {"info": {"underlyingType": "EQUITY",
                                  "contractType": "PREMARKET"}},
    "SPX/USDT:USDT": {"info": {"underlyingType": "INDEX",
                               "contractType": "TRADIFI_PERPETUAL"}},
}


def test_is_crypto_perp_rejects_tradfi_wrapper():
    assert is_crypto_perp(_MARKETS["BTC/USDT:USDT"]) is True
    assert is_crypto_perp(_MARKETS["GOLD/USDT:USDT"]) is False
    assert is_crypto_perp({"info": {}}) is True  # metadata gap -> keep plain perp


@pytest.mark.parametrize("sym", list(_TRADFI_WRAPPERS))
def test_is_crypto_perp_rejects_every_tradfi_underlying(sym):
    # tokenized EQUITY (AAPL), KR_EQUITY (Samsung), PREMARKET pre-IPO, INDEX basket, COMMODITY
    assert is_crypto_perp(_TRADFI_WRAPPERS[sym]) is False
    # a plain crypto COIN perp still passes the same gate
    assert is_crypto_perp(_MARKETS["BTC/USDT:USDT"]) is True


def test_scan_universe_excludes_all_tradfi_wrappers():
    """A normal crypto perp ranks; every tokenized TradFi wrapper (EQUITY/KR_EQUITY/PREMARKET/
    INDEX/COMMODITY) is excluded from the scanned universe despite high 24h volume."""
    markets = {"BTC/USDT:USDT": _MARKETS["BTC/USDT:USDT"], **_TRADFI_WRAPPERS}

    class _Client:
        def fetch_tickers(self):
            t = {"BTC/USDT:USDT": {"quoteVolume": 1e10, "percentage": 0.1, "last": 70000.0}}
            # each wrapper has higher volume than BTC, yet must be filtered out
            for i, s in enumerate(_TRADFI_WRAPPERS):
                t[s] = {"quoteVolume": 9e10 + i, "percentage": 0.0, "last": 100.0}
            return t

    _Client.markets = markets
    rows = scan_universe(_Client(), top_n=10)
    syms = [r["symbol"] for r in rows]
    assert syms == ["BTC/USDT:USDT"]  # only the crypto perp survives
    for s in _TRADFI_WRAPPERS:
        assert s not in syms


# Gold/commodity-PEGGED tokens (spec §1/§20: "no gold coins / metals / commodities"). These are
# classified underlyingType=COIN by Binance (they are tradeable crypto tokens that TRACK a metal),
# so the underlyingType allowlist passes them — an explicit base-symbol denylist must reject them.
_PEGGED_COMMODITY_MARKETS = {
    "PAXG/USDT:USDT": {"base": "PAXG", "symbol": "PAXG/USDT:USDT",
                       "info": {"baseAsset": "PAXG", "underlyingType": "COIN",
                                "contractType": "PERPETUAL"}},   # PAX Gold
    "XAUT/USDT:USDT": {"base": "XAUT", "symbol": "XAUT/USDT:USDT",
                       "info": {"baseAsset": "XAUT", "underlyingType": "COIN",
                                "contractType": "PERPETUAL"}},   # Tether Gold
}


@pytest.mark.parametrize("sym", list(_PEGGED_COMMODITY_MARKETS))
def test_is_crypto_perp_rejects_pegged_commodity_token(sym):
    # PAXG (PAX Gold) / XAUT (Tether Gold) are classified underlyingType=COIN, so they slip past the
    # COIN allowlist — but they are gold-PEGGED and the no-gold-coins mandate (§1/§20) forbids them.
    # The explicit pegged-commodity denylist must reject them while real crypto COIN perps pass.
    assert is_crypto_perp(_PEGGED_COMMODITY_MARKETS[sym]) is False
    assert is_crypto_perp(_MARKETS["BTC/USDT:USDT"]) is True


def test_scan_universe_excludes_pegged_commodity_tokens():
    """PAXG / XAUT rank HIGH by 24h volume on Binance USD-M and are classified COIN, yet the
    gold-pegged denylist must keep them OUT of the scanned universe while BTC/ETH survive."""
    markets = {
        "BTC/USDT:USDT": _MARKETS["BTC/USDT:USDT"],
        "ETH/USDT:USDT": {"info": {"underlyingType": "COIN", "contractType": "PERPETUAL"}},
        **_PEGGED_COMMODITY_MARKETS,
    }

    class _Client:
        def fetch_tickers(self):
            t = {
                "BTC/USDT:USDT": {"quoteVolume": 1e10, "percentage": 0.1, "last": 70000.0},
                "ETH/USDT:USDT": {"quoteVolume": 5e9, "percentage": 0.0, "last": 2000.0},
            }
            # each gold-pegged token out-ranks BTC by 24h volume, yet must be filtered out
            for i, s in enumerate(_PEGGED_COMMODITY_MARKETS):
                t[s] = {"quoteVolume": 9e10 + i, "percentage": 0.0, "last": 2300.0}
            return t

    _Client.markets = markets
    rows = scan_universe(_Client(), top_n=10)
    syms = [r["symbol"] for r in rows]
    assert syms == ["BTC/USDT:USDT", "ETH/USDT:USDT"]  # only the real crypto perps survive
    for s in _PEGGED_COMMODITY_MARKETS:
        assert s not in syms


def test_scan_universe_drops_tradfi_and_non_perp():
    rows = scan_universe(_TickerClient(), top_n=10)
    syms = [r["symbol"] for r in rows]
    assert "GOLD/USDT:USDT" not in syms      # tokenized commodity excluded
    assert "ETH/USDT:USD" not in syms        # not a USDT perp
    assert syms == ["BTC/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT"]  # vol-ranked


def test_liquidity_floor_trims_thin_names_and_caps_top_n():
    rows = scan_universe(_TickerClient(), top_n=10)
    kept = liquidity_floor(rows, min_adv_usd=5e7, symbol_count=30)
    syms = [r["symbol"] for r in kept]
    assert syms == ["BTC/USDT:USDT", "SOL/USDT:USDT"]  # DOGE (3e7) below the 5e7 floor


def test_liquidity_floor_caps_to_symbol_count():
    rows = [{"symbol": f"X{i}/USDT:USDT", "vol_24h_usd": 1e9 - i, "last": 1.0,
             "chg_24h_pct": 0.0} for i in range(40)]
    kept = liquidity_floor(rows, min_adv_usd=0.0, symbol_count=30)
    assert len(kept) == 30
    assert kept[0]["symbol"] == "X0/USDT:USDT"  # most liquid first preserved


def test_parse_ohlcv_sorts_and_labels_columns():
    df = parse_ohlcv([[1700000000000, 1, 2, 0.5, 1.5, 10],
                      [1699999996400, 1, 2, 0.5, 1.5, 10]])
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df["timestamp"].is_monotonic_increasing


def test_parse_funding_defaults_interval_8h():
    fr = {"symbol": "BTC/USDT:USDT", "fundingRate": "0.0001",
          "fundingTimestamp": 1700000000000, "markPrice": "70000", "indexPrice": "69990"}
    info = parse_funding(fr)
    assert info.interval_hours == pytest.approx(8.0)
    assert info.current_rate == pytest.approx(0.0001)
    assert info.mark_price == pytest.approx(70000.0)


def test_parse_funding_sources_interval_when_present():
    fr = {"symbol": "SOL/USDT:USDT", "fundingRate": "0.0002",
          "fundingTimestamp": 1700000000000, "markPrice": "150", "indexPrice": "149.9"}
    info = parse_funding(fr, {"info": {"fundingIntervalHours": 4}})
    assert info.interval_hours == pytest.approx(4.0)


def test_parse_symbol_spec_maps_filters_and_brackets():
    market = {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT",
              "precision": {"price": 0.1, "amount": 0.001},
              "limits": {"cost": {"min": 100.0}},
              "info": {"filters": [
                  {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                  {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                  {"filterType": "MIN_NOTIONAL", "notional": "5.0"}]}}
    tiers = [{"minNotional": 0, "maxNotional": 50000, "maintenanceMarginRate": 0.004,
              "maxLeverage": 125, "info": {"cum": "0"}}]
    spec = parse_symbol_spec(market, tiers)
    assert spec.tick_size == pytest.approx(0.1)
    assert spec.step_size == pytest.approx(0.001)
    assert spec.min_notional == pytest.approx(5.0)
    assert spec.mmr_brackets[0].max_leverage == pytest.approx(125.0)


class _FakeOnboardClient:
    markets = {
        "BTC/USDT:USDT": {"info": {"underlyingType": "COIN", "onboardDate": "1567965300000"}},
        "NEW/USDT:USDT": {"info": {"underlyingType": "COIN"}},  # no onboardDate -> None
    }

    def fetch_tickers(self):
        return {
            "BTC/USDT:USDT": {"last": 60000.0, "quoteVolume": 2e9, "percentage": 1.0},
            "NEW/USDT:USDT": {"last": 1.0, "quoteVolume": 1e9, "percentage": 130.0},
        }


def test_scan_universe_carries_onboard_date_ms_int_or_none():
    rows = scan_universe(_FakeOnboardClient(), top_n=10)
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["BTC/USDT:USDT"]["onboard_date"] == 1567965300000
    assert by_sym["NEW/USDT:USDT"]["onboard_date"] is None
    # existing fields unchanged
    assert by_sym["NEW/USDT:USDT"]["chg_24h_pct"] == 130.0


def test_parse_onboard_date_ms_parses_string_epoch():
    assert parse_onboard_date_ms({"info": {"onboardDate": "1567965300000"}}) == 1567965300000


def test_parse_onboard_date_ms_none_when_absent_or_unparseable():
    assert parse_onboard_date_ms({"info": {}}) is None
    assert parse_onboard_date_ms({}) is None
    assert parse_onboard_date_ms(None) is None
    assert parse_onboard_date_ms({"info": {"onboardDate": None}}) is None
    assert parse_onboard_date_ms({"info": {"onboardDate": "not-a-number"}}) is None
