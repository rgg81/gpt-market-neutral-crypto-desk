"""Deterministic, agent-facing performance context for one desk cycle.

The agents decide what to trade.  This module only measures the PAPER account, recent desk
outcomes, and each role's recorded calibration so every agent can adapt to realized performance
instead of reasoning from a market snapshot in isolation.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from statistics import stdev

from futures_fund.account import load_account
from futures_fund.candle_proxy import validate_candle_audit
from futures_fund.cycle_io import cycle_dir
from futures_fund.metrics import PERIODS_PER_YEAR_DAILY, sharpe, sortino
from futures_fund.precheck import _one_way_friction_usd
from futures_fund.reconcile_commit import (
    completed_artifact_is_bound,
    completed_artifact_sha256,
    cycle_is_complete,
)
from futures_fund.reflection import (
    DAILY_LEARNING_HORIZON_HOURS,
    FORECAST_SCORE_SCHEMA_VERSION,
    SCHEDULED_MARK_TOLERANCE,
    daily_score_is_learning_eligible,
    read_candidate_scorecard,
    read_forecast_scorecard,
    score_record_is_manifest_bound,
)
from futures_fund.risk_context import position_correlation_context
from futures_fund.scorecard import (
    SCORECARD_MIGRATION_WAL_FILE,
    ScoreRecord,
    parse_score_record_json,
    pm_decision_edge_frac,
    realized_edge_frac,
)
from futures_fund.slippage import ExecutionRealism

_WINDOWS = (3, 5, 10)
_ROLES = ("sentiment", "technical", "futures")
_MAX_CARRY_HORIZON_INTERVALS = 40
_TREND_MOMENTUM_PCT = 20.0
_MIN_SHARPE_OBSERVATIONS = 20
_CALIBRATION_WINDOW_CYCLES = 30
_MIN_CALIBRATION_CYCLES = 12
_MIN_SPECIALIST_DIRECTIONAL_CALLS = 30
_MIN_SPECIALIST_OUTPUT_COVERAGE = 0.80
_FORECAST_CALIBRATION_HORIZONS = (24, 72, 168)
_MIN_FORECAST_HORIZON_OBSERVATIONS = 12
PERFORMANCE_SCHEMA_VERSION = 9
_BENCHMARK_POLICY_VERSION = 3
_MIN_INFORMATION_RATIO_OBSERVATIONS = 20

_COMMITTED_THESIS_SOURCE_POLICY = (
    "exact_position_reference_to_latest_prior_completed_manifest_bound_book"
)


def canonical_sha256(value) -> str:
    """Stable content binding used to reject a stale performance packet at reconcile."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()


def _as_utc(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"malformed JSONL row {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"non-object JSONL row {path}:{line_number}")
        rows.append(row)
    return rows


def _score_jsonl(path: Path) -> list[ScoreRecord]:
    """Load score rows with the scorecard's strict serialized-JSON contract."""
    if (path.parent / SCORECARD_MIGRATION_WAL_FILE).exists():
        raise ValueError("scorecard migration is incomplete; recover it before performance")
    if not path.exists():
        return []
    rows: list[ScoreRecord] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = parse_score_record_json(line)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"malformed score JSONL row {path}:{line_number}") from exc
        rows.append(record)
    return rows


