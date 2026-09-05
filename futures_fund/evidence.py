"""Per-coin evidence packs — the deterministic market data the LLM specialists read.

Pure data assembly from the exchange: NO opinion, NO decision. Optional positioning and
liquidity fields are fail-soft. The 1h/1d candle paths that drive price and beta evidence are
required and fail closed when missing or stale.

2026-07 forensic-review additions (the fields whose absence destroyed the desk):
  * LIQUIDITY (`depth_usd_bid/ask`, `spread_bps`, `est_slippage_bps_2k`) — the PM sized a $9K
    short in a name whose real one-way exit cost was 300-1000bps and could not see it.
  * FUNDING ECONOMICS — Binance's last settled rate is kept separate from a conservative,
    persistence-qualified historical carry statistic.  Neither is an exchange prediction.
  * HONEST BETA (`beta_clamped`, `beta_n_samples`) — beta was a 45-HOUR OLS (the config documents
    45 DAYS); LAB printed beta 10.42 and forced a 4x-cash hedge. `beta_btc` keeps the raw value
    for audit; `beta_clamped` (|beta| capped at 3.0) is what sizing should use.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from futures_fund.beta import beta_for_symbols, log_returns
from futures_fund.slippage import estimate_slippage

BETA_CLAMP = 3.0  # sizing beta cap: |beta| beyond this is estimation noise
_SLIP_PROBE_USD = 2_000.0  # est_slippage_bps_2k probe size (legacy field, kept for compat)
_SLIP_CURVE_USD = (2_000.0, 5_000.0, 10_000.0, 20_000.0)  # includes a full convex flip delta


class EvidencePack(BaseModel):
    symbol: str
    mark: float
    # The fresh proxy tail is exposed only as an intrabar observation.  Every load-bearing
    # momentum/beta/covariance/volatility feature below ends on the last completed candle.
    momentum_mark: float = 0.0
    momentum_mark_observed_at: datetime | None = None
    momentum_candle_open_ts: datetime | None = None
    momentum_mark_is_partial: bool = False
    intrabar_move_from_last_completed_pct: float = 0.0
    hourly_statistics_as_of_ts: datetime | None = None
    daily_beta_as_of_ts: datetime | None = None
    momentum_pct: float = 0.0  # close-to-close % change over the OHLCV window
    momentum_6h_pct: float = 0.0
    momentum_24h_pct: float = 0.0
    momentum_72h_pct: float = 0.0
    momentum_168h_pct: float = 0.0
    beta_adjusted_momentum_6h_pct: float = 0.0
    beta_adjusted_momentum_24h_pct: float = 0.0
    beta_adjusted_momentum_72h_pct: float = 0.0
    beta_adjusted_momentum_168h_pct: float = 0.0
    momentum_acceleration_24h_pct: float = 0.0
    drawdown_from_72h_high_pct: float = 0.0
    # stdev of log returns, annualized-ish (raw stdev * sqrt(periods))
    realized_vol: float = 0.0
    beta_btc: float = 1.0  # raw rolling beta to BTC (audit value)
    beta_clamped: float = 1.0  # sign(beta) * min(|beta|, 3.0) — USE THIS for sizing/hedging
    beta_n_samples: int = 0  # aligned return points behind the estimate (0 = fallback 1.0)
    beta_standard_error: float | None = None
    beta_ci95_lower: float | None = None
    beta_ci95_upper: float | None = None
    beta_r_squared: float | None = None
    beta_uncertainty_status: str = "unavailable"
    beta_adjusted_cross_sectional_zscore_24h: float | None = None
    beta_adjusted_cross_sectional_zscore_72h: float | None = None
    beta_adjusted_cross_sectional_zscore_168h: float | None = None
    beta_adjusted_cross_sectional_percentile_24h: float | None = None
    beta_adjusted_cross_sectional_percentile_72h: float | None = None
    beta_adjusted_cross_sectional_percentile_168h: float | None = None
    residual_trend_by_horizon: dict[str, dict[str, float | int | None]] = Field(
        default_factory=dict
    )
    residual_return_observations: int = 0
    beta_adjusted_momentum_acceleration_24h_pct: float | None = None
    beta_adjusted_momentum_acceleration_72h_pct: float | None = None
    residual_reversal_24h_pct: float | None = None
    residual_reversal_72h_pct: float | None = None
    realized_vol_annualized_by_horizon: dict[str, float | None] = Field(default_factory=dict)
    downside_vol_annualized_by_horizon: dict[str, float | None] = Field(default_factory=dict)
    volume_features_available: bool = False
    volume_participation_24h: float | None = None
    volume_surprise_24h: float | None = None
    quote_volume_24h: float | None = None
    # Truthful funding fields.  ``last_settled_*`` is backward-looking.  The conservative field is
    # a shrunken historical persistence statistic and is also NOT an exchange forecast.
    last_settled_funding_rate: float = 0.0
    last_settled_funding_8h_bps: float = 0.0
    last_settled_funding_ts: datetime | None = None
    funding_history_available: bool = False
    funding_observations_24h: int = 0
    funding_observations_72h: int = 0
    funding_observations_168h: int = 0
    funding_mean_8h_bps_24h: float = 0.0
    funding_mean_8h_bps_72h: float = 0.0
    funding_mean_8h_bps_168h: float = 0.0
    funding_median_8h_bps_24h: float = 0.0
    funding_median_8h_bps_72h: float = 0.0
    funding_median_8h_bps_168h: float = 0.0
    funding_dispersion_8h_bps_24h: float = 0.0
    funding_dispersion_8h_bps_72h: float = 0.0
    funding_dispersion_8h_bps_168h: float = 0.0
    funding_sign_persistence_24h: float = 0.0
    funding_sign_persistence_72h: float = 0.0
    funding_sign_persistence_168h: float = 0.0
    conservative_funding_8h_bps: float = 0.0
    conservative_funding_apr: float = 0.0
    # Compatibility aliases.  ``funding_rate`` and ``funding_apr`` describe only the last settled
    # observation. ``expected_funding_8h_bps`` aliases the conservative statistic; despite its
    # legacy name, it is not a prediction and never copies the latest rate directly.
    funding_rate: float = 0.0
    funding_apr: float = 0.0
    funding_interval_h: float = 8.0
    expected_funding_8h_bps: float = 0.0
    basis_bps: float = 0.0  # (mark - index) / index * 1e4
    open_interest_contracts: float = 0.0
    open_interest_usd: float = 0.0
    open_interest_as_of_ts: datetime | None = None
    open_interest_history_available: bool = False
    open_interest_latest_age_hours: float | None = None
    open_interest_sampling_interval_hours: float | None = None
    open_interest_latest_fresh: bool = False
    oi_contract_change_24h_pct: float | None = None
    oi_contract_change_72h_pct: float | None = None
    oi_contract_change_168h_pct: float | None = None
    oi_contract_change_24h_start_ts: datetime | None = None
    oi_contract_change_72h_start_ts: datetime | None = None
    oi_contract_change_168h_start_ts: datetime | None = None
    oi_contract_change_24h_available: bool = False
    oi_contract_change_72h_available: bool = False
    oi_contract_change_168h_available: bool = False
    oi_contract_change_24h_observation_hours: float | None = None
    oi_contract_change_72h_observation_hours: float | None = None
    oi_contract_change_168h_observation_hours: float | None = None
    oi_contract_change_24h_coverage_frac: float = 0.0
    oi_contract_change_72h_coverage_frac: float = 0.0
    oi_contract_change_168h_coverage_frac: float = 0.0
    # Deprecated compatibility fields: USD OI and the 168h contract-amount change, respectively.
    open_interest: float = 0.0
    oi_change_pct: float = 0.0
    long_short_ratio: float = 0.0
    long_short_ratio_as_of_ts: datetime | None = None
    long_short_ratio_history_available: bool = False
    long_short_ratio_latest_age_hours: float | None = None
    long_short_ratio_sampling_interval_hours: float | None = None
    long_short_ratio_latest_fresh: bool = False
    long_short_ratio_change_24h_pct: float | None = None
    long_short_ratio_change_72h_pct: float | None = None
    long_short_ratio_change_168h_pct: float | None = None
    long_short_ratio_change_24h_start_ts: datetime | None = None
    long_short_ratio_change_72h_start_ts: datetime | None = None
    long_short_ratio_change_168h_start_ts: datetime | None = None
    long_short_ratio_change_24h_available: bool = False
    long_short_ratio_change_72h_available: bool = False
    long_short_ratio_change_168h_available: bool = False
    long_short_ratio_change_24h_observation_hours: float | None = None
    long_short_ratio_change_72h_observation_hours: float | None = None
    long_short_ratio_change_168h_observation_hours: float | None = None
    long_short_ratio_change_24h_coverage_frac: float = 0.0
    long_short_ratio_change_72h_coverage_frac: float = 0.0
    long_short_ratio_change_168h_coverage_frac: float = 0.0
    long_short_ratio_observations_168h: int = 0
    long_short_ratio_zscore_168h: float | None = None
    long_short_ratio_percentile_168h: float | None = None
    depth_usd_bid: float = 0.0  # summed top-of-book bid notional (USD)
    depth_usd_ask: float = 0.0  # summed top-of-book ask notional (USD)
    liquidity_mid: float = 0.0  # midpoint of the SAME L2 snapshot used for the cost curve
    spread_bps: float = 0.0  # (best_ask - best_bid) / mid, bps
    est_slippage_bps_2k: float = 0.0  # est. one-way slippage (bps) for a $2K clip via depth walk
    # SIZE-AWARE cost curve (bps at each USD clip size, denominated at ``liquidity_mid``).
    # Slippage is CONVEX in clip size: on cycle 11
    # a $5K WLD leg cost 4.1x what its $2K probe implied, so the break-even math cleared a trade
    # whose true payback was ~32 funding intervals, not 7.7. NEVER extrapolate the 2k probe — read
    # the bps at (or above) the clip you actually intend to trade.
    slippage_curve_bps: dict[str, float] = Field(default_factory=dict)
    # Directional curves from the exact same midpoint/L2 snapshot. A BUY crosses asks and a SELL
    # crosses bids. Keeping these separate lets one-way loss control use the side it will actually
    # execute without requiring unrelated opposite-side depth. The aggregate curve above remains
    # the conservative worse-side compatibility/display field for round-trip and legacy consumers.
    slippage_curve_buy_bps: dict[str, float] = Field(default_factory=dict)
    slippage_curve_sell_bps: dict[str, float] = Field(default_factory=dict)
    as_of_ts: datetime


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — evidence is fail-soft; a bad read defaults to neutral
        return default


def _return_pct(closes: list[float], hours: int, *, end_offset: int = 0) -> float:
    """Close-to-close return over an exact hourly interval, ending `end_offset` bars ago."""
    end = len(closes) - 1 - end_offset
    start = end - hours
    if start < 0 or end < 0 or closes[start] <= 0.0:
        return 0.0
    return (closes[end] / closes[start] - 1.0) * 100.0


def _drawdown_from_high_pct(closes: list[float], hours: int) -> float:
    window = closes[-(hours + 1) :]
    if len(window) < 2:
        return 0.0
    peak = max(window)
    return (window[-1] / peak - 1.0) * 100.0 if peak > 0.0 else 0.0


def _beta_diagnostics(
    asset_closes: pd.Series,
    btc_closes: pd.Series,
    *,
    lookback: int = 45,
    is_btc: bool = False,
) -> dict[str, float | str | None]:
    """OLS beta uncertainty from the same timestamp-aligned daily sample as beta sizing."""
    if is_btc:
        return {
            "beta_standard_error": 0.0,
            "beta_ci95_lower": 1.0,
            "beta_ci95_upper": 1.0,
            "beta_r_squared": 1.0,
            "beta_uncertainty_status": "identity",
        }
    aligned = (
        pd.concat(
            [log_returns(asset_closes).rename("asset"), log_returns(btc_closes).rename("btc")],
            axis=1,
            join="inner",
        )
        .dropna()
        .tail(lookback)
    )
    if len(aligned) < 10:
        return {
            "beta_standard_error": None,
            "beta_ci95_lower": None,
            "beta_ci95_upper": None,
            "beta_r_squared": None,
            "beta_uncertainty_status": "insufficient_aligned_samples",
        }
    x = aligned["btc"].to_numpy(dtype=float)
    y = aligned["asset"].to_numpy(dtype=float)
    x_centered = x - x.mean()
    y_centered = y - y.mean()
    ssx = float(np.dot(x_centered, x_centered))
    if ssx <= 0.0:
        return {
            "beta_standard_error": None,
            "beta_ci95_lower": None,
            "beta_ci95_upper": None,
            "beta_r_squared": None,
            "beta_uncertainty_status": "zero_btc_variance",
        }
    beta = float(np.dot(x_centered, y_centered) / ssx)
    fitted = y.mean() + beta * x_centered
    residual = y - fitted
    sse = float(np.dot(residual, residual))
    dof = len(aligned) - 2
    standard_error = math.sqrt(max(sse / dof / ssx, 0.0)) if dof > 0 else None
    sst = float(np.dot(y_centered, y_centered))
    r_squared = 1.0 - sse / sst if sst > 0.0 else None
    return {
        "beta_standard_error": standard_error,
        "beta_ci95_lower": beta - 1.96 * standard_error if standard_error is not None else None,
        "beta_ci95_upper": beta + 1.96 * standard_error if standard_error is not None else None,
        "beta_r_squared": r_squared,
        "beta_uncertainty_status": "usable",
    }


def _residual_path_features(
    asset_closes: pd.Series,
    btc_closes: pd.Series,
    *,
    beta: float,
) -> dict:
    """Trend, acceleration, reversal and volatility diagnostics on completed candles only."""
    aligned = pd.concat(
        [asset_closes.rename("asset"), btc_closes.rename("btc")],
        axis=1,
        join="inner",
    ).dropna()
    aligned = aligned[(aligned["asset"] > 0.0) & (aligned["btc"] > 0.0)]
    residual_level = np.log(aligned["asset"]) - beta * np.log(aligned["btc"])
    residual_returns = residual_level.diff().dropna()
    trend: dict[str, dict[str, float | int | None]] = {}
    annualizer = math.sqrt(24.0 * 365.0)
    raw_returns = log_returns(asset_closes)
    raw_vol: dict[str, float | None] = {}
    downside_vol: dict[str, float | None] = {}
    for hours in (24, 72, 168):
        window = residual_level.tail(hours + 1)
        if len(window) < hours + 1:
            trend[str(hours)] = {
                "samples": len(window),
                "slope_log_return_per_hour": None,
                "slope_pct_per_hour": None,
                "slope_t_stat": None,
                "r_squared": None,
                "one_hour_sign_consistency": None,
            }
        else:
            y = window.to_numpy(dtype=float)
            x = np.arange(len(y), dtype=float)
            xc = x - x.mean()
            yc = y - y.mean()
            ssx = float(np.dot(xc, xc))
            slope = float(np.dot(xc, yc) / ssx) if ssx > 0.0 else 0.0
            fitted = y.mean() + slope * xc
            errors = y - fitted
            sse = float(np.dot(errors, errors))
            sst = float(np.dot(yc, yc))
            dof = len(y) - 2
            slope_se = math.sqrt(max(sse / dof / ssx, 0.0)) if dof > 0 and ssx > 0 else None
            changes = np.diff(y)
            direction = 1.0 if slope > 0.0 else -1.0 if slope < 0.0 else 0.0
            consistency = float(np.mean(changes * direction > 0.0)) if direction else None
            trend[str(hours)] = {
                "samples": len(window),
                "slope_log_return_per_hour": slope,
                "slope_pct_per_hour": math.expm1(slope) * 100.0,
                "slope_t_stat": slope / slope_se if slope_se and slope_se > 0.0 else None,
                "r_squared": 1.0 - sse / sst if sst > 0.0 else None,
                "one_hour_sign_consistency": consistency,
            }
        vol_window = raw_returns.tail(hours)
        if len(vol_window) < max(12, hours // 2):
            raw_vol[str(hours)] = None
            downside_vol[str(hours)] = None
        else:
            raw_vol[str(hours)] = float(vol_window.std(ddof=1) * annualizer)
            negative = vol_window[vol_window < 0.0]
            downside_vol[str(hours)] = (
                float(math.sqrt(float(np.mean(np.square(negative)))) * annualizer)
                if len(negative) >= 3
                else None
            )

    def cumulative(hours: int, end_offset: int = 0) -> float | None:
        end = len(residual_level) - 1 - end_offset
        start = end - hours
        if start < 0 or end < 0:
            return None
        return math.expm1(float(residual_level.iloc[end] - residual_level.iloc[start])) * 100.0

    current_24 = cumulative(24)
    prior_24 = cumulative(24, 24)
    current_72 = cumulative(72)
    prior_72 = cumulative(72, 72)
    recent_6 = cumulative(6)
    return {
        "residual_trend_by_horizon": trend,
        "beta_adjusted_momentum_acceleration_24h_pct": (
            current_24 - prior_24 if current_24 is not None and prior_24 is not None else None
        ),
        "beta_adjusted_momentum_acceleration_72h_pct": (
            current_72 - prior_72 if current_72 is not None and prior_72 is not None else None
        ),
        "residual_reversal_24h_pct": (
            recent_6 - current_24 * 0.25
            if recent_6 is not None and current_24 is not None
            else None
        ),
        "residual_reversal_72h_pct": (
            current_24 - current_72 / 3.0
            if current_24 is not None and current_72 is not None
            else None
        ),
        "realized_vol_annualized_by_horizon": raw_vol,
        "downside_vol_annualized_by_horizon": downside_vol,
        "residual_return_observations": len(residual_returns),
    }


def _volume_features(frame: pd.DataFrame, completed_index: pd.Index) -> dict:
    """Volume participation/surprise, explicitly unavailable when a feed omits volume."""
    if "timestamp" not in frame or "close" not in frame or "volume" not in frame:
        return {
            "volume_features_available": False,
            "volume_participation_24h": None,
            "volume_surprise_24h": None,
            "quote_volume_24h": None,
        }
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    series = (
        pd.DataFrame(
            {"volume": volume.to_numpy(), "close": close.to_numpy()},
            index=pd.DatetimeIndex(timestamps),
        )
        .reindex(completed_index)
        .dropna()
    )
    if len(series) < 48 or (series["volume"] < 0.0).any():
        return {
            "volume_features_available": False,
            "volume_participation_24h": None,
            "volume_surprise_24h": None,
            "quote_volume_24h": None,
        }
    recent = series.tail(24)
    history = series.iloc[:-24].tail(144)
    total_window = series.tail(168)["volume"].sum()
    baseline = history["volume"].mean() if not history.empty else float("nan")
    return {
        "volume_features_available": True,
        "volume_participation_24h": (
            float(recent["volume"].sum() / total_window) if total_window > 0.0 else None
        ),
        "volume_surprise_24h": (
            float(recent["volume"].mean() / baseline - 1.0)
            if np.isfinite(baseline) and baseline > 0.0
            else None
        ),
        "quote_volume_24h": float((recent["volume"] * recent["close"]).sum()),
    }


def _timestamped_closes(frame: pd.DataFrame, *, symbol: str, timeframe: str) -> pd.Series:
    """Preserve real candle timestamps so beta aligns common dates, never row positions."""
    if "timestamp" not in frame or "close" not in frame:
        raise ValueError(f"required {timeframe} candles for {symbol} lack timestamp/close")
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="raise")
    if timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
        raise ValueError(f"required {timeframe} candles for {symbol} are not unique/sorted")
    closes = pd.to_numeric(frame["close"], errors="raise").astype(float)
    return pd.Series(closes.to_numpy(), index=pd.DatetimeIndex(timestamps), dtype=float)


def _completed_candle_series(
    series: pd.Series,
    *,
    timeframe: str,
    now: datetime,
) -> tuple[pd.Series, datetime]:
    """Return only candles whose close boundary is not later than ``now``.

    The proxy contract still requires the currently-forming tail.  This filter is downstream of
    that freshness proof and prevents the tail from entering beta, covariance, or volatility as a
    completed observation.
    """
    durations = {"1h": pd.Timedelta(hours=1), "1d": pd.Timedelta(days=1)}
    try:
        duration = durations[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported statistical candle timeframe {timeframe}") from exc
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    completed = series[(series.index + duration) <= now_ts]
    if completed.empty:
        raise ValueError(f"no completed {timeframe} candles at {now_ts.isoformat()}")
    data_as_of = (completed.index[-1] + duration).to_pydatetime()
    return completed, data_as_of


def _clean_history_frame(
    frame: pd.DataFrame,
    *,
    value_columns: tuple[str, ...],
    now: datetime,
) -> pd.DataFrame:
    """Normalize an ancillary timestamped history without manufacturing missing observations."""
    required = {"timestamp", *value_columns}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=["timestamp", *value_columns])
    cleaned = frame[["timestamp", *value_columns]].copy()
    cleaned["timestamp"] = pd.to_datetime(cleaned["timestamp"], utc=True, errors="coerce")
    for column in value_columns:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")
    finite = np.ones(len(cleaned), dtype=bool)
    for column in value_columns:
        finite &= np.isfinite(cleaned[column].to_numpy(dtype=float))
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    cleaned = cleaned[cleaned["timestamp"].notna() & finite]
    cleaned = cleaned[cleaned["timestamp"] <= now_ts]
    return (
        cleaned.sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )


def _horizon_change(
    frame: pd.DataFrame,
    *,
    value_column: str,
    hours: int,
    latest_fresh: bool,
    expected_interval_h: float,
) -> dict[str, object]:
    """Return a horizon change only when endpoint freshness and path coverage are adequate."""
    result: dict[str, object] = {
        "change_pct": None,
        "start_ts": None,
        "observation_hours": None,
        "coverage_frac": 0.0,
        "available": False,
    }
    if frame.empty:
        return result
    latest = frame.iloc[-1]
    target = latest["timestamp"] - pd.Timedelta(hours=hours)
    nearest_index = (frame["timestamp"] - target).abs().idxmin()
    start = frame.loc[nearest_index]
    observation_hours = (latest["timestamp"] - start["timestamp"]).total_seconds() / 3600.0
    result["start_ts"] = start["timestamp"].to_pydatetime()
    result["observation_hours"] = observation_hours
    if observation_hours <= 0.0 or expected_interval_h <= 0.0:
        return result

    path = frame[
        (frame["timestamp"] >= start["timestamp"]) & (frame["timestamp"] <= latest["timestamp"])
    ]
    expected_points = max(2, int(round(observation_hours / expected_interval_h)) + 1)
    sample_coverage = min(len(path) / expected_points, 1.0)
    time_coverage = min(observation_hours / hours, hours / observation_hours)
    coverage = min(sample_coverage, time_coverage)
    result["coverage_frac"] = coverage
    close_enough = abs(observation_hours - hours) <= 1.5 * expected_interval_h
    start_value = float(start[value_column])
    available = latest_fresh and close_enough and coverage >= 0.90 and start_value != 0.0
    result["available"] = available
    if not available:
        return result
    change = (float(latest[value_column]) / start_value - 1.0) * 100.0
    result["change_pct"] = change
    return result


def _history_freshness(
    frame: pd.DataFrame,
    *,
    now: datetime,
    expected_interval_h: float,
) -> dict[str, object]:
    if frame.empty:
        return {
            "history_available": False,
            "latest_age_hours": None,
            "sampling_interval_hours": None,
            "latest_fresh": False,
        }
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    latest_age = max(
        0.0,
        (now_ts - frame.iloc[-1]["timestamp"]).total_seconds() / 3600.0,
    )
    gaps = frame["timestamp"].diff().dt.total_seconds().div(3600.0)
    positive_gaps = gaps[(gaps > 0.0) & np.isfinite(gaps)]
    observed_interval = (
        float(positive_gaps.median()) if not positive_gaps.empty else expected_interval_h
    )
    return {
        "history_available": True,
        "latest_age_hours": latest_age,
        "sampling_interval_hours": observed_interval,
        "latest_fresh": latest_age <= 1.5 * expected_interval_h,
    }


def _funding_history_features(
    events: list[dict],
    *,
    now: datetime,
    last_settled_rate: float,
    current_interval_h: float,
) -> dict:
    """Describe settled funding history and derive a conservative persistence statistic.

    Historical settlement rates are standardized to an 8h equivalent using observed boundary
    spacing.  ``conservative_funding_8h_bps`` is zero unless 24h/72h/168h robust centers and the
    latest settled direction agree with at least two-thirds dominant-sign fraction in every
    window.  The persisted ``funding_sign_persistence_*`` value is that dominant-sign fraction
    (for example, two positive and one negative settlement is 2/3), not net signed imbalance.  It
    is deliberately a shrunken historical descriptor, not a next-rate forecast.
    """
    fields: dict[str, object] = {
        "last_settled_funding_ts": None,
        "funding_history_available": False,
        "conservative_funding_8h_bps": 0.0,
    }
    for hours in (24, 72, 168):
        fields[f"funding_observations_{hours}h"] = 0
        fields[f"funding_mean_8h_bps_{hours}h"] = 0.0
        fields[f"funding_median_8h_bps_{hours}h"] = 0.0
        fields[f"funding_dispersion_8h_bps_{hours}h"] = 0.0
        fields[f"funding_sign_persistence_{hours}h"] = 0.0

    records: list[dict] = []
    for event in events:
        try:
            timestamp = pd.to_datetime(event["timestamp"], utc=True, errors="raise")
            rate = float(event["rate"])
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite(rate):
            records.append({"timestamp": timestamp, "rate": rate})
    if not records:
        return fields

    history = pd.DataFrame(records).sort_values("timestamp")
    history = history.drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    history = history[history["timestamp"] <= now_ts].reset_index(drop=True)
    if history.empty:
        return fields

    fallback_interval = current_interval_h if current_interval_h > 0.0 else 8.0
    observed_intervals = history["timestamp"].diff().dt.total_seconds().div(3600.0)
    valid_intervals = observed_intervals.where(
        (observed_intervals >= 0.5) & (observed_intervals <= 24.0),
        fallback_interval,
    ).fillna(fallback_interval)
    history["rate_8h_bps"] = history["rate"] * (8.0 / valid_intervals) * 1e4
    fields["last_settled_funding_ts"] = history.iloc[-1]["timestamp"].to_pydatetime()
    fields["funding_history_available"] = True

    centers: dict[int, tuple[float, float, float, int]] = {}
    for hours in (24, 72, 168):
        window = history[history["timestamp"] > now_ts - pd.Timedelta(hours=hours)]
        values = window["rate_8h_bps"].to_numpy(dtype=float)
        count = len(values)
        mean = float(np.mean(values)) if count else 0.0
        median = float(np.median(values)) if count else 0.0
        dispersion = float(np.std(values, ddof=0)) if count else 0.0
        if count:
            signs_in_window = np.sign(values)
            persistence = float(
                max(np.sum(signs_in_window > 0), np.sum(signs_in_window < 0)) / count
            )
        else:
            persistence = 0.0
        fields[f"funding_observations_{hours}h"] = count
        fields[f"funding_mean_8h_bps_{hours}h"] = mean
        fields[f"funding_median_8h_bps_{hours}h"] = median
        fields[f"funding_dispersion_8h_bps_{hours}h"] = dispersion
        fields[f"funding_sign_persistence_{hours}h"] = persistence
        centers[hours] = (mean, median, persistence, count)

    last_8h_bps = last_settled_rate * (8.0 / fallback_interval) * 1e4
    minimum_counts = {
        hours: max(2, math.ceil(0.80 * hours / fallback_interval)) for hours in (24, 72, 168)
    }
    signs = [int(np.sign(last_8h_bps))]
    candidates: list[float] = []
    sufficient = True
    for hours, (mean, median, persistence, count) in centers.items():
        signs.append(int(np.sign(median)))
        candidates.extend((abs(mean), abs(median)))
        sufficient &= count >= minimum_counts[hours] and persistence >= (2.0 / 3.0)
    latest_age_h = (now_ts - history.iloc[-1]["timestamp"]).total_seconds() / 3600.0
    recent_history = history[history["timestamp"] > now_ts - pd.Timedelta(hours=168)]
    recent_gaps = recent_history["timestamp"].diff().dt.total_seconds().div(3600.0).dropna()
    continuous = latest_age_h <= max(1.5 * fallback_interval, 1.0)
    if not recent_gaps.empty:
        continuous &= float(recent_gaps.max()) <= 1.5 * fallback_interval
    aligned = signs[0] != 0 and len(set(signs)) == 1
    if sufficient and continuous and aligned and candidates:
        shrink = min(centers[hours][2] for hours in (24, 72, 168))
        fields["conservative_funding_8h_bps"] = signs[0] * min(candidates) * shrink
    return fields


def _open_interest_features(frame: pd.DataFrame, *, now: datetime) -> dict:
    cleaned = _clean_history_frame(
        frame,
        value_columns=("oi_amount", "oi_value"),
        now=now,
    )
    fields: dict[str, object] = {
        "open_interest_contracts": 0.0,
        "open_interest_usd": 0.0,
        "open_interest_as_of_ts": None,
        "open_interest_history_available": False,
        "open_interest_latest_age_hours": None,
        "open_interest_sampling_interval_hours": None,
        "open_interest_latest_fresh": False,
    }
    freshness = _history_freshness(cleaned, now=now, expected_interval_h=4.0)
    fields.update({f"open_interest_{key}": value for key, value in freshness.items()})
    for hours in (24, 72, 168):
        horizon = _horizon_change(
            cleaned,
            value_column="oi_amount",
            hours=hours,
            latest_fresh=bool(freshness["latest_fresh"]),
            expected_interval_h=4.0,
        )
        fields[f"oi_contract_change_{hours}h_pct"] = horizon["change_pct"]
        fields[f"oi_contract_change_{hours}h_start_ts"] = horizon["start_ts"]
        fields[f"oi_contract_change_{hours}h_available"] = horizon["available"]
        fields[f"oi_contract_change_{hours}h_observation_hours"] = horizon["observation_hours"]
        fields[f"oi_contract_change_{hours}h_coverage_frac"] = horizon["coverage_frac"]
    if not cleaned.empty:
        latest = cleaned.iloc[-1]
        fields["open_interest_contracts"] = float(latest["oi_amount"])
        fields["open_interest_usd"] = float(latest["oi_value"])
        fields["open_interest_as_of_ts"] = latest["timestamp"].to_pydatetime()
    return fields


def _long_short_features(frame: pd.DataFrame, *, now: datetime) -> dict:
    cleaned = _clean_history_frame(
        frame,
        value_columns=("long_short_ratio",),
        now=now,
    )
    fields: dict[str, object] = {
        "long_short_ratio": 0.0,
        "long_short_ratio_as_of_ts": None,
        "long_short_ratio_history_available": False,
        "long_short_ratio_latest_age_hours": None,
        "long_short_ratio_sampling_interval_hours": None,
        "long_short_ratio_latest_fresh": False,
        "long_short_ratio_observations_168h": 0,
        "long_short_ratio_zscore_168h": None,
        "long_short_ratio_percentile_168h": None,
    }
    freshness = _history_freshness(cleaned, now=now, expected_interval_h=4.0)
    fields.update({f"long_short_ratio_{key}": value for key, value in freshness.items()})
    horizon_results: dict[int, dict[str, object]] = {}
    for hours in (24, 72, 168):
        horizon = _horizon_change(
            cleaned,
            value_column="long_short_ratio",
            hours=hours,
            latest_fresh=bool(freshness["latest_fresh"]),
            expected_interval_h=4.0,
        )
        horizon_results[hours] = horizon
        fields[f"long_short_ratio_change_{hours}h_pct"] = horizon["change_pct"]
        fields[f"long_short_ratio_change_{hours}h_start_ts"] = horizon["start_ts"]
        fields[f"long_short_ratio_change_{hours}h_available"] = horizon["available"]
        fields[f"long_short_ratio_change_{hours}h_observation_hours"] = horizon["observation_hours"]
        fields[f"long_short_ratio_change_{hours}h_coverage_frac"] = horizon["coverage_frac"]
    if cleaned.empty:
        return fields

    latest = cleaned.iloc[-1]
    latest_value = float(latest["long_short_ratio"])
    fields["long_short_ratio"] = latest_value
    fields["long_short_ratio_as_of_ts"] = latest["timestamp"].to_pydatetime()
    window = cleaned[cleaned["timestamp"] >= latest["timestamp"] - pd.Timedelta(hours=168)][
        "long_short_ratio"
    ].to_numpy(dtype=float)
    fields["long_short_ratio_observations_168h"] = len(window)
    if len(window) and bool(horizon_results[168]["available"]):
        mean = float(np.mean(window))
        dispersion = float(np.std(window, ddof=0))
        fields["long_short_ratio_zscore_168h"] = (
            (latest_value - mean) / dispersion if dispersion > 0.0 else 0.0
        )
        less = float(np.sum(window < latest_value))
        equal = float(np.sum(window == latest_value))
        fields["long_short_ratio_percentile_168h"] = (less + 0.5 * equal) / len(window) * 100.0
    return fields


def _slip_bps_at(
    sym: str,
    reference_price: float,
    levels: list[tuple[float, float]],
    notional: float,
    half_spread_bps: float = 0.0,
) -> float:
    """One-way slippage in bps for a clip of `notional` USD, via the SAME depth walk the fill path
    uses (`estimate_slippage`), floored at the half-spread.

    `reference_price` and `levels` MUST come from the same L2 snapshot. Market movement between the
    evidence snapshot and the later paper execution is recorded as decision-to-execution drift,
    not multiplied into this liquidity estimate. The floor preserves the unavoidable cost of
    crossing a deep book even when its price impact is otherwise negligible."""
    if reference_price <= 0 or not levels or notional <= 0:
        base = max(0.0, half_spread_bps)
    else:
        qty = notional / reference_price
        cost = _safe(
            lambda: estimate_slippage(
                sym,
                qty,
                reference_price,
                depth=levels,
                adv_usd=0.0,
                half_spread_bps=half_spread_bps,
            ),
            0.0,
        )
        walk_bps = (cost / notional) * 1e4 if cost else 0.0
        base = max(walk_bps, half_spread_bps)
    return base


def _depth_fields(
    exchange, sym: str
) -> tuple[
    float,
    float,
    float,
    float,
    float,
    dict[str, float],
    dict[str, float],
    dict[str, float],
]:
    """Return depth, same-snapshot midpoint/spread, and the size-aware cost curve.

    The curve prices the clip sizes the desk actually trades ($2k/$5k/$10k/$20k, including a
    combined flip delta), because slippage is convex in size. It takes the worse of buying through
    asks and selling through bids so the side-agnostic evidence remains conservative without
    confusing market drift with friction."""
    book = _safe(lambda: exchange.depth(sym), None)
    if not book:
        return 0.0, 0.0, 0.0, 0.0, 0.0, {}, {}, {}
    bids = [
        (float(p), float(q))
        for p, q in (book.get("bids") or [])
        if float(p) > 0.0 and float(q) > 0.0
    ]
    asks = [
        (float(p), float(q))
        for p, q in (book.get("asks") or [])
        if float(p) > 0.0 and float(q) > 0.0
    ]
    bid_usd = sum(p * q for p, q in bids)
    ask_usd = sum(p * q for p, q in asks)
    if not bids or not asks or bids[0][0] > asks[0][0]:
        # A one-sided/crossed book has no coherent midpoint. Preserve its visible depth for audit
        # but publish no cost curve rather than pairing it with a mark from another timestamp.
        return bid_usd, ask_usd, 0.0, 0.0, 0.0, {}, {}, {}

    mid = (bids[0][0] + asks[0][0]) / 2.0
    spread = (asks[0][0] - bids[0][0]) / mid * 1e4 if mid > 0 else 0.0
    # Floor every curve point at the HALF-SPREAD — crossing the book always costs at least that,
    # even when the depth-walk impact is ~0 (cycle-17 fix).
    half_spread = spread / 2.0

    def fully_covered(levels: list[tuple[float, float]], notional: float) -> bool:
        return sum(qty for _, qty in levels) + 1e-12 >= notional / mid

    # Never let estimate_slippage's over-depth fallback turn an incomplete L2 snapshot into a
    # deceptively cheap measured point. There is no ADV estimate in this evidence path, so each
    # directional point exists only when its actual crossing side visibly covers the full clip.
    # The legacy aggregate remains the worse side and therefore exists only on the intersection.
    buy_curve = {
        f"{int(n // 1000)}k": round(
            _slip_bps_at(sym, mid, asks, n, half_spread),
            3,
        )
        for n in _SLIP_CURVE_USD
        if fully_covered(asks, n)
    }
    sell_curve = {
        f"{int(n // 1000)}k": round(
            _slip_bps_at(sym, mid, bids, n, half_spread),
            3,
        )
        for n in _SLIP_CURVE_USD
        if fully_covered(bids, n)
    }
    curve = {
        f"{int(n // 1000)}k": round(
            max(
                buy_curve[f"{int(n // 1000)}k"],
                sell_curve[f"{int(n // 1000)}k"],
            ),
            3,
        )
        for n in _SLIP_CURVE_USD
        if f"{int(n // 1000)}k" in buy_curve and f"{int(n // 1000)}k" in sell_curve
    }
    slip_bps = curve.get("2k", 0.0)
    return bid_usd, ask_usd, mid, spread, slip_bps, curve, buy_curve, sell_curve


def _build_residual_risk_model(
    hourly_closes: dict[str, pd.Series],
    betas: dict[str, float],
    *,
    btc_symbol: str,
    now: datetime,
    lookback_hours: int = 168,
    beta_estimation_closes: dict[str, pd.Series] | None = None,
) -> dict:
    """Measure alpha covariance after removing each asset's clamped BTC component.

    This is descriptive evidence only. It gives the PM and Adversary the same deterministic
    covariance/risk inputs; it never sizes or vetoes a leg.
    """
    btc_returns = log_returns(hourly_closes.get(btc_symbol, pd.Series(dtype=float)))
    residuals: dict[str, pd.Series] = {}
    for symbol, closes in hourly_closes.items():
        if symbol == btc_symbol:
            continue
        raw_beta = float(betas.get(symbol, 1.0))
        clamped_beta = (
            float(np.sign(raw_beta) * min(abs(raw_beta), BETA_CLAMP)) if raw_beta else 0.0
        )
        aligned = pd.concat(
            [log_returns(closes).rename("asset"), btc_returns.rename("btc")],
            axis=1,
            join="inner",
        ).dropna()
        if len(aligned) >= 2:
            residuals[symbol] = (aligned["asset"] - clamped_beta * aligned["btc"]).tail(
                lookback_hours
            )

    symbols = sorted(residuals)
    if not symbols:
        return {
            "schema_version": 2,
            "as_of_ts": now.isoformat(),
            "return_label": "hourly_log_return_minus_beta_clamped_times_btc",
            "lookback_hours": lookback_hours,
            "available": False,
            "unavailable_reason": "no residual return series",
            "minimum_common_samples": 48,
            "common_samples": 0,
            "symbols": [],
            "residual_vol_annualized": {},
            "covariance_annualized": {},
            "correlation": {},
            "pairwise_samples": {},
            "high_correlation_pairs": [],
            "preferred_covariance_estimator": "unavailable",
            "residual_vol_ewma_shrunk_annualized": {},
            "covariance_ewma_shrunk_annualized": {},
            "correlation_ewma_shrunk": {},
            "multi_window_estimators": {},
            "residual_horizon_scenarios": {},
            "beta_uncertainty": {},
        }

    minimum_common_samples = 48
    frame = pd.concat(residuals, axis=1).dropna(how="any").tail(lookback_hours)
    if len(frame) < minimum_common_samples:
        return {
            "schema_version": 2,
            "as_of_ts": now.isoformat(),
            "return_label": "hourly_log_return_minus_beta_clamped_times_btc",
            "lookback_hours": lookback_hours,
            "available": False,
            "unavailable_reason": "insufficient common complete-case residual returns",
            "minimum_common_samples": minimum_common_samples,
            "common_samples": len(frame),
            "symbols": symbols,
            "residual_vol_annualized": {},
            "covariance_annualized": {},
            "correlation": {},
            "pairwise_samples": {},
            "high_correlation_pairs": [],
            "preferred_covariance_estimator": "unavailable",
            "residual_vol_ewma_shrunk_annualized": {},
            "covariance_ewma_shrunk_annualized": {},
            "correlation_ewma_shrunk": {},
            "multi_window_estimators": {},
            "residual_horizon_scenarios": {},
            "beta_uncertainty": {},
        }
    annualizer = 24.0 * 365.0
    covariance = frame.cov() * annualizer
    correlation = frame.corr()

    def ewma_shrunk(sample: pd.DataFrame, half_life: float) -> tuple[pd.DataFrame, float]:
        count = len(sample)
        age = np.arange(count - 1, -1, -1, dtype=float)
        weights = np.power(0.5, age / half_life)
        weights /= weights.sum()
        values = sample.to_numpy(dtype=float)
        centered = values - np.sum(values * weights[:, None], axis=0)
        correction = max(1.0 - float(np.dot(weights, weights)), 1e-12)
        raw = (centered * weights[:, None]).T @ centered / correction
        shrinkage = min(0.35, max(0.05, len(symbols) / max(count - 1, 1)))
        estimate = (1.0 - shrinkage) * raw + shrinkage * np.diag(np.diag(raw))
        return (
            pd.DataFrame(estimate * annualizer, index=symbols, columns=symbols),
            shrinkage,
        )

    preferred_covariance, shrinkage = ewma_shrunk(frame, half_life=72.0)
    preferred_std = np.sqrt(np.clip(np.diag(preferred_covariance), 0.0, None))
    preferred_denominator = np.outer(preferred_std, preferred_std)
    preferred_values = preferred_covariance.to_numpy(dtype=float)
    preferred_correlation = pd.DataFrame(
        np.divide(
            preferred_values,
            preferred_denominator,
            out=np.zeros_like(preferred_values),
            where=preferred_denominator > 0.0,
        ),
        index=symbols,
        columns=symbols,
    )
    min_eigenvalue = float(np.linalg.eigvalsh(preferred_covariance.to_numpy(dtype=float)).min())
    if not np.isfinite(min_eigenvalue) or min_eigenvalue < -1e-10:
        return {
            "schema_version": 2,
            "as_of_ts": now.isoformat(),
            "return_label": "hourly_log_return_minus_beta_clamped_times_btc",
            "lookback_hours": lookback_hours,
            "available": False,
            "unavailable_reason": "residual covariance is not positive semidefinite",
            "minimum_common_samples": minimum_common_samples,
            "common_samples": len(frame),
            "symbols": symbols,
            "residual_vol_annualized": {},
            "covariance_annualized": {},
            "correlation": {},
            "pairwise_samples": {},
            "high_correlation_pairs": [],
            "preferred_covariance_estimator": "unavailable_non_psd",
            "residual_vol_ewma_shrunk_annualized": {},
            "covariance_ewma_shrunk_annualized": {},
            "correlation_ewma_shrunk": {},
            "multi_window_estimators": {},
            "residual_horizon_scenarios": {},
            "beta_uncertainty": {},
        }
    pairwise_samples: dict[str, dict[str, int]] = {}
    covariance_rows: dict[str, dict[str, float | None]] = {}
    correlation_rows: dict[str, dict[str, float | None]] = {}
    preferred_covariance_rows: dict[str, dict[str, float | None]] = {}
    preferred_correlation_rows: dict[str, dict[str, float | None]] = {}
    residual_vol: dict[str, float | None] = {}
    preferred_residual_vol: dict[str, float | None] = {}
    high_pairs: list[dict] = []
    for left in symbols:
        pairwise_samples[left] = {}
        covariance_rows[left] = {}
        correlation_rows[left] = {}
        preferred_covariance_rows[left] = {}
        preferred_correlation_rows[left] = {}
        variance = float(covariance.loc[left, left])
        residual_vol[left] = math.sqrt(max(variance, 0.0)) if np.isfinite(variance) else None
        preferred_variance = float(preferred_covariance.loc[left, left])
        preferred_residual_vol[left] = (
            math.sqrt(max(preferred_variance, 0.0)) if np.isfinite(preferred_variance) else None
        )
        for right in symbols:
            count = len(frame)
            cov_value = float(covariance.loc[left, right])
            corr_value = float(correlation.loc[left, right])
            preferred_cov_value = float(preferred_covariance.loc[left, right])
            preferred_corr_value = float(preferred_correlation.loc[left, right])
            pairwise_samples[left][right] = count
            covariance_rows[left][right] = cov_value if np.isfinite(cov_value) else None
            correlation_rows[left][right] = corr_value if np.isfinite(corr_value) else None
            preferred_covariance_rows[left][right] = (
                preferred_cov_value if np.isfinite(preferred_cov_value) else None
            )
            preferred_correlation_rows[left][right] = (
                preferred_corr_value if np.isfinite(preferred_corr_value) else None
            )
            if (
                left < right
                and np.isfinite(preferred_corr_value)
                and abs(preferred_corr_value) >= 0.60
            ):
                high_pairs.append(
                    {
                        "left": left,
                        "right": right,
                        "correlation": preferred_corr_value,
                        "samples": count,
                        "estimator": "ewma_diagonal_shrinkage",
                    }
                )
    high_pairs.sort(key=lambda row: abs(row["correlation"]), reverse=True)

    def matrix_rows(matrix: pd.DataFrame) -> dict[str, dict[str, float | None]]:
        return {
            left: {
                right: (
                    float(matrix.loc[left, right])
                    if np.isfinite(float(matrix.loc[left, right]))
                    else None
                )
                for right in symbols
            }
            for left in symbols
        }

    multi_window: dict[str, dict] = {}
    for window in (48, 72, 168):
        sample = frame.tail(window)
        minimum = min(window, minimum_common_samples)
        if len(sample) < minimum:
            multi_window[str(window)] = {
                "available": False,
                "samples": len(sample),
                "minimum_samples": minimum,
                "covariance_annualized": {},
            }
            continue
        half_life = max(12.0, window / 3.0)
        estimate, window_shrinkage = ewma_shrunk(sample, half_life=half_life)
        multi_window[str(window)] = {
            "available": True,
            "samples": len(sample),
            "minimum_samples": minimum,
            "half_life_hours": half_life,
            "diagonal_shrinkage_intensity": window_shrinkage,
            "covariance_annualized": matrix_rows(estimate),
        }

    horizon_scenarios: dict[str, dict] = {}
    for horizon in (6, 24, 72, 168):
        aggregated = frame.rolling(horizon).sum().dropna()
        minimum_scenarios = 12
        scenario_selection = {
            "method": "cross_sectional_rms_extremes_plus_even_time_grid",
            "maximum_stored_scenarios": 12,
            "distributional_statistics_use_full_observation_set": True,
            "stored_scenarios_are_distributionally_representative": False,
            "overlapping_horizon_observations": horizon > 1,
        }
        if len(aggregated) < minimum_scenarios:
            horizon_scenarios[str(horizon)] = {
                "available": False,
                "observations": len(aggregated),
                "minimum_observations": minimum_scenarios,
                "scenario_selection": scenario_selection,
                "stored_scenarios": 0,
                "expected_shortfall_97_5_by_symbol": {},
                "expected_shortfall_97_5_lower_by_symbol": {},
                "expected_shortfall_97_5_upper_by_symbol": {},
                "scenarios": [],
            }
            continue
        converted = np.expm1(aggregated)
        expected_shortfall_lower = {}
        expected_shortfall_upper = {}
        for symbol in symbols:
            values = converted[symbol].to_numpy(dtype=float)
            lower_threshold = float(np.quantile(values, 0.025))
            lower_tail = values[values <= lower_threshold]
            expected_shortfall_lower[symbol] = (
                float(lower_tail.mean()) if len(lower_tail) else lower_threshold
            )
            upper_threshold = float(np.quantile(values, 0.975))
            upper_tail = values[values >= upper_threshold]
            expected_shortfall_upper[symbol] = (
                float(upper_tail.mean()) if len(upper_tail) else upper_threshold
            )

        # The full rolling panel can be thousands of values once the universe expands. Persisting
        # all of it would materially inflate every agent prompt. Keep a small, deterministic set
        # for joint-path stress inspection while computing the per-symbol tail statistics above
        # from *all* observations. The selected paths must never be treated as an empirical
        # distribution (performance.py preserves that distinction explicitly).
        maximum_stored = int(scenario_selection["maximum_stored_scenarios"])
        severity = np.sqrt(converted.pow(2).mean(axis=1)).to_numpy(dtype=float)
        ranked_positions = list(np.argsort(-severity, kind="stable"))
        extreme_count = min(len(converted), maximum_stored // 2)
        chosen_positions = [int(position) for position in ranked_positions[:extreme_count]]
        grid_count = min(len(converted), maximum_stored - len(chosen_positions))
        if grid_count > 0:
            grid_positions = np.linspace(0, len(converted) - 1, num=grid_count)
            for position in np.rint(grid_positions).astype(int):
                if int(position) not in chosen_positions:
                    chosen_positions.append(int(position))
        # Rounding the time grid can collide with an extreme. Fill any gap deterministically,
        # preferring the remaining largest cross-sectional moves, then chronological coverage.
        for position in [*ranked_positions, *range(len(converted))]:
            if len(chosen_positions) >= min(maximum_stored, len(converted)):
                break
            if int(position) not in chosen_positions:
                chosen_positions.append(int(position))
        selected = converted.iloc[sorted(chosen_positions[:maximum_stored])]
        horizon_scenarios[str(horizon)] = {
            "available": True,
            "observations": len(converted),
            "minimum_observations": minimum_scenarios,
            "scenario_selection": scenario_selection,
            "stored_scenarios": len(selected),
            # Compatibility alias: historically this field meant the adverse lower return tail.
            "expected_shortfall_97_5_by_symbol": expected_shortfall_lower,
            "expected_shortfall_97_5_lower_by_symbol": expected_shortfall_lower,
            "expected_shortfall_97_5_upper_by_symbol": expected_shortfall_upper,
            "scenarios": [
                {
                    "end_ts": timestamp.isoformat(),
                    "residual_returns": {symbol: float(row[symbol]) for symbol in symbols},
                }
                for timestamp, row in selected.iterrows()
            ],
        }

    beta_closes = beta_estimation_closes or {}
    beta_uncertainty = {
        symbol: {
            "beta_btc": float(betas.get(symbol, 1.0)),
            **_beta_diagnostics(
                beta_closes.get(symbol, pd.Series(dtype=float)),
                beta_closes.get(btc_symbol, pd.Series(dtype=float)),
            ),
        }
        for symbol in symbols
    }
    return {
        "schema_version": 2,
        "as_of_ts": now.isoformat(),
        "return_label": "hourly_log_return_minus_beta_clamped_times_btc",
        "lookback_hours": lookback_hours,
        "available": True,
        "unavailable_reason": None,
        "minimum_common_samples": minimum_common_samples,
        "common_samples": len(frame),
        "covariance_min_eigenvalue": min_eigenvalue,
        "symbols": symbols,
        "residual_vol_annualized": residual_vol,
        "covariance_annualized": covariance_rows,
        "correlation": correlation_rows,
        "pairwise_samples": pairwise_samples,
        "high_correlation_pairs": high_pairs,
        "preferred_covariance_estimator": "ewma_diagonal_shrinkage",
        "sample_covariance_estimator": "equal_weight_complete_case",
        "ewma_half_life_hours": 72.0,
        "diagonal_shrinkage_intensity": shrinkage,
        "residual_vol_ewma_shrunk_annualized": preferred_residual_vol,
        "covariance_ewma_shrunk_annualized": preferred_covariance_rows,
        "correlation_ewma_shrunk": preferred_correlation_rows,
        "multi_window_estimators": multi_window,
        "residual_horizon_scenarios": horizon_scenarios,
        "beta_uncertainty": beta_uncertainty,
    }


def build_evidence(
    exchange,
    symbols: list[str],
    *,
    now: datetime,
    btc_symbol: str,
    risk_model_out: dict | None = None,
    risk_symbols: set[str] | None = None,
) -> list[EvidencePack]:
    """Assemble one EvidencePack per symbol from the exchange reads. Fail-soft per field.

    `btc_symbol` is ALWAYS included (even if it missed the top-N) so its mark is available for a PM
    BTC-hedge leg and so beta-to-BTC can be computed. Beta is the rolling beta of each coin's
    DAILY close series to BTC's (BTC itself = 1.0; insufficient history -> 1.0). Daily closes make
    the 45-sample lookback the ~45 DAYS the config documents — the prior 1h-bar feed made it 45
    HOURS and printed a beta of 10.42 on a crashed coin (the cycle-4 blowout input)."""
    all_syms = list(dict.fromkeys([*symbols, btc_symbol]))
    closes_by: dict[str, pd.Series] = {}
    hourly_series_by: dict[str, pd.Series] = {}
    hourly_closes_by: dict[str, list[float]] = {}
    rows: list[dict] = []
    for sym in all_syms:
        # funding carries the mark price, so fetch funding ONCE and reuse its mark — avoids a
        # redundant fetch_funding_rate per symbol (mark_price() hit the same endpoint), trimming
        # the per-cycle REST burst that was tripping Binance's rate-limit ban.
        fi = _safe(lambda: exchange.funding(sym), None)  # noqa: B023
        mark = float(getattr(fi, "mark_price", 0.0) or 0.0) if fi else 0.0
        if mark <= 0:  # funding read failed or lacked a mark — fall back to the dedicated call
            mark = _safe(lambda: float(exchange.mark_price(sym)), 0.0)  # noqa: B023
        if mark <= 0:
            continue
        # Candles are the price/momentum/beta evidence and are therefore REQUIRED, not fail-soft.
        # FuturesExchange routes both calls exclusively through the local Binance proxy and proves
        # that the currently-forming candle is present. Any proxy/staleness failure aborts evidence
        # before agents run instead of silently manufacturing a flat momentum read.
        hourly_frame = exchange.ohlcv(sym, timeframe="1h", limit=200)
        live_hourly_series = _timestamped_closes(
            hourly_frame,
            symbol=sym,
            timeframe="1h",
        )
        live_daily_series = _timestamped_closes(
            exchange.ohlcv(sym, timeframe="1d", limit=60),
            symbol=sym,
            timeframe="1d",
        )
        completed_hourly, hourly_statistics_as_of = _completed_candle_series(
            live_hourly_series,
            timeframe="1h",
            now=now,
        )
        completed_daily, daily_beta_as_of = _completed_candle_series(
            live_daily_series,
            timeframe="1d",
            now=now,
        )
        live_closes = live_hourly_series.tolist()
        if len(live_closes) < 2 or len(completed_hourly) < 2 or len(completed_daily) < 2:
            raise ValueError(f"insufficient required candle history for {sym}")
        # beta from daily closes when available (>=10 points), else fall back to the hourly
        # series (better than the 1.0 default for very young listings).
        beta_src = completed_daily if len(completed_daily) >= 10 else completed_hourly
        completed_closes = [float(value) for value in completed_hourly.tolist()]
        hourly_closes_by[sym] = completed_closes
        hourly_series_by[sym] = completed_hourly
        if len(beta_src) >= 2:
            closes_by[sym] = beta_src
        mom = (
            (completed_closes[-1] / completed_closes[0] - 1.0) * 100.0
            if len(completed_closes) >= 2 and completed_closes[0]
            else 0.0
        )
        momentum_windows = {
            hours: _return_pct(hourly_closes_by[sym], hours) for hours in (6, 24, 72, 168)
        }
        acceleration = momentum_windows[24] - _return_pct(hourly_closes_by[sym], 24, end_offset=24)
        rv = 0.0
        if len(completed_hourly) >= 3:
            rets = np.diff(np.log(np.clip(completed_hourly.to_numpy(), 1e-12, None)))
            rv = float(np.std(rets) * np.sqrt(len(rets)))
        fr = float(getattr(fi, "last_settled_rate", 0.0) or 0.0) if fi else 0.0
        interval = float(getattr(fi, "interval_hours", 8.0) or 8.0) if fi else 8.0
        apr = fr * (8760.0 / interval) if interval else 0.0
        last_8h_bps = fr * (8.0 / interval) * 1e4 if interval else 0.0
        history_since_ms = int((now - timedelta(hours=192)).timestamp() * 1000.0)
        funding_events = _safe(
            lambda symbol=sym, since=history_since_ms: exchange.funding_history(
                symbol,
                since_ms=since,
                limit=1000,
            ),
            [],
        )
        funding_features = _funding_history_features(
            funding_events,
            now=now,
            last_settled_rate=fr,
            current_interval_h=interval,
        )
        conservative_funding = float(funding_features["conservative_funding_8h_bps"])
        conservative_apr = conservative_funding * 1e-4 * (8760.0 / 8.0)
        idx = float(getattr(fi, "index_price", mark) or mark) if fi else mark
        basis = ((mark - idx) / idx * 1e4) if idx else 0.0
        oi_frame = _safe(
            lambda: exchange.open_interest_history(sym),  # noqa: B023
            pd.DataFrame(),
        )
        oi_features = _open_interest_features(oi_frame, now=now)
        lsr_frame = _safe(
            lambda: exchange.long_short_ratio(sym),  # noqa: B023
            pd.DataFrame(),
        )
        lsr_features = _long_short_features(lsr_frame, now=now)
        legacy_oi_change = oi_features.get("oi_contract_change_168h_pct")
        (
            d_bid,
            d_ask,
            liquidity_mid,
            spread,
            slip2k,
            slip_curve,
            slip_curve_buy,
            slip_curve_sell,
        ) = _depth_fields(exchange, sym)
        momentum_open_ts = live_hourly_series.index[-1].to_pydatetime()
        momentum_is_partial = live_hourly_series.index[-1] + pd.Timedelta(hours=1) > pd.Timestamp(
            now
        )
        intrabar_move = (
            (float(live_closes[-1]) / completed_closes[-1] - 1.0) * 100.0
            if completed_closes[-1] > 0.0
            else 0.0
        )
        rows.append(
            {
                "symbol": sym,
                "mark": mark,
                "momentum_mark": float(live_closes[-1]),
                "momentum_mark_observed_at": now,
                "momentum_candle_open_ts": momentum_open_ts,
                "momentum_mark_is_partial": bool(momentum_is_partial),
                "intrabar_move_from_last_completed_pct": intrabar_move,
                "hourly_statistics_as_of_ts": hourly_statistics_as_of,
                "daily_beta_as_of_ts": daily_beta_as_of,
                "momentum_pct": mom,
                "momentum_6h_pct": momentum_windows[6],
                "momentum_24h_pct": momentum_windows[24],
                "momentum_72h_pct": momentum_windows[72],
                "momentum_168h_pct": momentum_windows[168],
                "momentum_acceleration_24h_pct": acceleration,
                "drawdown_from_72h_high_pct": _drawdown_from_high_pct(hourly_closes_by[sym], 72),
                "realized_vol": rv,
                **_volume_features(hourly_frame, completed_hourly.index),
                "last_settled_funding_rate": fr,
                "last_settled_funding_8h_bps": last_8h_bps,
                **funding_features,
                "conservative_funding_apr": conservative_apr,
                "funding_rate": fr,
                "funding_apr": apr,
                "funding_interval_h": interval,
                "expected_funding_8h_bps": conservative_funding,
                "basis_bps": basis,
                **oi_features,
                "open_interest": float(oi_features["open_interest_usd"]),
                "oi_change_pct": float(legacy_oi_change or 0.0),
                **lsr_features,
                "depth_usd_bid": d_bid,
                "depth_usd_ask": d_ask,
                "liquidity_mid": liquidity_mid,
                "spread_bps": spread,
                "est_slippage_bps_2k": slip2k,
                "slippage_curve_bps": slip_curve,
                "slippage_curve_buy_bps": slip_curve_buy,
                "slippage_curve_sell_bps": slip_curve_sell,
            }
        )
    betas = beta_for_symbols(closes_by, btc_symbol=btc_symbol, lookback=45)
    if risk_model_out is not None:
        risk_model_out.clear()
        risk_model_out.update(
            _build_residual_risk_model(
                {
                    symbol: series
                    for symbol, series in hourly_series_by.items()
                    if risk_symbols is None or symbol in risk_symbols or symbol == btc_symbol
                },
                betas,
                btc_symbol=btc_symbol,
                now=now,
                beta_estimation_closes=closes_by,
            )
        )
    btc_hourly = hourly_closes_by.get(btc_symbol, [])
    btc_momentum = {hours: _return_pct(btc_hourly, hours) for hours in (6, 24, 72, 168)}
    btc_beta_returns = log_returns(closes_by.get(btc_symbol, pd.Series(dtype=float)))
    for row in rows:
        symbol = row["symbol"]
        raw = betas.get(symbol, 1.0)
        clamped = float(np.sign(raw) * min(abs(raw), BETA_CLAMP)) if raw else 0.0
        row.update(
            _beta_diagnostics(
                closes_by.get(symbol, pd.Series(dtype=float)),
                closes_by.get(btc_symbol, pd.Series(dtype=float)),
                is_btc=symbol == btc_symbol,
            )
        )
        row.update(
            _residual_path_features(
                hourly_series_by.get(symbol, pd.Series(dtype=float)),
                hourly_series_by.get(btc_symbol, pd.Series(dtype=float)),
                beta=clamped,
            )
        )
        for hours in (6, 24, 72, 168):
            row[f"beta_adjusted_momentum_{hours}h_pct"] = (
                float(row[f"momentum_{hours}h_pct"]) - clamped * btc_momentum[hours]
            )

    alpha_rows = [row for row in rows if row["symbol"] != btc_symbol]
    for hours in (24, 72, 168):
        values = np.asarray(
            [float(row[f"beta_adjusted_momentum_{hours}h_pct"]) for row in alpha_rows],
            dtype=float,
        )
        if len(values) >= 3 and np.isfinite(values).all():
            mean = float(values.mean())
            dispersion = float(values.std(ddof=0))
            ranks = pd.Series(values).rank(method="average", pct=True).to_numpy() * 100.0
            for index, row in enumerate(alpha_rows):
                row[f"beta_adjusted_cross_sectional_zscore_{hours}h"] = (
                    (float(values[index]) - mean) / dispersion if dispersion > 0.0 else None
                )
                row[f"beta_adjusted_cross_sectional_percentile_{hours}h"] = (
                    float(ranks[index]) if dispersion > 0.0 else None
                )

    packs: list[EvidencePack] = []
    for row in rows:
        asset_beta_returns = log_returns(closes_by.get(row["symbol"], pd.Series(dtype=float)))
        n_beta = min(
            len(pd.concat([asset_beta_returns, btc_beta_returns], axis=1, join="inner").dropna()),
            45,
        )
        raw = betas.get(row["symbol"], 1.0)
        clamped = float(np.sign(raw) * min(abs(raw), BETA_CLAMP)) if raw else 0.0
        packs.append(
            EvidencePack(
                **row, beta_btc=raw, beta_clamped=clamped, beta_n_samples=n_beta, as_of_ts=now
            )
        )
    return packs
