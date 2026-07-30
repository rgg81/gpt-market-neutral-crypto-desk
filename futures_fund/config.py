from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ExchangeSettings(BaseModel):
    testnet: bool = True


class DataSettings(BaseModel):
    news_rss_sources: list[str] = Field(default_factory=lambda: [
        "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.cryptoslate.com/feed/",
        "https://bitcoinmagazine.com/feed",
        "https://cryptopotato.com/feed/",
    ])
    reddit_subreddits: list[str] = Field(
        default_factory=lambda: ["CryptoCurrency", "CryptoMarkets"])
    fred_key_env: str = "FRED_API_KEY"
    fred_series: list[str] = Field(
        default_factory=lambda: ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]
    )
    archive_dir: str = "state/archive"

    @property
    def fred_api_key(self) -> str | None:
        return os.environ.get(self.fred_key_env)


class UniverseSettings(BaseModel):
    symbol_count: int = 30
    min_adv_usd: float = 50_000_000.0
    crypto_only: bool = True
    # Phase 10 quality filter (liquid + established only)
    min_age_days: int = 30                 # exclude names listed < this many days ago
    max_abs_chg_24h_pct: float = 25.0      # exclude extreme 24h movers (|chg| > this)
    min_depth_usd: float = 250_000.0       # floor on FULL top-of-book notional (thinner side)
    depth_ref_usd: float = 100_000.0       # reference clip for the slippage model (NOT a floor cap)


class FeeSettings(BaseModel):
    taker_bps: float = 5.0
    maker_bps: float = 2.0
    pay_bnb: bool = False
    bnb_discount: float = 0.90


class FundingSettings(BaseModel):
    default_interval_hours: int = 8
    major_cap: float = 0.003
    alt_cap: float = 0.02
    majors: list[str] = Field(default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"])
    unclamped_in_rr: bool = True
    signed_realized: bool = True


class SlippageSettings(BaseModel):
    model: str = "depth"
    k: float = 0.1
    half_spread_bps_default: float = 1.0
    depth_levels: int = 20
    flat_bps: float | None = None


class MetricsSettings(BaseModel):
    daily_periods_per_year: int = 365
    weekly_periods_per_year: int = 52
    benchmark_return: float = 0.0


class BetaSettings(BaseModel):
    lookback_days: int = 45
    btc_symbol: str = "BTC/USDT:USDT"


class Settings(BaseModel):
    """LLM market-neutral desk settings. Trading DECISIONS are made by GPT agents; this config
    only parameterizes the deterministic plumbing (universe scan, exchange reads, fills, funding,
    slippage). PAPER-ONLY: `live` MUST stay false forever."""
    account_size_usdt: float = 20_000.0
    live: Literal[False] = False             # structurally PAPER-ONLY; true cannot validate
    agent_model: str = "gpt-5.6-sol"         # root + inherited subagents, xhigh via launcher
    universe_top_n: int = 40                 # rank the top-40 by 24h quote volume
    cadence_tf_minutes: int = 480            # 8h decision cadence (00/08/16 UTC, funding-aligned)
    btc_symbol: str = "BTC/USDT:USDT"        # beta/hedge reference
    beta: BetaSettings = Field(default_factory=BetaSettings)
    universe: UniverseSettings = Field(default_factory=UniverseSettings)
    fees: FeeSettings = Field(default_factory=FeeSettings)
    funding: FundingSettings = Field(default_factory=FundingSettings)
    slippage: SlippageSettings = Field(default_factory=SlippageSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    data: DataSettings = Field(default_factory=DataSettings)


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ WITHOUT overriding existing vars."""
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.exists():
        return loaded
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        loaded[k] = v
        os.environ.setdefault(k, v)
    return loaded


def load_settings(path: str | Path = "config.yaml") -> Settings:
    """Load non-secret config from YAML (defaults if file absent). Secrets come from env."""
    p = Path(path)
    load_env_file(p.parent / ".env")
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Settings(**(raw or {}))