def _dedupe_cycles(rows: list[dict]) -> tuple[list[dict], int]:
    """Canonicalize exact idempotent retries and reject conflicting cycle history."""
    by_cycle: dict[int, dict] = {}
    valid = 0
    for row in rows:
        try:
            cycle = int(row["cycle"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("JSONL cycle row lacks a valid integer cycle") from exc
        if cycle in by_cycle and by_cycle[cycle] != row:
            raise ValueError(f"conflicting duplicate JSONL cycle {cycle}")
        if cycle not in by_cycle:
            by_cycle[cycle] = row
        valid += 1
    return [by_cycle[c] for c in sorted(by_cycle)], valid - len(by_cycle)


def _recent_windows(ledger: list[dict], equity: float, starting_capital: float) -> dict[str, dict]:
    out: dict[str, dict] = {}
    closes = [
        float(row["closing_equity"])
        for row in ledger
        if float(row.get("closing_equity") or 0.0) > 0.0
    ]
    # The current evidence mark is a new endpoint after the latest completed close. Appending it
    # (rather than replacing that close) preserves the newest holding-period sign.
    endpoints = [starting_capital, *closes, equity]
    for size in _WINDOWS:
        used = min(size, len(endpoints) - 1)
        selected_endpoints = endpoints[-(used + 1) :]
        start_equity = selected_endpoints[0]
        changes = [
            selected_endpoints[i] - selected_endpoints[i - 1]
            for i in range(1, len(selected_endpoints))
        ]
        out[str(size)] = {
            "cycles_available": used,
            "start_equity": start_equity,
            "end_equity": equity,
            "pnl": equity - start_equity,
            "return_frac": (equity / start_equity - 1.0) if start_equity > 0.0 else 0.0,
            "winning_marks": sum(change > 0.0 for change in changes),
            "losing_marks": sum(change < 0.0 for change in changes),
            # The current mark adds no decision turnover, so at most used-1 completed ledger
            # transitions contribute turnover to this marked interval window.
            "turnover_usd": sum(
                float(row.get("turnover_usd") or 0.0) for row in ledger[-max(0, used - 1) :]
            )
            if used > 1
            else 0.0,
        }
    return out


def _row_time(row: dict, *keys: str) -> datetime | None:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return _as_utc(str(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError("performance scorecard contains an invalid ScoreRecord") from exc
    return None


def _time_based_performance(
    ledger: list[dict],
    heartbeats: list[dict],
    *,
    now: datetime,
    equity: float,
) -> dict:
    """Calendar-time returns and risk statistics from timestamped official PAPER marks.

    Daily Sharpe inputs use the fixed 16:07 UTC heartbeat only. A multi-day gap is decomposed into
    geometric daily-equivalent returns so its full compounded PnL is preserved, but any gap keeps
    the Sharpe diagnostic non-usable. Rolling/calendar returns use the closest recorded mark at or
    before their exact boundary and fail availability when that boundary is stale.
    """
    events: list[tuple[datetime, float, str]] = []
    for row in ledger:
        ts = _row_time(row, "ts")
        value = float(row.get("closing_equity") or 0.0)
        if ts is not None and value > 0.0 and ts <= now:
            events.append((ts, value, "cycle_close"))
    daily_by_date: dict[str, tuple[datetime, float]] = {}
    for row in heartbeats:
        ts = _row_time(row, "ts")
        value = float(row.get("equity") or 0.0)
        if ts is None or value <= 0.0 or ts > now:
            continue
        events.append((ts, value, "heartbeat"))
        slot = _row_time(row, "schedule_slot") or ts
        if slot.hour == 16 and slot.minute == 7:
            key = slot.date().isoformat()
            prior = daily_by_date.get(key)
            if prior is None or ts > prior[0]:
                daily_by_date[key] = (ts, value)
    events.append((now, equity, "current_evidence"))
    events.sort(key=lambda item: item[0])

    daily_cutoff = now.date() - timedelta(days=28)
    daily = [
        item
        for item in sorted(daily_by_date.values(), key=lambda item: item[0])
        if item[0].date() >= daily_cutoff
    ]
    daily_returns: list[float] = []
    interior_daily_gaps = 0
    for (prev_ts, prev_equity), (cur_ts, cur_equity) in zip(daily, daily[1:], strict=False):
        day_gap = (cur_ts.date() - prev_ts.date()).days
        if day_gap <= 0 or prev_equity <= 0.0 or cur_equity <= 0.0:
            continue
        interior_daily_gaps += max(0, day_gap - 1)
        daily_equivalent = (cur_equity / prev_equity) ** (1.0 / day_gap) - 1.0
        daily_returns.extend([daily_equivalent] * day_gap)
    latest_daily_boundary = now.replace(hour=16, minute=7, second=0, microsecond=0)
    if latest_daily_boundary > now:
        latest_daily_boundary -= timedelta(days=1)
    trailing_daily_gaps = (
        max(0, (latest_daily_boundary.date() - daily[-1][0].date()).days) if daily else 0
    )
    daily_gaps = interior_daily_gaps + trailing_daily_gaps

    diagnostic_downside_deviation = (
        math.sqrt(sum(min(value, 0.0) ** 2 for value in daily_returns) / len(daily_returns))
        if daily_returns
        else 0.0
    )
    diagnostic_mean = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    diagnostic_volatility = stdev(daily_returns) if len(daily_returns) >= 2 else 0.0
    diagnostic_profitable_rate = (
        sum(value > 0.0 for value in daily_returns) / len(daily_returns) if daily_returns else None
    )
    diagnostic_sharpe = sharpe(daily_returns, periods_per_year=PERIODS_PER_YEAR_DAILY)
    raw_sortino = sortino(daily_returns, periods_per_year=PERIODS_PER_YEAR_DAILY)
    diagnostic_sortino = raw_sortino if math.isfinite(raw_sortino) else None
    gap_compounded_return = math.prod(1.0 + value for value in daily_returns) - 1.0
    metrics_usable = daily_gaps == 0

    def boundary_return(boundary: datetime) -> dict:
        candidates = [item for item in events if item[0] <= boundary]
        if not candidates:
            return {
                "available": False,
                "return_frac": None,
                "pnl": None,
                "boundary_ts": boundary.isoformat(),
                "baseline_ts": None,
                "baseline_age_hours": None,
            }
        baseline_ts, baseline_equity, source = candidates[-1]
        age = (boundary - baseline_ts).total_seconds() / 3600.0
        fresh = age <= 24.0
        diagnostic_return = equity / baseline_equity - 1.0
        diagnostic_pnl = equity - baseline_equity
        return {
            "available": fresh,
            "return_frac": diagnostic_return if fresh else None,
            "pnl": diagnostic_pnl if fresh else None,
            "diagnostic_return_frac": diagnostic_return,
            "diagnostic_pnl": diagnostic_pnl,
            "boundary_ts": boundary.isoformat(),
            "baseline_ts": baseline_ts.isoformat(),
            "baseline_equity": baseline_equity,
            "baseline_source": source,
            "baseline_age_hours": age,
            "boundary_fresh": fresh,
            "coverage_status": "fresh" if fresh else "stale_boundary",
        }

    rolling = {str(days): boundary_return(now - timedelta(days=days)) for days in (7, 28)}
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = week_start - timedelta(days=week_start.weekday())
    calendar_week = boundary_return(week_start)

    # A completed week is represented only by its official Sunday 16:07 UTC mark. Sparse events
    # from early in a week are not silently promoted to a weekly close.
    weekly_closes: dict[tuple[int, int], tuple[datetime, float]] = {}
    for ts, value in daily:
        if ts.weekday() != 6:
            continue
        iso = ts.isocalendar()
        key = (iso.year, iso.week)
        weekly_closes[key] = (ts, value)
    latest_weekly_boundary = now.replace(hour=16, minute=7, second=0, microsecond=0)
    latest_weekly_boundary -= timedelta(days=(latest_weekly_boundary.weekday() - 6) % 7)
    if latest_weekly_boundary > now:
        latest_weekly_boundary -= timedelta(days=7)
    latest_weekly_iso = latest_weekly_boundary.isocalendar()
    latest_weekly_key = (latest_weekly_iso.year, latest_weekly_iso.week)
    completed = [
        (key, item) for key, item in sorted(weekly_closes.items()) if key <= latest_weekly_key
    ]
    weekly_returns: list[float] = []
    interior_week_gaps = 0
    for (prev_key, prev), (cur_key, cur) in zip(completed, completed[1:], strict=False):
        prev_monday = datetime.fromisocalendar(prev_key[0], prev_key[1], 1).date()
        cur_monday = datetime.fromisocalendar(cur_key[0], cur_key[1], 1).date()
        week_gap = (cur_monday - prev_monday).days // 7
        if week_gap != 1:
            interior_week_gaps += max(0, week_gap - 1)
            continue
        if prev[1] > 0.0:
            weekly_returns.append(cur[1] / prev[1] - 1.0)
    trailing_week_gaps = 0
    if completed:
        last_key = completed[-1][0]
        last_monday = datetime.fromisocalendar(last_key[0], last_key[1], 1).date()
        expected_monday = datetime.fromisocalendar(
            latest_weekly_key[0], latest_weekly_key[1], 1
        ).date()
        trailing_week_gaps = max(0, (expected_monday - last_monday).days // 7)
    missing_week_gaps = interior_week_gaps + trailing_week_gaps
    weekly_metrics_usable = missing_week_gaps == 0
    diagnostic_profitable_week_rate = (
        sum(value > 0.0 for value in weekly_returns) / len(weekly_returns)
        if weekly_returns
        else None
    )

    if daily_gaps > 0:
        sortino_status = "gapped_history"
    elif len(daily_returns) < _MIN_SHARPE_OBSERVATIONS:
        sortino_status = "insufficient_history"
    elif diagnostic_sortino is None:
        sortino_status = "no_downside_observations"
    else:
        sortino_status = "usable"

    return {
        "official_daily_mark": "16:07 UTC token-free heartbeat",
        "daily_observations": len(daily),
        "daily_return_observations": len(daily_returns),
        "missing_daily_gaps": daily_gaps,
        "missing_interior_daily_marks": interior_daily_gaps,
        "missing_trailing_daily_marks": trailing_daily_gaps,
        "latest_expected_daily_mark_ts": latest_daily_boundary.isoformat(),
        # A missing official close does not reveal the path within that gap. Geometric daily
        # equivalents preserve the total PnL but are synthetic, so they must never masquerade as
        # observed volatility, hit-rate, Sharpe, or Sortino inputs.
        "daily_mean_return_frac": diagnostic_mean if metrics_usable else None,
        "daily_volatility_frac": diagnostic_volatility if metrics_usable else None,
        "downside_deviation_frac": (diagnostic_downside_deviation if metrics_usable else None),
        "sharpe_annualized": diagnostic_sharpe if metrics_usable else None,
        "sortino_annualized": diagnostic_sortino if metrics_usable else None,
        "diagnostic_geometric_daily_equivalent_mean_return_frac": diagnostic_mean,
        "diagnostic_geometric_daily_equivalent_volatility_frac": diagnostic_volatility,
        "diagnostic_geometric_daily_equivalent_downside_deviation_frac": (
            diagnostic_downside_deviation
        ),
        "diagnostic_geometric_daily_equivalent_sharpe_annualized": diagnostic_sharpe,
        "diagnostic_geometric_daily_equivalent_sortino_annualized": diagnostic_sortino,
        "diagnostic_geometric_daily_equivalent_profitable_rate": diagnostic_profitable_rate,
        "diagnostic_gap_compounded_return_frac": gap_compounded_return,
        "sortino_status": sortino_status,
        "sortino_computation_status": (
            "synthetic_gap_returns"
            if daily_gaps > 0
            else "no_downside_observations"
            if not math.isfinite(raw_sortino)
            else "finite"
        ),
        "sharpe_status": (
            "gapped_history"
            if daily_gaps > 0
            else "usable"
            if len(daily_returns) >= _MIN_SHARPE_OBSERVATIONS
            else "insufficient_history"
        ),
        "minimum_sharpe_observations": _MIN_SHARPE_OBSERVATIONS,
        "profitable_daily_rate": diagnostic_profitable_rate if metrics_usable else None,
        "rolling_windows": rolling,
        "calendar_week_to_date": calendar_week,
        "completed_week_return_observations": len(weekly_returns),
        "completed_week_close_observations": len(completed),
        "missing_completed_week_gaps": missing_week_gaps,
        "missing_interior_completed_weeks": interior_week_gaps,
        "missing_trailing_completed_weeks": trailing_week_gaps,
        "latest_expected_completed_week_ts": latest_weekly_boundary.isoformat(),
        "completed_week_status": (
            "gapped_history"
            if missing_week_gaps > 0
            else "usable"
            if weekly_returns
            else "insufficient_history"
        ),
        "profitable_week_rate": (
            diagnostic_profitable_week_rate if weekly_metrics_usable else None
        ),
        "worst_completed_week_return_frac": (
            min(weekly_returns) if weekly_returns and weekly_metrics_usable else None
        ),
        "diagnostic_profitable_week_rate": diagnostic_profitable_week_rate,
        "diagnostic_worst_completed_week_return_frac": (
            min(weekly_returns) if weekly_returns else None
        ),
    }


def _portfolio_risk_context(
    account,
    marks: dict[str, float],
    equity: float,
    model: dict,
    evidence_by_symbol: dict[str, dict] | None = None,
) -> dict:
    """Describe current alpha risk from the cycle's beta-residual covariance evidence."""
    if model.get("available", True) is not True:
        return {
            "available": False,
            "unavailable_reason": model.get("unavailable_reason") or "risk model unavailable",
            "return_label": model.get("return_label"),
            "lookback_hours": model.get("lookback_hours"),
        }
    btc_symbol = "BTC/USDT:USDT"
    alpha_symbols = sorted(
        symbol
        for symbol, position in account.positions.items()
        if not (symbol == btc_symbol and position.seat_role == "hedge")
    )
    covariance = (
        model.get("covariance_ewma_shrunk_annualized") or model.get("covariance_annualized") or {}
    )
    vols = (
        model.get("residual_vol_ewma_shrunk_annualized")
        or model.get("residual_vol_annualized")
        or {}
    )
    missing = sorted(
        symbol for symbol in alpha_symbols if symbol not in covariance or vols.get(symbol) is None
    )
    if missing:
        return {
            "available": False,
            "missing_alpha_symbols": missing,
            "return_label": model.get("return_label"),
            "lookback_hours": model.get("lookback_hours"),
        }

    signed_exposure = {
        symbol: (
            (1.0 if account.positions[symbol].direction == "long" else -1.0)
            * account.positions[symbol].qty
            * marks[symbol]
        )
        for symbol in alpha_symbols
    }
    variance_usd = 0.0
    covariance_vector: dict[str, float] = {}
    for left in alpha_symbols:
        value = 0.0
        for right in alpha_symbols:
            raw_cov = covariance.get(left, {}).get(right)
            if raw_cov is None:
                return {
                    "available": False,
                    "missing_covariance_pair": [left, right],
                    "return_label": model.get("return_label"),
                    "lookback_hours": model.get("lookback_hours"),
                }
            value += float(raw_cov) * signed_exposure[right]
        covariance_vector[left] = value
        variance_usd += signed_exposure[left] * value
    if not math.isfinite(variance_usd) or variance_usd < -1e-8:
        return {
            "available": False,
            "unavailable_reason": "held-book covariance produced negative variance",
            "return_label": model.get("return_label"),
            "lookback_hours": model.get("lookback_hours"),
        }
    variance_usd = max(variance_usd, 0.0)
    portfolio_vol_usd = math.sqrt(variance_usd)

    standalone = {
        symbol: abs(signed_exposure[symbol]) * float(vols[symbol]) for symbol in alpha_symbols
    }
    standalone_total = sum(standalone.values())
    seats = []
    for symbol in alpha_symbols:
        variance_contribution = signed_exposure[symbol] * covariance_vector[symbol]
        seats.append(
            {
                "symbol": symbol,
                "side": account.positions[symbol].direction,
                "signed_notional": signed_exposure[symbol],
                "residual_vol_annualized": float(vols[symbol]),
                "standalone_vol_usd": standalone[symbol],
                "standalone_risk_share": (
                    standalone[symbol] / standalone_total if standalone_total > 0.0 else 0.0
                ),
                "variance_contribution_frac": (
                    variance_contribution / variance_usd if variance_usd > 0.0 else 0.0
                ),
            }
        )
    seats.sort(key=lambda row: row["standalone_risk_share"], reverse=True)
    long_risk = sum(
        standalone[symbol]
        for symbol in alpha_symbols
        if account.positions[symbol].direction == "long"
    )
    short_risk = standalone_total - long_risk
    side_by_symbol = {symbol: account.positions[symbol].direction for symbol in alpha_symbols}
    correlation_context = position_correlation_context(
        model.get("high_correlation_pairs", []),
        side_by_symbol=side_by_symbol,
        standalone_risk=standalone,
        variance_contribution_frac={
            str(row["symbol"]): float(row["variance_contribution_frac"]) for row in seats
        },
    )
    multi_window_volatility = {}
    for window, packet in (model.get("multi_window_estimators") or {}).items():
        window_covariance = packet.get("covariance_annualized") or {}
        if packet.get("available") is not True or any(
            window_covariance.get(left, {}).get(right) is None
            for left in alpha_symbols
            for right in alpha_symbols
        ):
            multi_window_volatility[str(window)] = {
                "available": False,
                "samples": packet.get("samples"),
                "vol_annualized_usd": None,
                "vol_annualized_frac_equity": None,
            }
            continue
        window_variance = sum(
            signed_exposure[left]
            * sum(
                signed_exposure[right] * float(window_covariance[left][right])
                for right in alpha_symbols
            )
            for left in alpha_symbols
        )
        if not math.isfinite(window_variance) or window_variance < -1e-8:
            multi_window_volatility[str(window)] = {
                "available": False,
                "samples": packet.get("samples"),
                "vol_annualized_usd": None,
                "vol_annualized_frac_equity": None,
            }
            continue
        window_vol = math.sqrt(max(window_variance, 0.0))
        multi_window_volatility[str(window)] = {
            "available": True,
            "samples": packet.get("samples"),
            "vol_annualized_usd": window_vol,
            "vol_annualized_frac_equity": window_vol / equity if equity > 0.0 else None,
        }

    historical_stress = {}
    for horizon, packet in (model.get("residual_horizon_scenarios") or {}).items():
        scenarios = packet.get("scenarios") or []
        pnl_values = []
        for scenario in scenarios:
            returns = scenario.get("residual_returns") or {}
            if all(symbol in returns for symbol in alpha_symbols):
                pnl_values.append(
                    sum(
                        signed_exposure[symbol] * float(returns[symbol]) for symbol in alpha_symbols
                    )
                )
        scenario_selection = packet.get("scenario_selection")
        if isinstance(scenario_selection, dict):
            lower_tail = (
                packet.get("expected_shortfall_97_5_lower_by_symbol")
                or packet.get("expected_shortfall_97_5_by_symbol")
                or {}
            )
            upper_tail = packet.get("expected_shortfall_97_5_upper_by_symbol") or {}
            missing_tail_symbols = sorted(
                symbol
                for symbol in alpha_symbols
                if symbol not in lower_tail or symbol not in upper_tail
            )
            marginal_tail_sum = None
            if not missing_tail_symbols:
                marginal_tail_sum = sum(
                    signed_exposure[symbol]
                    * float(
                        lower_tail[symbol]
                        if signed_exposure[symbol] >= 0.0
                        else upper_tail[symbol]
                    )
                    for symbol in alpha_symbols
                )
            curated_available = packet.get("available") is True and bool(pnl_values)
            worst_three = sorted(pnl_values)[:3]
            historical_stress[str(horizon)] = {
                "available": curated_available,
                "measure": "curated_joint_stress_plus_componentwise_marginal_tail_sum",
                "observations": packet.get("observations", 0),
                "stored_scenarios": len(pnl_values),
                "scenario_selection": scenario_selection,
                # A deliberately selected stress set is not a probability sample. Do not report
                # its order statistics under VaR/ES labels.
                "full_distribution_portfolio_es_available": False,
                "var_97_5_usd": None,
                "expected_shortfall_97_5_usd": None,
                "expected_shortfall_97_5_frac_equity": None,
                "curated_worst_joint_scenario_usd": min(pnl_values) if pnl_values else None,
                "curated_mean_worst_3_joint_scenarios_usd": (
                    sum(worst_three) / len(worst_three) if worst_three else None
                ),
                "componentwise_marginal_tail_sum_97_5_usd": marginal_tail_sum,
                "componentwise_marginal_tail_sum_97_5_frac_equity": (
                    marginal_tail_sum / equity
                    if marginal_tail_sum is not None and equity > 0.0
                    else None
                ),
                "missing_componentwise_tail_symbols": missing_tail_symbols,
                "warning": (
                    "Stored joint paths are deliberately curated and cannot estimate portfolio "
                    "VaR/ES; the componentwise marginal tail sum uses full-sample univariate "
                    "tails but is not a joint expected-shortfall estimate."
                ),
            }
            continue
        if packet.get("available") is not True or len(pnl_values) < 12:
            historical_stress[str(horizon)] = {
                "available": False,
                "observations": len(pnl_values),
                "var_97_5_usd": None,
                "expected_shortfall_97_5_usd": None,
                "worst_usd": None,
            }
            continue
        threshold = sorted(pnl_values)[max(0, math.ceil(0.025 * len(pnl_values)) - 1)]
        tail = [value for value in pnl_values if value <= threshold]
        historical_stress[str(horizon)] = {
            "available": True,
            "observations": len(pnl_values),
            "var_97_5_usd": threshold,
            "expected_shortfall_97_5_usd": sum(tail) / len(tail),
            "worst_usd": min(pnl_values),
            "expected_shortfall_97_5_frac_equity": (
                (sum(tail) / len(tail)) / equity if equity > 0.0 else None
            ),
        }

    beta_uncertainty_rows = model.get("beta_uncertainty") or {}
    beta_standard_error_usd = math.sqrt(
        sum(
            (
                abs(signed_exposure[symbol])
                * float(beta_uncertainty_rows.get(symbol, {}).get("beta_standard_error") or 0.0)
            )
            ** 2
            for symbol in alpha_symbols
        )
    )
    evidence_rows = evidence_by_symbol or {}
    alpha_gross = sum(abs(value) for value in signed_exposure.values())
    factor_fields = {
        "residual_momentum_z_24h": "beta_adjusted_cross_sectional_zscore_24h",
        "residual_momentum_z_72h": "beta_adjusted_cross_sectional_zscore_72h",
        "residual_momentum_z_168h": "beta_adjusted_cross_sectional_zscore_168h",
        "conservative_funding_bps": "conservative_funding_8h_bps",
        "realized_vol": "realized_vol",
    }
    factor_exposures = {}
    for factor, field in factor_fields.items():
        covered = [
            symbol
            for symbol in alpha_symbols
            if evidence_rows.get(symbol, {}).get(field) is not None
        ]
        factor_exposures[factor] = {
            "available": bool(covered) and alpha_gross > 0.0,
            "coverage_frac_gross": (
                sum(abs(signed_exposure[symbol]) for symbol in covered) / alpha_gross
                if alpha_gross > 0.0
                else 0.0
            ),
            "signed_notional_weighted_exposure": (
                sum(
                    signed_exposure[symbol] * float(evidence_rows[symbol][field])
                    for symbol in covered
                )
                / alpha_gross
                if covered and alpha_gross > 0.0
                else None
            ),
        }
    return {
        "available": True,
        "covariance_estimator": model.get("preferred_covariance_estimator") or "legacy_sample",
        "return_label": model.get("return_label"),
        "lookback_hours": model.get("lookback_hours"),
        "alpha_symbols": alpha_symbols,
        "portfolio_residual_vol_annualized_usd": portfolio_vol_usd,
        "portfolio_residual_vol_annualized_frac_equity": (
            portfolio_vol_usd / equity if equity > 0.0 else 0.0
        ),
        "standalone_vol_usd_total": standalone_total,
        "long_standalone_vol_usd": long_risk,
        "short_standalone_vol_usd": short_risk,
        "long_short_standalone_risk_ratio": (long_risk / short_risk if short_risk > 0.0 else None),
        "max_alpha_standalone_risk_symbol": seats[0]["symbol"] if seats else "",
        "max_alpha_standalone_risk_share": (seats[0]["standalone_risk_share"] if seats else 0.0),
        "seats": seats,
        "multi_window_portfolio_volatility": multi_window_volatility,
        "historical_residual_stress": historical_stress,
        "beta_estimation_uncertainty": {
            "available": bool(beta_uncertainty_rows),
            "aggregation_assumption": "independent_symbol_beta_estimation_errors",
            "portfolio_beta_standard_error_usd": beta_standard_error_usd,
            "portfolio_beta_ci95_half_width_usd": 1.96 * beta_standard_error_usd,
        },
        "factor_exposures": factor_exposures,
        **correlation_context,
        "universe_high_correlation_pairs": model.get("high_correlation_pairs", []),
    }


def _closed_leg_performance(state: Path, account, *, limit: int = 20) -> dict:
    """Expose exact realized lifecycle outcomes, including settled funding and all frictions."""
    rows: list[dict] = []
    cycle_root = state / "rebal" / "cycle"
    if cycle_root.exists():
        cycle_dirs = sorted(
            (
                path
                for path in cycle_root.iterdir()
                if path.is_dir()
                and path.name.isdigit()
                and cycle_is_complete(state, int(path.name), cadence="rebal")
            ),
            key=lambda path: int(path.name),
        )
        for path in cycle_dirs:
            outcome_path = path / "closed_legs.json"
            if not outcome_path.exists() or not completed_artifact_is_bound(
                state, int(path.name), "closed_legs"
            ):
                continue
            raw = json.loads(outcome_path.read_text())
            if not isinstance(raw, list):
                raise ValueError(f"closed-leg artifact is not a list: {outcome_path}")
            rows.extend({**item, "recorded_cycle": int(path.name)} for item in raw)
    # Legacy carrier: outcomes closed before durable lifecycle artifacts were deployed. They remain
    # exact account data and will be atomically drained into the next completed generation.
    rows.extend(
        {**leg.model_dump(mode="json"), "recorded_cycle": None} for leg in account.closed_legs
    )
    total_outcomes = len(rows)
    enriched = []
    for row in rows[-limit:]:
        funding = float(row.get("realized_funding") or 0.0)
        fees = float(row.get("fees") or 0.0)
        slippage = float(row.get("slippage") or 0.0)
        price_pnl = float(row.get("realized_pnl") or 0.0)
        enriched.append(
            {
                **row,
                "realized_price_pnl": price_pnl,
                "realized_funding": funding,
                "fees": fees,
                "slippage": slippage,
                "realized_lifecycle_net_pnl": price_pnl + funding - fees - slippage,
            }
        )
    return {
        "count": len(enriched),
        "total_count": total_outcomes,
        "limit": limit,
        "truncated": total_outcomes > len(enriched),
        "normalization_status": (
            "dollar_pnl_only_for_legacy_records; entry_notional/duration unavailable"
        ),
        "realized_price_pnl": sum(row["realized_price_pnl"] for row in enriched),
        "realized_funding": sum(row["realized_funding"] for row in enriched),
        "fees": sum(row["fees"] for row in enriched),
        "slippage": sum(row["slippage"] for row in enriched),
        "realized_lifecycle_net_pnl": sum(row["realized_lifecycle_net_pnl"] for row in enriched),
        "outcomes": enriched,
    }


def _forecast_time_cohorts(
    sample: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Group exact outcome windows, then select a non-overlapping temporal cohort sample.

    Cross-sectional legs sharing one market interval are one statistical observation. Distinct
    windows are selected with deterministic earliest-finish interval scheduling, using only the
    same small scheduler tolerance as forecast maturity scoring.
    """
    grouped: dict[tuple, list[dict]] = {}
    interval_by_key: dict[tuple, tuple[datetime, datetime]] = {}
    for row in sample:
        start = _as_utc(str(row["origin_ts"]))
        end = _as_utc(str(row["evaluated_at"]))
        if end <= start:
            raise ValueError("forecast outcome interval must end after its origin")
        key = (
            start.isoformat(),
            end.isoformat(),
            int(row["forecast_horizon_hours"]),
            int(row["outcome_observation_cycle"]),
            str(row["outcome_scoring_marks_sha256"]),
        )
        grouped.setdefault(key, []).append(row)
        interval_by_key[key] = (start, end)

    candidates = []
    for key, rows in grouped.items():
        start, end = interval_by_key[key]
        ordered_rows = sorted(rows, key=lambda row: (int(row["origin_cycle"]), str(row["symbol"])))
        candidates.append(
            {
                "cohort_id": sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest(),
                "origin_ts": start,
                "evaluated_at": end,
                "outcome_observation_cycle": int(key[3]),
                "outcome_scoring_marks_sha256": str(key[4]),
                "rows": ordered_rows,
            }
        )
    candidates.sort(
        key=lambda cohort: (cohort["evaluated_at"], cohort["origin_ts"], cohort["cohort_id"])
    )

    selected: list[dict] = []
    overlapping: list[dict] = []
    prior_end: datetime | None = None
    for cohort in candidates:
        if prior_end is None or cohort["origin_ts"] >= prior_end - SCHEDULED_MARK_TOLERANCE:
            selected.append(cohort)
            prior_end = cohort["evaluated_at"]
        else:
            overlapping.append(cohort)
    return selected, overlapping


def _forecast_cohort_audit(cohort: dict) -> dict:
    rows = cohort["rows"]
    return {
        "cohort_id": cohort["cohort_id"],
        "origin_ts": cohort["origin_ts"].isoformat(),
        "evaluated_at": cohort["evaluated_at"].isoformat(),
        "outcome_observation_cycle": cohort["outcome_observation_cycle"],
        "outcome_scoring_marks_sha256": cohort["outcome_scoring_marks_sha256"],
        "leg_n": len(rows),
        "symbols": [str(row["symbol"]) for row in rows],
    }


def _forecast_metric_summary(time_cohorts: list[dict]) -> dict:
    """Average cohort-level metrics so cross-sectional leg count cannot inflate effective n."""
    sample = [row for cohort in time_cohorts for row in cohort["rows"]]
    leg_n = len(sample)
    cohort_n = len(time_cohorts)

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def cohort_mean(field: str) -> list[float]:
        return [
            sum(float(row[field]) for row in cohort["rows"]) / len(cohort["rows"])
            for cohort in time_cohorts
        ]

    errors = cohort_mean("forecast_error_frac")
    absolute_errors = [
        sum(abs(float(row["forecast_error_frac"])) for row in cohort["rows"]) / len(cohort["rows"])
        for cohort in time_cohorts
    ]
    profit_rates = [
        sum(bool(row["selected_side_profitable"]) for row in cohort["rows"]) / len(cohort["rows"])
        for cohort in time_cohorts
    ]
    directional_rows = [
        row for row in sample if isinstance(row.get("directional_forecast_hit"), bool)
    ]
    directional_cohort_rates = []
    for cohort in time_cohorts:
        covered = [
            row for row in cohort["rows"] if isinstance(row.get("directional_forecast_hit"), bool)
        ]
        if covered:
            directional_cohort_rates.append(
                sum(bool(row["directional_forecast_hit"]) for row in covered) / len(covered)
            )

    def weighted_cohort_means(weight_field: str, value_field: str) -> tuple[list[float], int]:
        values = []
        covered_legs = 0
        for cohort in time_cohorts:
            weighted_rows = []
            for row in cohort["rows"]:
                raw_weight = row.get(weight_field)
                weight = float(raw_weight) if raw_weight is not None else 0.0
                if weight > 0.0:
                    covered_legs += 1
                    weighted_rows.append((weight, float(row[value_field])))
            if len(weighted_rows) == len(cohort["rows"]):
                total_weight = sum(weight for weight, _value in weighted_rows)
                values.append(sum(weight * value for weight, value in weighted_rows) / total_weight)
        return values, covered_legs

    notional_predicted, notional_observations = weighted_cohort_means(
        "target_notional", "predicted_selected_edge_frac"
    )
    notional_realized, _ = weighted_cohort_means("target_notional", "realized_selected_edge_frac")
    risk_predicted, risk_observations = weighted_cohort_means(
        "origin_standalone_vol_usd", "predicted_selected_edge_frac"
    )
    risk_realized, _ = weighted_cohort_means(
        "origin_standalone_vol_usd", "realized_selected_edge_frac"
    )
    enough_history = cohort_n >= _MIN_FORECAST_HORIZON_OBSERVATIONS
    notional_complete = leg_n > 0 and notional_observations == leg_n
    risk_complete = leg_n > 0 and risk_observations == leg_n
    directional_accuracy = mean(directional_cohort_rates)
    return {
        # ``n`` is retained as a compatibility alias, but is explicitly the temporal effective n.
        "n": cohort_n,
        "n_semantics": "independent_nonoverlapping_time_outcome_cohorts",
        "leg_n": leg_n,
        "independent_time_cohort_n": cohort_n,
        "effective_n": cohort_n,
        "minimum_observations": _MIN_FORECAST_HORIZON_OBSERVATIONS,
        "minimum_independent_time_cohorts": _MIN_FORECAST_HORIZON_OBSERVATIONS,
        "calibration_status": (
            "usable" if enough_history else "insufficient_independent_time_cohorts"
        ),
        "metric_aggregation": "equal_weighted_time_cohort_means",
        "mean_predicted_selected_edge_frac": mean(cohort_mean("predicted_selected_edge_frac")),
        "mean_realized_selected_edge_frac": mean(cohort_mean("realized_selected_edge_frac")),
        "forecast_bias_frac": mean(errors),
        "mean_forecast_error_frac": mean(errors),
        "mean_absolute_forecast_error_frac": mean(absolute_errors),
        "selected_side_profit_rate": mean(profit_rates),
        "directional_observations": len(directional_rows),
        "directional_time_cohort_observations": len(directional_cohort_rates),
        "directional_coverage_frac": (len(directional_rows) / leg_n if leg_n else None),
        "directional_forecast_accuracy_rate": directional_accuracy,
        "sign_hit_rate": directional_accuracy,
        "notional_weighted_observations": notional_observations,
        "notional_weighted_time_cohort_observations": len(notional_predicted),
        "notional_weight_coverage_frac": (notional_observations / leg_n if leg_n else None),
        "notional_weighted_status": (
            "usable"
            if enough_history and notional_complete
            else "insufficient_independent_time_cohorts"
            if not enough_history
            else "incomplete_coverage"
        ),
        "notional_weighted_predicted_selected_edge_frac": (
            mean(notional_predicted) if notional_complete else None
        ),
        "notional_weighted_realized_selected_edge_frac": (
            mean(notional_realized) if notional_complete else None
        ),
        "residual_risk_weighted_observations": risk_observations,
        "residual_risk_weighted_time_cohort_observations": len(risk_predicted),
        "residual_risk_weight_coverage_frac": (risk_observations / leg_n if leg_n else None),
        "risk_capacity_status": (
            "usable"
            if enough_history and risk_complete
            else "insufficient_independent_time_cohorts"
            if not enough_history
            else "incomplete_coverage"
        ),
        "residual_risk_weighted_predicted_selected_edge_frac": (
            mean(risk_predicted) if risk_complete else None
        ),
        "residual_risk_weighted_realized_selected_edge_frac": (
            mean(risk_realized) if risk_complete else None
        ),
        "time_cohorts": [_forecast_cohort_audit(cohort) for cohort in time_cohorts],
    }


def _forecast_performance(rows: list[dict], *, limit: int = 30) -> dict:
    audit_recent = rows[-limit:]
    current_policy_rows = [
        row
        for row in rows
        if int(row.get("forecast_score_schema_version", 1)) >= FORECAST_SCORE_SCHEMA_VERSION
    ]
    decision_eligible_all = [
        row
        for row in current_policy_rows
        if row.get("horizon_label_eligible") is True
        and row.get("decision_learning_eligible") is True
    ]
    leg_nonoverlap_all = [
        row
        for row in decision_eligible_all
        if row.get("learning_eligible") is True and row.get("leg_nonoverlap_eligible") is True
    ]
    global_cohorts, _global_temporal_overlaps = _forecast_time_cohorts(leg_nonoverlap_all)
    recent_cohorts = global_cohorts[-limit:]
    overlapping_decisions = [
        row for row in decision_eligible_all if row.get("leg_nonoverlap_eligible") is not True
    ]
    aggregate = _forecast_metric_summary(recent_cohorts)
    aggregate["calibration_status"] = "context_only_cross_horizon"
    aggregate["notional_weighted_status"] = "context_only_cross_horizon"
    aggregate["risk_capacity_status"] = "context_only_cross_horizon"
    by_horizon = {}
    temporal_overlap_keys: set[tuple[int, str]] = set()
    temporal_overlap_cohort_n = 0
    for horizon in _FORECAST_CALIBRATION_HORIZONS:
        horizon_legs = [
            row
            for row in leg_nonoverlap_all
            if float(row.get("forecast_horizon_hours") or 0.0) == float(horizon)
        ]
        horizon_cohorts, horizon_overlaps = _forecast_time_cohorts(horizon_legs)
        horizon_cohorts = horizon_cohorts[-limit:]
        temporal_overlap_cohort_n += len(horizon_overlaps)
        for cohort in horizon_overlaps:
            temporal_overlap_keys.update(
                (int(row["origin_cycle"]), str(row["symbol"])) for row in cohort["rows"]
            )
        by_horizon[str(horizon)] = {
            "horizon_hours": horizon,
            **_forecast_metric_summary(horizon_cohorts),
            "overlapping_time_cohort_n_audit_only": len(horizon_overlaps),
            "overlapping_time_cohort_leg_n_audit_only": sum(
                len(cohort["rows"]) for cohort in horizon_overlaps
            ),
        }
    return {
        "audited_forecasts_in_window": len(audit_recent),
        "total_audited_forecasts": len(rows),
        "decision_eligible_forecasts_total": len(decision_eligible_all),
        "leg_nonoverlap_eligible_forecasts_total": len(leg_nonoverlap_all),
        "independent_time_cohort_n_in_context_window": len(recent_cohorts),
        "effective_n_in_context_window": len(recent_cohorts),
        "total_independent_time_cohorts": len(global_cohorts),
        "leg_nonoverlap_nonproduction_horizon_forecasts_context_only": sum(
            float(row.get("forecast_horizon_hours") or 0.0) not in _FORECAST_CALIBRATION_HORIZONS
            for row in leg_nonoverlap_all
        ),
        "temporally_overlapping_time_cohorts_audit_only": temporal_overlap_cohort_n,
        "temporally_overlapping_forecast_legs_audit_only": len(temporal_overlap_keys),
        "overlapping_decision_forecasts_audit_only": len(overlapping_decisions),
        "overlapping_changed_thesis_forecasts_audit_only": sum(
            row.get("forecast_independence_reason") == "explicit_thesis_changed"
            for row in overlapping_decisions
        ),
        "legacy_policy_forecasts_audit_only": sum(
            int(row.get("forecast_score_schema_version", 1)) < FORECAST_SCORE_SCHEMA_VERSION
            for row in rows
        ),
        "off_horizon_forecasts_audit_only": sum(
            int(row.get("forecast_score_schema_version", 1)) >= FORECAST_SCORE_SCHEMA_VERSION
            and row.get("horizon_label_eligible") is False
            for row in rows
        ),
        "overlapping_unchanged_renewals_audit_only": sum(
            row.get("forecast_independence_reason") == "overlapping_unchanged_thesis"
            for row in rows
        ),
        "production_calibration_horizons_hours": list(_FORECAST_CALIBRATION_HORIZONS),
        "sign_hit_semantics": (
            "directional forecast accuracy; selected-side profitability is reported separately"
        ),
        "calibration_sample_note": (
            "BookLeg calibration must use its exact by_horizon_hours bucket. Every bucket uses "
            "only schema-v4, horizon-eligible, same-symbol non-overlapping legs, clusters all legs "
            "sharing a time/outcome window, and gates usability on non-overlapping temporal "
            "cohorts rather than leg_n. Overlapping leg renewals and time cohorts are audit-only. "
            "aggregate_context_only pools incompatible horizons and is never calibration or "
            "risk-capacity evidence."
        ),
        "risk_capacity_evidence_basis": (
            "matching_horizon_nonoverlapping_time_outcome_cohorts_only"
        ),
        "aggregate_context_only": aggregate,
        "by_horizon_hours": by_horizon,
        "time_cohort_outcomes_context_only": [
            {**_forecast_cohort_audit(cohort), "outcomes": cohort["rows"]}
            for cohort in recent_cohorts
        ],
        "audit_only_outcomes": [
            row
            for row in audit_recent
            if int(row.get("forecast_score_schema_version", 1)) < FORECAST_SCORE_SCHEMA_VERSION
            or row.get("learning_eligible") is not True
            or row.get("leg_nonoverlap_eligible") is not True
            or (int(row["origin_cycle"]), str(row["symbol"])) in temporal_overlap_keys
        ],
    }


def _pending_forecast_inventory(state: Path, scored_rows: list[dict], *, now: datetime) -> dict:
    seen = {(int(row["origin_cycle"]), str(row["symbol"])) for row in scored_rows}
    pending: list[dict] = []
    root = state / "rebal" / "cycle"
    if root.exists():
        for directory in sorted(
            (path for path in root.iterdir() if path.is_dir() and path.name.isdigit()),
            key=lambda path: int(path.name),
        ):
            if not cycle_is_complete(
                state, int(directory.name), cadence="rebal", require_manifest=True
            ):
                continue
            if not all(
                completed_artifact_is_bound(state, int(directory.name), artifact, cadence="rebal")
                for artifact in ("book", "report")
            ):
                continue
            book_path = directory / "book.json"
            report_path = directory / "report.json"
            if not book_path.exists() or not report_path.exists():
                continue
            book = json.loads(book_path.read_text())
            report = json.loads(report_path.read_text())
            try:
                origin_ts = _as_utc(report["decision_ts"])
            except (KeyError, TypeError, ValueError):
                continue
            cycle = int(directory.name)
            for leg in book.get("legs", []):
                if not isinstance(leg, dict):
                    continue
                required = {"seat_role", "expected_price_edge_frac", "edge_horizon_hours"}
                symbol = str(leg.get("symbol") or "")
                if (
                    not required.issubset(leg)
                    or leg.get("seat_role") != "alpha"
                    or (cycle, symbol) in seen
                ):
                    continue
                maturity = origin_ts + timedelta(hours=float(leg["edge_horizon_hours"]))
                pending.append(
                    {
                        "origin_cycle": cycle,
                        "symbol": symbol,
                        "maturity_ts": maturity.isoformat(),
                        "hours_past_maturity": max(0.0, (now - maturity).total_seconds() / 3600.0),
                    }
                )
    return {
        "pending_forecasts": len(pending),
        "mature_waiting_for_mark": sum(row["hours_past_maturity"] > 0.0 for row in pending),
        "oldest_pending_hours_past_maturity": max(
            (row["hours_past_maturity"] for row in pending), default=0.0
        ),
        "pending": pending,
    }


def _committed_alpha_thesis_provenance(state: Path, account, *, cycle: int) -> dict[str, dict]:
    """Resolve held alpha theses only through the newest immutable committed Book.

    The account carries a convenience pointer to the Book that last refreshed each position's
    thesis. It is not itself decision evidence. This resolver requires that pointer to identify
    the newest prior completion, verifies that completion's manifest and exact Book hash, and then
    requires one exact symbol/side/role leg plus an exact match to the cached thesis fields. Any
    break returns a shaped unavailable record; it never searches an older cycle as a fallback.
    """
    alpha_positions = {
        symbol: position
        for symbol, position in account.positions.items()
        if position.seat_role == "alpha"
    }
    if not alpha_positions:
        return {}

    root = state / "rebal" / "cycle"
    candidates = (
        sorted(
            (
                int(path.name)
                for path in root.iterdir()
                if path.is_dir()
                and path.name.isdigit()
                and int(path.name) < cycle
                and ((path / "complete.json").exists() or (path / "report.json").exists())
            ),
            reverse=True,
        )
        if root.exists()
        else []
    )
    candidate_cycle = candidates[0] if candidates else None

    def unavailable(symbol: str, reason: str) -> dict:
        position = alpha_positions[symbol]
        return {
            "available": False,
            "source_policy": _COMMITTED_THESIS_SOURCE_POLICY,
            "candidate_cycle": candidate_cycle,
            "committed_cycle": None,
            "manifest_bound_book_sha256": None,
            "symbol": symbol,
            "side": position.direction,
            "seat_role": "alpha",
            "expected_price_edge_frac": None,
            "edge_horizon_hours": None,
            "edge_calibration_basis": None,
            "invalidation_condition": None,
            "unavailable_reason": reason,
        }

    def unavailable_all(reason: str) -> dict[str, dict]:
        return {symbol: unavailable(symbol, reason) for symbol in alpha_positions}

    if candidate_cycle is None:
        return unavailable_all("no_prior_completed_cycle_candidate")
    try:
        completion_valid = cycle_is_complete(
            state, candidate_cycle, cadence="rebal", require_manifest=True
        )
    except (OSError, TypeError, ValueError):
        completion_valid = False
    if not completion_valid:
        return unavailable_all("latest_prior_completion_manifest_invalid")
    try:
        book_sha256 = completed_artifact_sha256(state, candidate_cycle, "book", cadence="rebal")
    except (OSError, TypeError, ValueError):
        book_sha256 = None
    if book_sha256 is None:
        return unavailable_all("latest_prior_cycle_book_is_not_manifest_bound")

    book_path = root / str(candidate_cycle) / "book.json"
    try:
        book = json.loads(book_path.read_text())
    except (OSError, json.JSONDecodeError):
        return unavailable_all("latest_prior_bound_book_is_unreadable")
    if not isinstance(book, dict) or canonical_sha256(book) != book_sha256:
        return unavailable_all("latest_prior_bound_book_changed_during_read")
    raw_legs = book.get("legs")
    if not isinstance(raw_legs, list):
        return unavailable_all("latest_prior_bound_book_has_no_leg_list")

    by_symbol: dict[str, list[dict]] = {}
    for raw_leg in raw_legs:
        if isinstance(raw_leg, dict) and isinstance(raw_leg.get("symbol"), str):
            by_symbol.setdefault(str(raw_leg["symbol"]), []).append(raw_leg)

    resolved: dict[str, dict] = {}
    for symbol, position in alpha_positions.items():
        if position.thesis_cycle != candidate_cycle:
            resolved[symbol] = unavailable(
                symbol, "position_thesis_cycle_does_not_match_latest_completion"
            )
            continue
        if position.thesis_book_sha256 != book_sha256:
            resolved[symbol] = unavailable(
                symbol, "position_thesis_book_hash_does_not_match_manifest"
            )
            continue
        symbol_legs = by_symbol.get(symbol, [])
        if len(symbol_legs) != 1:
            resolved[symbol] = unavailable(
                symbol, "latest_prior_bound_book_does_not_uniquely_cover_position"
            )
            continue
        leg = symbol_legs[0]
        if leg.get("side") != position.direction or leg.get("seat_role") != "alpha":
            resolved[symbol] = unavailable(symbol, "latest_prior_bound_book_side_or_role_mismatch")
            continue
        try:
            raw_edge = leg["expected_price_edge_frac"]
            raw_horizon = leg["edge_horizon_hours"]
            calibration = leg["edge_calibration_basis"]
            invalidation = leg["invalidation_condition"]
        except KeyError:
            resolved[symbol] = unavailable(
                symbol, "latest_prior_bound_book_has_malformed_alpha_thesis"
            )
            continue
        if (
            isinstance(raw_edge, bool)
            or not isinstance(raw_edge, (int, float))
            or isinstance(raw_horizon, bool)
            or not isinstance(raw_horizon, (int, float))
            or (
                isinstance(raw_horizon, float)
                and (not math.isfinite(raw_horizon) or not raw_horizon.is_integer())
            )
            or not isinstance(calibration, str)
            or not isinstance(invalidation, str)
        ):
            resolved[symbol] = unavailable(
                symbol, "latest_prior_bound_book_has_malformed_alpha_thesis"
            )
            continue
        edge = float(raw_edge)
        horizon = int(raw_horizon)
        if (
            not math.isfinite(edge)
            or not -1.0 <= edge <= 1.0
            or horizon not in _FORECAST_CALIBRATION_HORIZONS
            or not calibration.strip()
            or not invalidation.strip()
        ):
            resolved[symbol] = unavailable(
                symbol, "latest_prior_bound_book_has_malformed_alpha_thesis"
            )
            continue
        cached_edge = position.expected_price_edge_frac
        if (
            cached_edge is None
            or not math.isclose(float(cached_edge), edge, rel_tol=0.0, abs_tol=1e-12)
            or position.edge_horizon_hours != horizon
            or position.edge_calibration_basis != calibration
            or position.invalidation_condition != invalidation
        ):
            resolved[symbol] = unavailable(
                symbol, "position_thesis_fields_do_not_match_manifest_bound_leg"
            )
            continue
        resolved[symbol] = {
            "available": True,
            "source_policy": _COMMITTED_THESIS_SOURCE_POLICY,
            "candidate_cycle": candidate_cycle,
            "committed_cycle": candidate_cycle,
            "manifest_bound_book_sha256": book_sha256,
            "symbol": symbol,
            "side": position.direction,
            "seat_role": "alpha",
            "expected_price_edge_frac": edge,
            "edge_horizon_hours": horizon,
            "edge_calibration_basis": calibration,
            "invalidation_condition": invalidation,
            "unavailable_reason": None,
        }
    return resolved


def _agent_performance(score_rows: list[dict], *, window: int = _CALIBRATION_WINDOW_CYCLES) -> dict:
    if window < 1:
        raise ValueError("agent performance window must be positive")
    parsed_records: list[ScoreRecord] = []
    for raw in score_rows:
        try:
            parsed_records.append(ScoreRecord.model_validate(raw, strict=True))
        except (TypeError, ValueError):
            continue
    unverified_records = [
        record for record in parsed_records if record.outcome_provenance != "manifest_bound"
    ]
    verified_records = [
        record for record in parsed_records if record.outcome_provenance == "manifest_bound"
    ]
    off_horizon_records = [
        record for record in verified_records if not daily_score_is_learning_eligible(record)
    ]
    eligible_records = [
        record for record in verified_records if daily_score_is_learning_eligible(record)
    ]
    records = eligible_records[-window:]
    roles: dict[str, dict] = {}
    for role in _ROLES:
        role_records = [
            record for record in records if record.specialist_return_label == "btc_beta_adjusted"
        ]
        available = calls = hits = 0.0
        edge_numerator = 0.0
        active_cycles = 0
        complete_output_cycles = 0
        for record in role_records:
            score = record.specialists.get(role)
            complete_output = bool(
                score is not None and record.n_symbols > 0 and score.n_available == record.n_symbols
            )
            if not complete_output:
                continue
            complete_output_cycles += 1
            available += score.n_available
            calls += score.n_scored
            hits += score.hit_rate * score.n_scored
            edge_numerator += score.conv_weighted_edge * score.n_scored
            active_cycles += int(score.n_scored > 0)
        output_coverage_rate = complete_output_cycles / len(role_records) if role_records else 0.0
        sample_deficits = []
        if complete_output_cycles < _MIN_CALIBRATION_CYCLES:
            sample_deficits.append(
                f"complete_output_cycles:{complete_output_cycles}/{_MIN_CALIBRATION_CYCLES}"
            )
        if output_coverage_rate < _MIN_SPECIALIST_OUTPUT_COVERAGE:
            sample_deficits.append(
                "output_coverage_rate:"
                f"{output_coverage_rate:.3f}/{_MIN_SPECIALIST_OUTPUT_COVERAGE:.3f}"
            )
        if calls < _MIN_SPECIALIST_DIRECTIONAL_CALLS:
            sample_deficits.append(
                f"directional_calls:{int(calls)}/{_MIN_SPECIALIST_DIRECTIONAL_CALLS}"
            )
        roles[role] = {
            "return_label": "btc_beta_adjusted",
            "window_cycles": len(role_records),
            "legacy_raw_cycles_ignored": len(records) - len(role_records),
            "complete_output_cycles": complete_output_cycles,
            "failed_or_incomplete_output_cycles": (len(role_records) - complete_output_cycles),
            "output_coverage_rate": output_coverage_rate,
            "active_cycles": active_cycles,
            "available_symbol_outcomes": int(available),
            "directional_calls": int(calls),
            "abstention_rate": (1.0 - calls / available) if available > 0.0 else 0.0,
            "hit_rate": (hits / calls) if calls > 0.0 else 0.0,
            "conviction_weighted_edge": (edge_numerator / calls if calls > 0.0 else 0.0),
            "calibration_sample_status": (
                "usable" if not sample_deficits else "insufficient_horizon_matched_history"
            ),
            "calibration_sample_deficits": sample_deficits,
            "calibration_sample_basis": ("complete_output_horizon_matched_serial_daily_cycles"),
            "minimum_complete_output_cycles": _MIN_CALIBRATION_CYCLES,
            "minimum_output_coverage_rate": _MIN_SPECIALIST_OUTPUT_COVERAGE,
            "minimum_directional_calls": _MIN_SPECIALIST_DIRECTIONAL_CALLS,
        }

    realized_strategy = [
        value
        for record in records
        if record.book.n_legs > 0
        if (value := realized_edge_frac(record.book)) is not None
    ]
    pm_decisions = [
        value for record in records if (value := pm_decision_edge_frac(record.book)) is not None
    ]
    exit_decisions = [
        record.book.incremental_edge_vs_no_change_frac
        for record in records
        if record.book.decision_kind == "exit_to_cash"
        and record.book.incremental_edge_vs_no_change_frac is not None
    ]
    actual_net = [
        record.book.actual_strategy_net_frac_on_whole_book_gross
        for record in records
        if record.book.actual_strategy_net_frac_on_whole_book_gross is not None
    ]
    projected_strategy = [
        record.book.strategy_net_frac
        for record in records
        if record.book.n_legs > 0
        and record.book.strategy_net_is_forecast
        and record.book.strategy_net_frac is not None
    ]
    pm_sample_deficits = []
    if len(realized_strategy) < _MIN_CALIBRATION_CYCLES:
        pm_sample_deficits.append(
            f"horizon_matched_books:{len(realized_strategy)}/{_MIN_CALIBRATION_CYCLES}"
        )
    roles["pm"] = {
        "window_cycles": len(records),
        "scored_books": len(realized_strategy),
        "profitable_books": sum(value >= 0.0 for value in realized_strategy),
        "average_realized_edge_ex_funding_frac": (
            sum(realized_strategy) / len(realized_strategy) if realized_strategy else 0.0
        ),
        "cumulative_realized_edge_ex_funding_frac": sum(realized_strategy),
        "scored_pm_decisions_including_exits": len(pm_decisions),
        "profitable_pm_decisions_including_exits": sum(value >= 0.0 for value in pm_decisions),
        "average_pm_decision_edge_vs_cash_or_no_change_frac": (
            sum(pm_decisions) / len(pm_decisions) if pm_decisions else None
        ),
        "cash_exit_decisions_scored": len(exit_decisions),
        "average_cash_exit_edge_vs_no_change_frac": (
            sum(exit_decisions) / len(exit_decisions) if exit_decisions else None
        ),
        "cash_hold_cycles_unscored_as_zero": sum(
            record.book.decision_kind == "cash_hold" and record.book.n_legs == 0
            for record in records
        ),
        "actual_funding_attributed_books": len(actual_net),
        "average_actual_price_funding_friction_net_frac_on_alpha_gross": (
            sum(actual_net) / len(actual_net) if actual_net else None
        ),
        "calibration_sample_status": (
            "usable" if not pm_sample_deficits else "insufficient_horizon_matched_history"
        ),
        "calibration_sample_deficits": pm_sample_deficits,
        "calibration_sample_basis": "horizon_matched_serial_daily_books",
        "minimum_horizon_matched_books": _MIN_CALIBRATION_CYCLES,
        "average_forecast_inclusive_edge_frac": (
            sum(projected_strategy) / len(projected_strategy) if projected_strategy else None
        ),
        "funding_label_note": (
            "Forecast funding remains separate. Exact next-interval report plus intervening "
            "heartbeat funding is attributed when provenance permits; cumulative account funding "
            "remains in desk.funding_net."
        ),
    }
    roles["adversary"] = {
        "window_cycles": len(records),
        "accepted_originals": sum(r.adv_accepted and not r.adv_revised for r in records),
        "revisions_demanded": sum(r.adv_revised for r in records),
        "accepted_losing_originals": sum(
            r.adv_accepted
            and not r.adv_revised
            and realized_edge_frac(r.book) is not None
            and realized_edge_frac(r.book) < 0.0
            for r in records
        ),
    }
    return {
        "window_cycles": len(records),
        "calibration_window_limit_cycles": window,
        "eligible_horizon_cycles_available": len(eligible_records),
        "sample_dependence_note": (
            "Horizon-matched daily rows are serial observations; this packet does not claim "
            "statistical independence for whole-book or specialist samples."
        ),
        "outcome_provenance": ("manifest_bound_scoring_marks_at_scheduled_24h_horizon_only"),
        "daily_learning_horizon_hours": DAILY_LEARNING_HORIZON_HOURS,
        "scheduled_mark_tolerance_minutes": (SCHEDULED_MARK_TOLERANCE.total_seconds() / 60.0),
        "manifest_bound_off_horizon_cycles_ignored": len(off_horizon_records),
        "off_horizon_audit": [
            {
                "cycle": record.cycle,
                "evaluation_horizon_hours": record.evaluation_horizon_hours,
                "outcome_observation_cycle": record.outcome_observation_cycle,
            }
            for record in off_horizon_records[-window:]
        ],
        "unverified_legacy_cycles_ignored": len(unverified_records),
        "roles": roles,
    }


def _candidate_opportunity_performance(rows: list[dict]) -> dict:
    """Summarize PM-declared shadows without treating them as independent alpha evidence."""
    on_horizon = [row for row in rows if row.get("horizon_label_eligible") is True]
    omitted = [row for row in on_horizon if row.get("status") != "selected"]
    gate_declared = [row for row in omitted if row.get("gate_causal_claim") is True]

    def summary(sample: list[dict]) -> dict:
        values = [float(row["realized_selected_edge_frac"]) for row in sample]
        return {
            "n": len(sample),
            "profitable_rate": (
                sum(value > 0.0 for value in values) / len(values) if values else None
            ),
            "mean_realized_selected_edge_frac": (sum(values) / len(values) if values else None),
            "cumulative_counterfactual_price_pnl": sum(
                float(row["counterfactual_price_pnl"]) for row in sample
            ),
        }

    return {
        "schema_version": 1,
        "total_bound_candidate_outcomes": len(rows),
        "on_declared_horizon": len(on_horizon),
        "off_horizon_audit_only": len(rows) - len(on_horizon),
        "omitted_candidates": summary(omitted),
        "pm_declared_entry_gate_exclusions": summary(gate_declared),
        "by_exclusion_reason": {
            reason: summary([row for row in omitted if str(row.get("exclusion_reason")) == reason])
            for reason in sorted({str(row.get("exclusion_reason")) for row in omitted})
        },
        "causality_note": (
            "entry_gate is the PM's immutable pre-outcome declaration; code binds and scores it "
            "but never infers causality or turns the result into a trade."
        ),
        "selection_bias_warning": (
            "Candidate outcomes are overlapping, adaptively selected shadow evidence and are "
            "not an independent alpha estimate."
        ),
    }


def _benchmark_leg_return(legs: list[tuple[str, str]], residual_returns: dict[str, float]) -> dict:
    """Equal-weight a two-sided shadow with 50% gross on each side."""
    unique = list(dict.fromkeys(legs))
    longs = [(symbol, side) for symbol, side in unique if side == "long"]
    shorts = [(symbol, side) for symbol, side in unique if side == "short"]
    if not longs or not shorts or any(symbol not in residual_returns for symbol, _side in unique):
        return {"available": False, "gross_price_return_frac": None, "n_legs": len(unique)}
    value = sum(residual_returns[symbol] for symbol, _side in longs) * 0.5 / len(longs)
    value -= sum(residual_returns[symbol] for symbol, _side in shorts) * 0.5 / len(shorts)
    return {"available": True, "gross_price_return_frac": value, "n_legs": len(unique)}


def _cost_net_benchmark_outcome(
    legs: list[tuple[str, str]],
    residual_returns: dict[str, float],
    evidence_by_symbol: dict[str, dict],
    previous_signed_quantities: dict[str, float],
    *,
    shadow_gross_usd: float,
    execution_realism: ExecutionRealism,
) -> dict:
    """Rebalance one frozen shadow portfolio and charge conservative one-way costs.

    This is measurement only. Missing full-clip directional liquidity makes the observation
    unavailable; code never substitutes a cheaper fill or changes a real desk position.
    """
    unique = list(dict.fromkeys(legs))
    longs = [symbol for symbol, side in unique if side == "long"]
    shorts = [symbol for symbol, side in unique if side == "short"]
    if (
        shadow_gross_usd <= 0.0
        or not longs
        or not shorts
        or set(longs) & set(shorts)
    ):
        return {
            "available": False,
            "unavailable_reason": "two_sided_origin_target_unavailable",
            "n_legs": len(unique),
            "origin_transaction_available": False,
            "outcome_complete": False,
            "transition_executed": False,
        }
    target = {
        **{symbol: 0.5 * shadow_gross_usd / len(longs) for symbol in longs},
        **{symbol: -0.5 * shadow_gross_usd / len(shorts) for symbol in shorts},
    }
    origin_marks: dict[str, float] = {}
    for symbol in sorted(set(target) | set(previous_signed_quantities)):
        mark = float((evidence_by_symbol.get(symbol) or {}).get("mark") or 0.0)
        if not math.isfinite(mark) or mark <= 0.0:
            return {
                "available": False,
                "unavailable_reason": f"missing_positive_origin_mark:{symbol}",
                "n_legs": len(unique),
                "origin_transaction_available": False,
                "outcome_complete": False,
                "transition_executed": False,
            }
        origin_marks[symbol] = mark
    existing_notional = {
        symbol: previous_signed_quantities.get(symbol, 0.0) * origin_marks[symbol]
        for symbol in origin_marks
    }
    deltas = {
        symbol: target.get(symbol, 0.0) - existing_notional.get(symbol, 0.0)
        for symbol in origin_marks
    }
    deltas = {symbol: value for symbol, value in deltas.items() if abs(value) > 0.01}
    legging_reserve_bps = (
        execution_realism.legging_bps_per_second
        * (execution_realism.latency_ms / 1000.0)
        * max(len(deltas) - 1, 0)
    )
    friction = 0.0
    cost_audit = []
    for symbol, delta in deltas.items():
        evidence = evidence_by_symbol.get(symbol)
        if evidence is None:
            return {
                "available": False,
                "unavailable_reason": f"missing_origin_evidence:{symbol}",
                "n_legs": len(unique),
                "origin_transaction_available": False,
                "outcome_complete": False,
                "transition_executed": False,
            }
        cost, executable_clip, lookup_clip, fill_fraction = _one_way_friction_usd(
            evidence,
            abs(delta),
            "buy" if delta > 0.0 else "sell",
            execution_realism=execution_realism,
            legging_reserve_bps=legging_reserve_bps,
        )
        if not math.isfinite(cost) or (
            fill_fraction is not None and fill_fraction < 1.0 - 1e-12
        ):
            return {
                "available": False,
                "unavailable_reason": f"full_clip_conservative_cost_unavailable:{symbol}",
                "n_legs": len(unique),
                "origin_transaction_available": False,
                "outcome_complete": False,
                "transition_executed": False,
            }
        friction += cost
        cost_audit.append(
            {
                "symbol": symbol,
                "side": "buy" if delta > 0.0 else "sell",
                "decision_turnover_usd": abs(delta),
                "executable_turnover_usd": executable_clip,
                "conservative_curve_lookup_usd": lookup_clip,
                "friction_usd": cost,
            }
        )
    turnover_usd = sum(abs(value) for value in deltas.values())
    target_signed_quantities = {
        symbol: signed_notional / origin_marks[symbol]
        for symbol, signed_notional in target.items()
    }
    finite_returns: dict[str, float] = {}
    for symbol in target:
        try:
            value = float(residual_returns[symbol])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            finite_returns[symbol] = value
    missing_outcome_symbols = sorted(set(target) - set(finite_returns))
    marked_gross_price_pnl_usd = sum(
        target[symbol] * value for symbol, value in finite_returns.items()
    )
    marked_gross_usd = sum(abs(target[symbol]) for symbol in finite_returns)
    common = {
        "n_legs": len(unique),
        "shadow_gross_usd": shadow_gross_usd,
        "transaction_cost_frac": friction / shadow_gross_usd,
        "turnover_usd": turnover_usd,
        "turnover_frac_gross": turnover_usd / shadow_gross_usd,
        "friction_usd": friction,
        "target_portfolio_sha256": canonical_sha256(target),
        "ending_signed_quantities": target_signed_quantities,
        "ending_signed_quantities_sha256": canonical_sha256(target_signed_quantities),
        "cost_audit_sha256": canonical_sha256(cost_audit),
        "origin_transaction_available": True,
        "transition_executed": True,
        "outcome_complete": not missing_outcome_symbols,
        "outcome_missing_symbols": missing_outcome_symbols,
        "marked_gross_usd": marked_gross_usd,
        "marked_gross_coverage_frac": marked_gross_usd / shadow_gross_usd,
        "marked_gross_price_pnl_usd": marked_gross_price_pnl_usd,
        "marked_cost_net_price_pnl_usd": marked_gross_price_pnl_usd - friction,
        "_target_signed_quantities": target_signed_quantities,
    }
    if missing_outcome_symbols:
        return {
            **common,
            "available": False,
            "unavailable_reason": (
                "complete_outcome_unavailable:" + ",".join(missing_outcome_symbols)
            ),
            "gross_price_return_frac": None,
            "cost_net_price_return_frac": None,
        }
    gross_return = marked_gross_price_pnl_usd / shadow_gross_usd
    return {
        **common,
        "available": True,
        "unavailable_reason": None,
        "gross_price_return_frac": gross_return,
        "cost_net_price_return_frac": gross_return - friction / shadow_gross_usd,
    }


def _benchmark_comparisons(history: list[dict]) -> dict[str, dict]:
    """Compare only continuous post-inception shadow paths.

    Paired values remain useful diagnostics across an incomplete path, but a skipped scheduled
    observation or unavailable post-inception outcome makes an annualized information ratio
    non-usable. Pre-inception rows do not create a gap for a benchmark that could not yet form.
    """
    comparisons: dict[str, dict] = {}
    for name in (
        "cash",
        "residual_momentum_ls",
        "selected_equal_weight_ls",
        "carry_ls",
        "no_change",
    ):
        desk_rows = [
            row for row in history if row.get("desk_cost_net_price_edge_frac") is not None
        ]
        if name == "cash":
            inception_index = 0 if desk_rows else None
        else:
            inception_index = next(
                (
                    index
                    for index, row in enumerate(desk_rows)
                    if row.get("benchmarks", {}).get(name, {}).get("transition_executed") is True
                ),
                None,
            )
        post_inception = desk_rows[inception_index:] if inception_index is not None else []
        availability_gap_cycles = [
            int(row["cycle"])
            for row in post_inception
            if row.get("benchmarks", {}).get(name, {}).get("available") is not True
        ]
        sequence_gap_cycles = [
            int(row["cycle"])
            for index, row in enumerate(post_inception)
            if index > 0 and row.get("scheduled_sequence_gap_before") is True
        ]
        continuity_gap_cycles = sorted(set(availability_gap_cycles + sequence_gap_cycles))
        paired = [
            (
                float(row["desk_cost_net_price_edge_frac"]),
                float(
                    row["benchmarks"][name].get(
                        "cost_net_price_return_frac",
                        row["benchmarks"][name].get("gross_price_return_frac"),
                    )
                ),
            )
            for row in post_inception
            if row["benchmarks"].get(name, {}).get("available") is True
        ]
        active = [desk - benchmark for desk, benchmark in paired]
        tracking_error = stdev(active) * math.sqrt(365.0) if len(active) >= 2 else None
        enough = len(paired) >= _MIN_INFORMATION_RATIO_OBSERVATIONS
        continuous = not continuity_gap_cycles
        status = (
            "gapped_history"
            if not continuous
            else "usable"
            if enough
            else "insufficient_horizon_matched_history"
        )
        if status == "gapped_history":
            warning = (
                "post-inception shadow path has unavailable or skipped scheduled observations; "
                "paired statistics are diagnostic only"
            )
        elif status != "usable":
            warning = "insufficient paired scheduled-horizon observations for inference"
        else:
            warning = None
        comparisons[name] = {
            "paired_observations": len(paired),
            "minimum_observations": _MIN_INFORMATION_RATIO_OBSERVATIONS,
            "status": status,
            "shadow_inception_cycle": (
                int(post_inception[0]["cycle"]) if post_inception else None
            ),
            "pre_inception_observations": (
                inception_index if inception_index is not None else len(desk_rows)
            ),
            "continuity_gap_count": len(continuity_gap_cycles),
            "continuity_gap_cycles": continuity_gap_cycles,
            "mean_active_return_frac": sum(active) / len(active) if active else None,
            "mean_excess_return_frac": sum(active) / len(active) if active else None,
            "tracking_error_annualized": tracking_error,
            "information_ratio_annualized": (
                (sum(active) / len(active)) * 365.0 / tracking_error
                if status == "usable" and tracking_error and tracking_error > 0.0
                else None
            ),
            "warning": warning,
        }
    return comparisons


def _benchmark_performance(
    state: Path,
    records: list[ScoreRecord],
    *,
    shadow_gross_usd: float,
) -> dict:
    """Point-in-time, code-versioned shadow benchmarks over committed 24h outcomes."""
    policy = {
        "version": _BENCHMARK_POLICY_VERSION,
        "cash": "zero return",
        "btc_context": "raw BTC return; not risk-equivalent",
        "residual_momentum_ls": (
            "50/50 equal-weight long top and short bottom 72h beta-adjusted momentum; "
            "one-to-three names per side"
        ),
        "selected_equal_weight_ls": "50/50 equal-weight PM-selected alpha sides",
        "carry_ls": (
            "50/50 equal-weight long lowest and short highest conservative funding; "
            "one-to-three names per side"
        ),
        "no_change": "previous completed alpha Book sides, equal-weight; target-size approximation",
        "return_basis": "beta-adjusted price return after hypothetical benchmark turnover cost",
        "shadow_gross_usd": shadow_gross_usd,
        "execution_realism": {
            "latency_ms": 500.0,
            "displayed_depth_fraction": 0.5,
            "adverse_selection_bps": 1.0,
            "legging_bps_per_second": 0.25,
            "allow_partial_fills": False,
            "taker_fee_bps": 5.0,
        },
        "funding_basis": (
            "unavailable for shadow portfolios; no per-symbol manifest-bound settlement path is "
            "guessed"
        ),
        "turnover_state": (
            "prior signed quantities are revalued at each next manifest-bound origin mark before "
            "target deltas are costed"
        ),
        "origin_outcome_separation": (
            "origin-time targets and conservative transitions advance without consulting future "
            "outcome completeness; missing outcomes never rewind shadow holdings"
        ),
        "continuity_policy": (
            "any unavailable or skipped scheduled observation after shadow inception makes the "
            "information ratio non-usable"
        ),
    }
    policy_sha256 = canonical_sha256(policy)
    execution_realism = ExecutionRealism(
        latency_ms=500.0,
        displayed_depth_fraction=0.5,
        adverse_selection_bps=1.0,
        legging_bps_per_second=0.25,
        allow_partial_fills=False,
    )
    completed = (
        sorted(
            int(path.name)
            for path in (state / "rebal" / "cycle").iterdir()
            if path.is_dir() and path.name.isdigit()
            and cycle_is_complete(
                state, int(path.name), cadence="rebal", require_manifest=True
            )
            and completed_artifact_is_bound(
                state, int(path.name), "book", cadence="rebal"
            )
        )
        if (state / "rebal" / "cycle").exists()
        else []
    )
    history = []
    previous_signed_quantities: dict[str, dict[str, float]] = {
        name: {}
        for name in ("residual_momentum_ls", "selected_equal_weight_ls", "carry_ls", "no_change")
    }
    previous_observation_cycle: int | None = None
    for record in records:
        if not daily_score_is_learning_eligible(record):
            continue
        observation_cycle = record.outcome_observation_cycle
        if observation_cycle is None:
            continue
        origin = cycle_dir(state, record.cycle)
        try:
            evidence = json.loads((origin / "evidence.json").read_text())
            book = json.loads((origin / "book.json").read_text())
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        scheduled_sequence_gap_before = (
            previous_observation_cycle is not None
            and record.cycle != previous_observation_cycle
        )
        try:
            outcome = json.loads(
                (cycle_dir(state, observation_cycle) / "scoring_marks.json").read_text()
            )
            marks = {str(symbol): float(mark) for symbol, mark in outcome["marks"].items()}
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            marks = {}
        by_symbol = {str(row["symbol"]): row for row in evidence}
        btc_symbol = record.btc_symbol
        btc_origin = float(by_symbol.get(btc_symbol, {}).get("mark") or 0.0)
        btc_outcome = float(marks.get(btc_symbol) or 0.0)
        btc_return = (
            btc_outcome / btc_origin - 1.0
            if math.isfinite(btc_origin)
            and math.isfinite(btc_outcome)
            and btc_origin > 0.0
            and btc_outcome > 0.0
            else None
        )
        residual_returns: dict[str, float] = {}
        if btc_return is not None:
            for symbol, row in by_symbol.items():
                if symbol == btc_symbol or symbol not in marks:
                    continue
                try:
                    origin_mark = float(row.get("mark") or 0.0)
                    outcome_mark = float(marks[symbol])
                    beta = float(row.get("beta_clamped", row.get("beta_btc", 1.0)))
                except (TypeError, ValueError):
                    continue
                if (
                    math.isfinite(origin_mark)
                    and math.isfinite(outcome_mark)
                    and math.isfinite(beta)
                    and origin_mark > 0.0
                    and outcome_mark > 0.0
                ):
                    residual_returns[symbol] = (
                        outcome_mark / origin_mark - 1.0 - beta * btc_return
                    )
        universe = [
            row
            for row in evidence
            if str(row["symbol"]) != btc_symbol
        ]

        def ranked_legs(
            rows: list[dict], field: str, low_side: str, high_side: str
        ) -> list[tuple[str, str]]:
            ranked = []
            for row in rows:
                try:
                    value = float(row[field])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    ranked.append(row)
            ranked.sort(key=lambda row: (float(row[field]), str(row["symbol"])))
            count = max(1, min(3, len(ranked) // 4)) if len(ranked) >= 2 else 0
            return [(str(row["symbol"]), low_side) for row in ranked[:count]] + [
                (str(row["symbol"]), high_side) for row in ranked[-count:]
            ]

        selected = [
            (str(leg["symbol"]), str(leg["side"]))
            for leg in book.get("legs", [])
            if leg.get("seat_role", "alpha") == "alpha" and leg.get("symbol") != btc_symbol
        ]
        prior = [cycle for cycle in completed if cycle < record.cycle]
        no_change: list[tuple[str, str]] = []
        if prior:
            try:
                prior_book = json.loads((cycle_dir(state, prior[-1]) / "book.json").read_text())
                no_change = [
                    (str(leg["symbol"]), str(leg["side"]))
                    for leg in prior_book.get("legs", [])
                    if leg.get("seat_role", "alpha") == "alpha" and leg.get("symbol") != btc_symbol
                ]
            except (OSError, TypeError, json.JSONDecodeError):
                pass
        benchmark_legs = {
            "residual_momentum_ls": ranked_legs(
                universe, "beta_adjusted_momentum_72h_pct", "short", "long"
            ),
            "selected_equal_weight_ls": selected,
            "carry_ls": ranked_legs(
                universe, "conservative_funding_8h_bps", "long", "short"
            ),
            "no_change": no_change,
        }
        benchmarks = {
            "cash": {"available": True, "gross_price_return_frac": 0.0},
            "btc_context": {
                "available": btc_return is not None,
                "gross_price_return_frac": btc_return,
                "risk_equivalent": False,
            },
        }
        for name, legs in benchmark_legs.items():
            result = _cost_net_benchmark_outcome(
                legs,
                residual_returns,
                by_symbol,
                previous_signed_quantities[name],
                shadow_gross_usd=shadow_gross_usd,
                execution_realism=execution_realism,
            )
            quantities = result.pop("_target_signed_quantities", None)
            if result.get("transition_executed") is True and isinstance(quantities, dict):
                previous_signed_quantities[name] = quantities
            benchmarks[name] = result
        history.append(
            {
                "cycle": record.cycle,
                "outcome_observation_cycle": observation_cycle,
                "scheduled_sequence_gap_before": scheduled_sequence_gap_before,
                "desk_cost_net_price_edge_frac": pm_decision_edge_frac(record.book),
                "benchmarks": benchmarks,
                "origin_evidence_sha256": completed_artifact_sha256(
                    state, record.cycle, "evidence"
                ),
                "outcome_scoring_marks_sha256": record.outcome_scoring_marks_sha256,
            }
        )
        previous_observation_cycle = observation_cycle

    comparisons = _benchmark_comparisons(history)
    return {
        "schema_version": 3,
        "policy": policy,
        "policy_sha256": policy_sha256,
        "primary_market_neutral_benchmark": "residual_momentum_ls",
        "shadow_ledger": history[-30:],
        "shadow_ledger_sha256": canonical_sha256(history[-30:]),
        "shadow_ledger_rows_truncated": max(0, len(history) - 30),
        # Compatibility alias for consumers introduced with schema 1.
        "history": history[-30:],
        "comparisons": comparisons,
        "warning": (
            "Information ratios require the predeclared minimum sample. Shadow returns are net "
            "of frozen conservative transaction-cost assumptions but exclude hypothetical "
            "funding because no per-symbol manifest-bound shadow settlement path exists."
        ),
    }


def build_performance_snapshot(
    state_dir: str | Path,
    memory_dir: str | Path,
    pending_dir: str | Path,
    *,
    cycle: int,
    as_of_ts: str | datetime,
    starting_capital: float,
    require_cycle_meta: bool = False,
) -> dict:
    """Build the performance packet from already-recorded local PAPER state.

    The current cycle's evidence supplies the only marks used.  Missing marks for a held position
    are a hard error: silently omitting a losing leg would give the agents a false performance
    picture.
    """
    state = Path(state_dir)
    memory = Path(memory_dir)
    pending = Path(pending_dir)
    evidence = json.loads((pending / "evidence.json").read_text())
    risk_model = json.loads((pending / "risk_model.json").read_text())
    meta_path = pending / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    if require_cycle_meta and not isinstance(meta, dict):
        raise ValueError("performance snapshot requires cycle meta.json")
    if meta is not None:
        if meta.get("evidence_sha256") != canonical_sha256(evidence) or meta.get(
            "risk_model_sha256"
        ) != canonical_sha256(risk_model):
            raise ValueError("cycle meta evidence/risk hash mismatch")
        validate_candle_audit(meta, evidence)
    by_symbol = {str(row["symbol"]): row for row in evidence}
    if len(by_symbol) != len(evidence):
        raise ValueError("performance snapshot evidence contains duplicate symbols")
    marks = {symbol: float(row.get("mark") or 0.0) for symbol, row in by_symbol.items()}

    account = load_account(state, default_cash=starting_capital)
    missing = sorted(set(account.positions) - {s for s, mark in marks.items() if mark > 0.0})
    if missing:
        raise ValueError(f"performance snapshot lacks positive marks for held symbols: {missing}")

    unrealized_by_symbol = account.mark_to_market(marks)
    unrealized = sum(unrealized_by_symbol.values())
    equity = account.equity(marks)
    funding_net = account.funding_received - account.funding_paid

    ledger_rows, ledger_duplicates = _dedupe_cycles(_jsonl(state / "ledger.jsonl"))
    heartbeat_rows = _jsonl(state / "portfolio-heartbeats.jsonl")
    raw_score_records = _score_jsonl(memory / "scorecard.jsonl")
    score_by_cycle: dict[int, ScoreRecord] = {}
    for record in raw_score_records:
        if record.outcome_provenance == "manifest_bound" and not score_record_is_manifest_bound(
            state, record
        ):
            raise ValueError(f"score cycle {record.cycle} is not bound to committed artifacts")
        prior = score_by_cycle.get(record.cycle)
        if prior is not None and prior != record:
            raise ValueError(f"conflicting duplicate scorecard cycle {record.cycle}")
        score_by_cycle.setdefault(record.cycle, record)
    parsed_score_records = [score_by_cycle[cycle] for cycle in sorted(score_by_cycle)]
    score_duplicates = len(raw_score_records) - len(parsed_score_records)
    forecast_rows = read_forecast_scorecard(memory / "forecast-scorecard.jsonl", state_dir=state)
    candidate_rows = read_candidate_scorecard(memory / "candidate-scorecard.jsonl", state_dir=state)
    historical_equity = (
        [starting_capital]
        + [
            float(row["closing_equity"])
            for row in ledger_rows
            if float(row.get("closing_equity") or 0.0) > 0.0
        ]
        + [float(row["equity"]) for row in heartbeat_rows if float(row.get("equity") or 0.0) > 0.0]
    )
    peak_equity = max([*historical_equity, equity])

    longs = sum(
        position.qty * marks[symbol]
        for symbol, position in account.positions.items()
        if position.direction == "long"
    )
    shorts = sum(
        position.qty * marks[symbol]
        for symbol, position in account.positions.items()
        if position.direction == "short"
    )
    gross = longs + shorts
    typed_hedge_symbols = {
        symbol
        for symbol, position in account.positions.items()
        if symbol == "BTC/USDT:USDT" and position.seat_role == "hedge"
    }
    alpha_longs = sum(
        position.qty * marks[symbol]
        for symbol, position in account.positions.items()
        if symbol not in typed_hedge_symbols and position.direction == "long"
    )
    alpha_shorts = sum(
        position.qty * marks[symbol]
        for symbol, position in account.positions.items()
        if symbol not in typed_hedge_symbols and position.direction == "short"
    )
    alpha_gross = alpha_longs + alpha_shorts
    hedge_gross = gross - alpha_gross
    beta_net = sum(
        (1.0 if position.direction == "long" else -1.0)
        * position.qty
        * marks[symbol]
        * float(by_symbol[symbol].get("beta_clamped", by_symbol[symbol].get("beta_btc", 1.0)))
        for symbol, position in account.positions.items()
    )
    alpha_beta_net = sum(
        (1.0 if position.direction == "long" else -1.0)
        * position.qty
        * marks[symbol]
        * float(by_symbol[symbol].get("beta_clamped", by_symbol[symbol].get("beta_btc", 1.0)))
        for symbol, position in account.positions.items()
        if symbol not in typed_hedge_symbols
    )
    hedge_beta_net = beta_net - alpha_beta_net
    hedge_beta_reduction_frac = (
        (abs(alpha_beta_net) - abs(beta_net)) / abs(alpha_beta_net)
        if typed_hedge_symbols and abs(beta_net) < abs(alpha_beta_net) and alpha_beta_net
        else 0.0
    )
    now = _as_utc(as_of_ts)
    funding_clock_age = (
        (now - account.last_funding_ts.astimezone(UTC)).total_seconds() / 3600.0
        if account.last_funding_ts is not None
        else None
    )
    committed_theses = _committed_alpha_thesis_provenance(state, account, cycle=cycle)

    positions = []
    for symbol, position in sorted(account.positions.items()):
        row = by_symbol[symbol]
        notional = position.qty * marks[symbol]
        entry_notional = position.qty * position.entry_price
        momentum_pct = float(row.get("momentum_pct") or 0.0)
        side_opposed_momentum_pct = momentum_pct if position.direction == "short" else -momentum_pct
        momentum_windows = {
            hours: float(row.get(f"momentum_{hours}h_pct") or 0.0) for hours in (6, 24, 72, 168)
        }
        beta_adjusted_windows = {
            hours: float(row.get(f"beta_adjusted_momentum_{hours}h_pct") or 0.0)
            for hours in (6, 24, 72, 168)
        }
        relative_path_72 = beta_adjusted_windows[72]
        relative_path_168 = beta_adjusted_windows[168]
        relative_coherent = relative_path_72 * relative_path_168 > 0.0
        realized_vol = float(row.get("realized_vol") or 0.0)
        relative_threshold_pct = max(3.0, realized_vol * 100.0 * 0.10)
        persistent_relative_trend = (
            relative_coherent
            and max(abs(relative_path_72), abs(relative_path_168)) >= relative_threshold_pct
        )
        relative_trend_direction = (
            1
            if persistent_relative_trend and relative_path_72 > 0.0
            else -1
            if persistent_relative_trend
            else 0
        )
        opposed_sign = 1.0 if position.direction == "short" else -1.0
        path_72 = momentum_windows[72]
        path_168 = momentum_windows[168]
        coherent_path = path_72 * path_168 > 0.0
        materially_trending = coherent_path and max(abs(path_72), abs(path_168)) >= (
            _TREND_MOMENTUM_PCT
        )
        if materially_trending:
            trend_direction = 1 if path_72 > 0.0 else -1
            trend_basis = "raw_72h_and_168h"
        elif path_72 == 0.0 and path_168 == 0.0 and abs(momentum_pct) >= (_TREND_MOMENTUM_PCT):
            # Backward-compatible fallback for a historical evidence row created before the
            # multi-horizon schema. Fresh cycle evidence always carries both horizons.
            trend_direction = 1 if momentum_pct > 0.0 else -1
            trend_basis = "legacy_199h_fallback"
        else:
            trend_direction = 0
            trend_basis = "mixed_or_small_multi_horizon_path"
        position_direction = 1 if position.direction == "long" else -1
        side_opposed_trend = trend_direction != 0 and trend_direction != position_direction
        funding_bps = float(
            row.get("conservative_funding_8h_bps")
            if row.get("conservative_funding_8h_bps") is not None
            else row.get("expected_funding_8h_bps") or 0.0
        )
        seat_carry_bps = funding_bps if position.direction == "short" else -funding_bps
        expected_carry = seat_carry_bps / 1e4 * notional
        seat_unrealized = unrealized_by_symbol[symbol]
        carry_recovery_intervals = (
            abs(seat_unrealized) / expected_carry
            if seat_unrealized < 0.0 and expected_carry > 0.0
            else None
        )
        expected_carry_horizon = expected_carry * _MAX_CARRY_HORIZON_INTERVALS
        opened_ts = position.opened_ts
        if opened_ts.tzinfo is None:
            opened_ts = opened_ts.replace(tzinfo=UTC)
        else:
            opened_ts = opened_ts.astimezone(UTC)
        position_age_hours = max(0.0, (now - opened_ts).total_seconds() / 3600.0)
        funding_intervals_held = position_age_hours / 8.0
        lifetime_net_pnl = (
            position.realized_pnl
            + seat_unrealized
            + position.accrued_funding
            - position.accrued_fees
            - position.accrued_slippage
        )
        positions.append(
            {
                "symbol": symbol,
                "side": position.direction,
                "seat_role": position.seat_role,
                "committed_thesis": committed_theses.get(symbol),
                "qty": position.qty,
                "entry_mark": position.entry_price,
                "current_mark": marks[symbol],
                "notional": notional,
                "gross_share": (notional / gross) if gross > 0.0 else 0.0,
                "unrealized_pnl": seat_unrealized,
                "unrealized_pnl_frac_entry_notional": (
                    seat_unrealized / entry_notional if entry_notional > 0.0 else 0.0
                ),
                "realized_pnl": position.realized_pnl,
                "lifetime_net_pnl": lifetime_net_pnl,
                "accrued_funding": position.accrued_funding,
                "accrued_fees": position.accrued_fees,
                "accrued_slippage": position.accrued_slippage,
                "opened_ts": opened_ts.isoformat(),
                "opened_cycle": position.opened_cycle,
                "position_age_hours": position_age_hours,
                "funding_intervals_held": funding_intervals_held,
                "past_max_hold_horizon": (funding_intervals_held > _MAX_CARRY_HORIZON_INTERVALS),
                "cycles_held": (
                    max(1, cycle - position.opened_cycle + 1)
                    if position.opened_cycle is not None
                    else None
                ),
                "beta_clamped": float(row.get("beta_clamped", row.get("beta_btc", 1.0))),
                "momentum_pct": momentum_pct,
                "side_opposed_momentum_pct": side_opposed_momentum_pct,
                **{f"momentum_{hours}h_pct": momentum_windows[hours] for hours in (6, 24, 72, 168)},
                **{
                    f"side_opposed_momentum_{hours}h_pct": opposed_sign * momentum_windows[hours]
                    for hours in (6, 24, 72, 168)
                },
                **{
                    f"beta_adjusted_momentum_{hours}h_pct": beta_adjusted_windows[hours]
                    for hours in (6, 24, 72, 168)
                },
                **{
                    f"side_opposed_beta_adjusted_momentum_{hours}h_pct": (
                        opposed_sign * beta_adjusted_windows[hours]
                    )
                    for hours in (6, 24, 72, 168)
                },
                "momentum_acceleration_24h_pct": float(
                    row.get("momentum_acceleration_24h_pct") or 0.0
                ),
                "drawdown_from_72h_high_pct": float(row.get("drawdown_from_72h_high_pct") or 0.0),
                "realized_vol": realized_vol,
                "persistent_relative_trend": persistent_relative_trend,
                "relative_trend_direction": (
                    "up"
                    if relative_trend_direction > 0
                    else "down"
                    if relative_trend_direction < 0
                    else "none"
                ),
                "relative_trend_threshold_pct": relative_threshold_pct,
                "relative_trend_strength_vol_units": (
                    max(abs(relative_path_72), abs(relative_path_168)) / (realized_vol * 100.0)
                    if realized_vol > 0.0
                    else None
                ),
                "side_opposed_relative_trend": (
                    relative_trend_direction != 0 and relative_trend_direction != position_direction
                ),
                "price_regime": (
                    "trend"
                    if trend_direction != 0
                    else "transition"
                    if max(abs(momentum_pct), *(abs(value) for value in momentum_windows.values()))
                    >= 10.0
                    else "chop"
                ),
                "trend_direction": (
                    "up" if trend_direction > 0 else "down" if trend_direction < 0 else "none"
                ),
                "trend_basis": trend_basis,
                "side_opposed_trend": side_opposed_trend,
                "seat_carry_bps_per_8h": seat_carry_bps,
                "expected_carry_usd_per_8h": expected_carry,
                "max_carry_horizon_intervals": _MAX_CARRY_HORIZON_INTERVALS,
                "expected_carry_usd_max_horizon": expected_carry_horizon,
                "carry_recovery_intervals": carry_recovery_intervals,
                "max_horizon_carry_loss_coverage_frac": (
                    expected_carry_horizon / abs(seat_unrealized) if seat_unrealized < 0.0 else None
                ),
            }
        )

    latest_cycle = int(ledger_rows[-1]["cycle"]) if ledger_rows else None
    # Consume the same exact objects that passed provenance replay above. Never reparse or aggregate
    # a looser raw representation after the trust boundary has approved a stricter one.
    agent_performance = _agent_performance(
        [record.model_dump(mode="json") for record in parsed_score_records]
    )
    forecast_performance = {
        **_forecast_performance(forecast_rows),
        **_pending_forecast_inventory(state, forecast_rows, now=now),
    }
    drawdown_frac = (equity / peak_equity - 1.0) if peak_equity > 0.0 else 0.0
    pm_performance = agent_performance["roles"]["pm"]
    defensive_drawdown = drawdown_frac <= -0.10
    negative_calibrated_pm_edge = (
        int(pm_performance.get("scored_books") or 0) > 0
        and float(pm_performance.get("average_realized_edge_ex_funding_frac") or 0.0) < 0.0
    )
    return {
        "schema_version": PERFORMANCE_SCHEMA_VERSION,
        "cycle": cycle,
        "as_of_ts": now.isoformat(),
        "paper_only": True,
        "bindings": {
            "evidence_sha256": canonical_sha256(evidence),
            "account_sha256": canonical_sha256(account.to_dict()),
            "risk_model_sha256": canonical_sha256(risk_model),
            "meta_sha256": canonical_sha256(meta) if meta is not None else "",
        },
        "profit_objective": (
            "Maximize repeatable net PAPER PnL after funding, fees, slippage, and opportunity "
            "cost while preserving dollar/beta neutrality. Persistent beta-adjusted relative "
            "price alpha is the anchor; conservative history-qualified carry may lead only in "
            "verified chop. Do not manufacture trades or signals."
        ),
        "desk": {
            "starting_capital": starting_capital,
            "equity": equity,
            "net_pnl": equity - starting_capital,
            "return_frac": (equity / starting_capital - 1.0) if starting_capital > 0.0 else 0.0,
            "peak_equity": peak_equity,
            "drawdown_frac": drawdown_frac,
            "realized_pnl": account.realized_pnl,
            "unrealized_pnl": unrealized,
            "funding_net": funding_net,
            "fees_paid": account.fees_paid,
            "slippage_paid": account.slippage_paid,
            "total_turnover_usd": sum(float(row.get("turnover_usd") or 0.0) for row in ledger_rows),
            "gross_usd": gross,
            "deploy_frac": (gross / equity) if equity > 0.0 else 0.0,
            "alpha_gross_usd": alpha_gross,
            "alpha_deploy_frac": alpha_gross / equity if equity > 0.0 else 0.0,
            "alpha_longs_usd": alpha_longs,
            "alpha_shorts_usd": alpha_shorts,
            "hedge_gross_usd": hedge_gross,
            "hedge_deploy_frac": hedge_gross / equity if equity > 0.0 else 0.0,
            "longs_usd": longs,
            "shorts_usd": shorts,
            "dollar_residual_frac": (abs(longs - shorts) / gross if gross > 0.0 else 0.0),
            "beta_net_usd": beta_net,
            "beta_residual": beta_net / equity if equity > 0.0 else 0.0,
            "alpha_beta_net_usd_before_hedge": alpha_beta_net,
            "hedge_beta_net_usd": hedge_beta_net,
            "hedge_beta_reduction_frac": hedge_beta_reduction_frac,
            "risk_capacity_context": {
                "drawdown_at_or_beyond_10pct": defensive_drawdown,
                "calibration_eligible_pm_edge_negative": negative_calibrated_pm_edge,
                "defensive_reference_active": (defensive_drawdown and negative_calibrated_pm_edge),
                "ordinary_total_gross_reference": [0.90, 1.15],
                "defensive_total_gross_reference": [0.55, 0.75],
                "decision_authority": (
                    "descriptive_only; GPT PM chooses and GPT Adversary is sole veto"
                ),
                "sample_warning": (
                    "Use only calibration-eligible scheduled-horizon books; small samples do "
                    "not establish positive Sharpe."
                ),
            },
            "last_completed_cycle": latest_cycle,
            "last_funding_ts": (
                account.last_funding_ts.astimezone(UTC).isoformat()
                if account.last_funding_ts is not None
                else None
            ),
            "funding_clock_age_hours": funding_clock_age,
            "recent_windows": _recent_windows(ledger_rows, equity, starting_capital),
            "time_performance": _time_based_performance(
                ledger_rows, heartbeat_rows, now=now, equity=equity
            ),
            "portfolio_risk": _portfolio_risk_context(
                account, marks, equity, risk_model, by_symbol
            ),
        },
        "positions": positions,
        "closed_leg_performance": _closed_leg_performance(state, account),
        "agent_performance": agent_performance,
        "pm_forecast_performance": forecast_performance,
        "candidate_opportunity_performance": _candidate_opportunity_performance(candidate_rows),
        "frozen_benchmarks": _benchmark_performance(
            state,
            parsed_score_records,
            shadow_gross_usd=starting_capital,
        ),
        "data_quality": {
            "ledger_rows": len(ledger_rows),
            "ledger_duplicate_cycles_removed": ledger_duplicates,
            "heartbeat_rows": len(heartbeat_rows),
            "latest_heartbeat_ts": (str(heartbeat_rows[-1].get("ts")) if heartbeat_rows else None),
            "score_rows": len(parsed_score_records),
            "score_duplicate_cycles_removed": score_duplicates,
            "latest_scored_cycle": (
                parsed_score_records[-1].cycle if parsed_score_records else None
            ),
            "forecast_score_rows": len(forecast_rows),
            "candidate_score_rows": len(candidate_rows),
            "committed_alpha_thesis_available": sum(
                thesis["available"] for thesis in committed_theses.values()
            ),
            "committed_alpha_thesis_unavailable_symbols": sorted(
                symbol
                for symbol, thesis in committed_theses.items()
                if thesis["available"] is not True
            ),
        },
    }


def write_performance_snapshot(path: str | Path, snapshot: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2) + "\n")
    os.replace(tmp, target)
