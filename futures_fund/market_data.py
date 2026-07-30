from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel, Field

from futures_fund.models import MmrBracket, SymbolSpec


class FundingInfo(BaseModel):
    symbol: str
    current_rate: float = Field(
        description="Current (last) funding rate, NOT a prediction "
        "(ccxt fundingRate == Binance lastFundingRate)."
    )
    next_funding_ts: datetime
    interval_hours: float
    mark_price: float
    index_price: float


def _filter_field(filters: list[dict], filter_type: str, field: str) -> float | None:
    for f in filters:
        if f.get("filterType") == filter_type and field in f:
            return float(f[field])
    return None


def parse_onboard_date_ms(market: dict | None) -> int | None:
    """Binance listing timestamp (ms-epoch) from a ccxt market dict's `info.onboardDate`, or None.

    The single home for the onboard-date parsing rule: `info.onboardDate` is a string after
    load_markets(); coerce to int, fail-soft to None when absent/None/unparseable. Both the
    universe scan and any age-fallback consumer share this one parser so the rule never drifts."""
    raw = ((market or {}).get("info") or {}).get("onboardDate")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def parse_symbol_spec(market: dict, tiers: list[dict]) -> SymbolSpec:
    """ccxt market dict + leverage tiers -> SymbolSpec, preferring exchangeInfo filters."""
    filters = (market.get("info") or {}).get("filters") or []
    tick = _filter_field(filters, "PRICE_FILTER", "tickSize")
    step = _filter_field(filters, "LOT_SIZE", "stepSize")
    min_notional = _filter_field(filters, "MIN_NOTIONAL", "notional")
    if tick is None:
        tick = float(market["precision"]["price"])
    if step is None:
        step = float(market["precision"]["amount"])
    if min_notional is None:
        min_notional = float(market["limits"]["cost"]["min"])
    brackets = [
        MmrBracket(
            notional_floor=float(t["minNotional"]),
            notional_cap=float(t["maxNotional"]),
            mmr=float(t["maintenanceMarginRate"]),
            maint_amount=float(t["info"]["cum"]),
            max_leverage=float(t["maxLeverage"]),
        )
        for t in tiers
    ]
    return SymbolSpec(
        symbol=market["id"],
        tick_size=tick,
        step_size=step,
        min_notional=min_notional,
        mmr_brackets=brackets,
    )


# CRYPTO-ONLY desk: Binance USD-M lists TradFi-wrapper perps (gold/silver/oil COMMODITY,
# US/KR stocks EQUITY/KR_EQUITY, PREMARKET pre-IPO, INDEX baskets) that rank HIGH by 24h volume.
# `underlyingType` is COIN for the real cryptocurrencies; everything else is excluded.
_CRYPTO_UNDERLYING_TYPES = frozenset({"COIN"})

# Metal/commodity-PEGGED tokens the no-gold-coins mandate (§1/§20) FORBIDS. These are tradeable
# crypto tokens that TRACK a metal price, so Binance classifies them `underlyingType=COIN` and they
# slip past the COIN allowlist above — an explicit base-symbol denylist is the only thing that keeps
# them out. Keyed on the base asset (the part before `/USDT`), so it is exchange/quote agnostic and
# trivially extensible: add a base symbol here to ban a new metal/commodity-pegged token.
_PEGGED_COMMODITY_BASES = frozenset({"PAXG", "XAUT"})  # PAX Gold, Tether Gold


def _base_symbol(market: dict | None) -> str:
    """Best-effort base asset for a ccxt market dict, UPPER-cased.

    Prefers the ccxt unified `base` field, then the raw Binance `info.baseAsset`, then parses the
    unified `symbol` (`PAXG/USDT:USDT` -> `PAXG`). Returns "" when nothing identifies the base."""
    market = market or {}
    base = market.get("base")
    if not base:
        base = (market.get("info") or {}).get("baseAsset")
    if not base:
        sym = market.get("symbol") or ""
        base = sym.split("/", 1)[0] if "/" in sym else ""
    return str(base).upper()


def is_crypto_perp(market: dict | None) -> bool:
    """True only for a cryptocurrency COIN perp; False for TradFi-wrapper / metal-pegged contracts.

    Uses `underlyingType` authoritatively (COIN-only allowlist); on a metadata gap falls back to
    `contractType` so a TRADIFI_PERPETUAL is still rejected while a plain PERPETUAL is kept. A
    metal/commodity-PEGGED token (`_PEGGED_COMMODITY_BASES`, e.g. PAXG/XAUT) is rejected EVEN THOUGH
    Binance classifies it COIN — the no-gold-coins mandate (§1/§20) forbids gold/metals/commodities.
    """
    if _base_symbol(market) in _PEGGED_COMMODITY_BASES:
        return False  # gold/metal-pegged token — forbidden despite its COIN classification
    info = (market or {}).get("info") or {}
    utype = info.get("underlyingType")
    if utype:
        return utype in _CRYPTO_UNDERLYING_TYPES
    ctype = info.get("contractType")
    return ctype in (None, "", "PERPETUAL")


def scan_universe(client, top_n: int = 30) -> list[dict]:
    """Rank the live USD-M linear perp universe by 24h quote volume. Public/keyless. Returns up to
    top_n rows {symbol, last, chg_24h_pct, vol_24h_usd, onboard_date}, most-liquid first. Skips
    non-USDT-perps,
    zero vol/price, and (CRYPTO-ONLY) every non-cryptocurrency TradFi-wrapper perp."""
    tickers = client.fetch_tickers()
    markets = getattr(client, "markets", None) or {}
    rows: list[dict] = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        # Carry the ticker symbol into the market dict so `is_crypto_perp` can derive the base asset
        # (for the pegged-commodity denylist) even when the market metadata omits base/baseAsset.
        market = {**(markets.get(sym) or {}), "symbol": sym}
        if not is_crypto_perp(market):
            continue
        qv = t.get("quoteVolume") or 0.0
        last = t.get("last")
        if qv and last:
            rows.append({"symbol": sym, "last": float(last),
                         "chg_24h_pct": round(float(t.get("percentage") or 0.0), 2),
                         "vol_24h_usd": float(qv),
                         "onboard_date": parse_onboard_date_ms(market)})
    rows.sort(key=lambda r: r["vol_24h_usd"], reverse=True)
    return rows[:top_n]


def liquidity_floor(rows: list[dict], *, min_adv_usd: float, symbol_count: int) -> list[dict]:
    """Trim a vol-ranked universe to liquid large-caps: drop names below the 24h-ADV floor, then
    cap to `symbol_count` (the ~top 20-30 requirement, spec §4/§13). Input is assumed already
    ranked most-liquid-first by scan_universe; the floor is applied on `vol_24h_usd`."""
    kept = [r for r in rows if float(r.get("vol_24h_usd") or 0.0) >= min_adv_usd]
    return kept[:symbol_count]


def _book_depth_usd(levels: list[tuple[float, float]]) -> float:
    """FULL dollar notional of all `levels` (top-N book on one side). No cap — the depth floor is
    measured against this summed value, NOT clipped to depth_ref_usd."""
    acc = 0.0
    for price, qty in levels:
        acc += float(price) * float(qty)
    return acc


def _age_days(row: dict, *, now: datetime, exchange) -> float | None:
    """Listing age in days. Prefer onboard_date (ms-epoch); else derive from the earliest OHLCV
    kline timestamp (now - earliest). Returns None only when neither source is available (caller
    keeps the name, recording it under 'age_unknown' — a sane fallback, never a silent drop)."""
    onboard = row.get("onboard_date")
    if onboard is not None:
        return (now.timestamp() * 1000.0 - float(onboard)) / 86_400_000.0
    try:
        df = exchange.ohlcv(row["symbol"])
    except Exception:
        return None
    if df is None or df.empty or "timestamp" not in df:
        return None
    earliest = pd.to_datetime(df["timestamp"].iloc[0], utc=True).to_pydatetime()
    return (now - earliest).total_seconds() / 86_400.0


def quality_filter(
    rows: list[dict], *, now: datetime, exchange,
    min_adv_usd: float, min_age_days: int, max_abs_chg_24h_pct: float,
    min_depth_usd: float, depth_ref_usd: float, symbol_count: int,
) -> tuple[list[dict], dict[str, int]]:
    """'Liquid + established only': apply, in order, age -> 24h-mover -> depth -> ADV gates to a
    vol-ranked universe, then cap to symbol_count. Returns (kept_rows, drop_counts) so the scout
    can log EXACTLY how many names each gate removed (no silent truncation).

    - age: exclude names listed < min_age_days ago (onboard_date, else earliest-kline fallback);
      unknown age keeps the name (counted under 'age_unknown').
    - chg_24h: exclude |chg_24h_pct| > max_abs_chg_24h_pct (extreme movers are reversal traps).
    - depth: require the FULL top-of-book notional on the THINNER side >= min_depth_usd via
      exchange.depth(); missing/erroring/empty depth keeps the name ('depth_unavailable').
    - adv: the existing 24h-quote-volume floor (>= min_adv_usd).

    depth_ref_usd is accepted for config symmetry (it documents the slippage-model clip) but is NOT
    used as a cap inside the depth floor.
    """
    _ = depth_ref_usd  # reserved: slippage-model reference clip, not a floor cap
    drops = {"age": 0, "age_unknown": 0, "chg_24h": 0, "depth": 0,
             "depth_unavailable": 0, "adv": 0}
    kept: list[dict] = []
    for r in rows:
        age = _age_days(r, now=now, exchange=exchange)
        if age is None:
            drops["age_unknown"] += 1
        elif age < min_age_days:
            drops["age"] += 1
            continue
        if abs(float(r.get("chg_24h_pct") or 0.0)) > max_abs_chg_24h_pct:
            drops["chg_24h"] += 1
            continue
        try:
            book = exchange.depth(r["symbol"])
            bid_usd = _book_depth_usd(book.get("bids") or [])
            ask_usd = _book_depth_usd(book.get("asks") or [])
            side_usd = min(bid_usd, ask_usd)
            if side_usd <= 0.0:
                raise ValueError("empty book")
        except Exception:
            drops["depth_unavailable"] += 1
            side_usd = None
        if side_usd is not None and side_usd < min_depth_usd:
            drops["depth"] += 1
            continue
        if float(r.get("vol_24h_usd") or 0.0) < min_adv_usd:
            drops["adv"] += 1
            continue
        kept.append(r)
    return kept[:symbol_count], drops


def parse_ohlcv(rows: list[list]) -> pd.DataFrame:
    """ccxt OHLCV rows [[ts_ms,o,h,l,c,v], ...] -> sorted UTC-timestamped DataFrame."""
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return (
        df[["timestamp", "open", "high", "low", "close", "volume"]]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def parse_funding(fr: dict, interval: dict | None = None) -> FundingInfo:
    interval_hours = 8.0
    if interval and (interval.get("info") or {}).get("fundingIntervalHours") is not None:
        interval_hours = float(interval["info"]["fundingIntervalHours"])
    return FundingInfo(
        symbol=fr["symbol"],
        current_rate=float(fr["fundingRate"]),
        next_funding_ts=datetime.fromtimestamp(
            fr["fundingTimestamp"] / 1000, tz=timezone.utc),  # noqa: UP017
        interval_hours=interval_hours,
        mark_price=float(fr["markPrice"]),
        index_price=float(fr["indexPrice"]),
    )


def parse_open_interest_history(rows: list[dict]) -> pd.DataFrame:
    cols = ["timestamp", "oi_amount", "oi_value"]
    recs = []
    for r in rows:
        try:
            recs.append({
                "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
                "oi_amount": float(r["openInterestAmount"]),
                "oi_value": (float(r["openInterestValue"])
                             if r.get("openInterestValue") is not None else float("nan")),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not recs:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)


def parse_long_short_ratio(raw_rows: list[dict]) -> pd.DataFrame:
    cols = ["timestamp", "long_short_ratio", "long_account", "short_account"]
    recs = []
    for r in raw_rows:
        try:
            recs.append({
                "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
                "long_short_ratio": float(r["longShortRatio"]),
                "long_account": float(r["longAccount"]),
                "short_account": float(r["shortAccount"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not recs:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)
