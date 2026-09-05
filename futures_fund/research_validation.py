"""Frozen-trial research validation for the PAPER desk.

This module measures candidate policies; it never promotes a policy, sizes a position, or creates
a trade.  Definitions are append-only so the number of attempted variants is not forgotten when
multiple-testing statistics are computed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.stats import kurtosis, skew

from futures_fund.metrics import max_drawdown, sharpe, trial_sharpe_std
from futures_fund.vendor.overfit_detector import (
    deflated_sharpe_ratio,
    minimum_backtest_length,
    probability_of_backtest_overfitting,
)


def _aware_timestamp(raw: str, *, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {label} timestamp {raw!r}") from exc
    if value.tzinfo is None:
        raise ValueError(f"{label} timestamp must be timezone-aware")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _durable_replace(path: Path, payload: bytes) -> None:
    """Atomically replace ``path`` and make both data and directory entry durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


class TrialDefinition(BaseModel):
    """Immutable identity of one strategy policy that entered the research process."""

    model_config = ConfigDict(extra="forbid")
    trial_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    role: Literal["champion", "challenger", "benchmark"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_at: str = Field(min_length=1)
    evaluation_start: str = Field(min_length=1)
    max_label_horizon_hours: int = Field(ge=1)
    description: str = Field(min_length=1)
    frozen_parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_pre_registration(self) -> TrialDefinition:
        registered = _aware_timestamp(self.registered_at, label="registered_at")
        evaluation = _aware_timestamp(self.evaluation_start, label="evaluation_start")
        if registered > evaluation:
            raise ValueError("trial must be registered no later than its evaluation_start")
        return self


class FrozenReturnMatrix(BaseModel):
    """Common-clock, fully attributed net return streams for frozen policies."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    as_of_ts: str = Field(min_length=1)
    observation_timestamps: list[str] = Field(min_length=2)
    return_basis: Literal["net_after_fees_funding_slippage_execution"]
    data_lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    universe_lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    returns: dict[str, list[float]]

    @model_validator(mode="after")
    def validate_common_clock(self) -> FrozenReturnMatrix:
        parsed = [
            _aware_timestamp(timestamp, label="observation")
            for timestamp in self.observation_timestamps
        ]
        if any(right <= left for left, right in zip(parsed, parsed[1:], strict=False)):
            raise ValueError("observation timestamps must be strictly increasing")
        if _aware_timestamp(self.as_of_ts, label="as_of_ts") < parsed[-1]:
            raise ValueError("as_of_ts cannot precede the final observation")
        expected = len(parsed)
        if not self.returns:
            raise ValueError("return matrix cannot be empty")
        if any(len(values) != expected for values in self.returns.values()):
            raise ValueError("each return stream must match the common observation clock")
        return self


def validate_frozen_trial_window(
    matrix: FrozenReturnMatrix, registry: list[RegisteredTrial]
) -> None:
    """Prove every registered policy was frozen before the shared forward window began."""
    if not registry:
        raise ValueError("research registry cannot be empty")
    if set(matrix.returns) != {row.definition.trial_id for row in registry}:
        raise ValueError("return matrix must cover every and only registered frozen trial")
    first_observation = _aware_timestamp(
        matrix.observation_timestamps[0], label="first observation"
    )
    late = sorted(
        row.definition.trial_id
        for row in registry
        if _aware_timestamp(row.definition.evaluation_start, label="evaluation_start")
        > first_observation
    )
    if late:
        raise ValueError(
            "return matrix begins before registered evaluation_start for trials: "
            f"{late}"
        )


class RegisteredTrial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: TrialDefinition
    definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> RegisteredTrial:
        expected = _sha256(self.definition.model_dump(mode="json"))
        if self.definition_sha256 != expected:
            raise ValueError("trial definition digest mismatch")
        return self


class WalkForwardSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    train_start: int = Field(ge=0)
    train_stop: int = Field(gt=0)
    test_start: int = Field(ge=0)
    test_stop: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> WalkForwardSplit:
        if not self.train_start < self.train_stop <= self.test_start < self.test_stop:
            raise ValueError("walk-forward split boundaries overlap or are unordered")
        return self


def load_trial_registry(path: str | Path) -> list[RegisteredTrial]:
    registry = Path(path)
    if not registry.exists():
        return []
    by_id: dict[str, RegisteredTrial] = {}
    for line_number, line in enumerate(registry.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = RegisteredTrial.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"invalid research registry row {registry}:{line_number}") from exc
        trial_id = item.definition.trial_id
        prior = by_id.get(trial_id)
        if prior is not None and prior != item:
            raise ValueError(f"conflicting research trial id {trial_id}")
        by_id.setdefault(trial_id, item)
    return list(by_id.values())


def register_frozen_trial(path: str | Path, definition: TrialDefinition) -> RegisteredTrial:
    """Append one immutable trial identity, idempotently for an exact repeat."""
    registry = Path(path)
    candidate = RegisteredTrial(
        definition=definition,
        definition_sha256=_sha256(definition.model_dump(mode="json")),
    )
    registry.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry.with_suffix(registry.suffix + ".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            existing = load_trial_registry(registry)
            same_id = [row for row in existing if row.definition.trial_id == definition.trial_id]
            if same_id:
                if same_id != [candidate]:
                    raise ValueError(
                        f"trial id {definition.trial_id} is already frozen differently"
                    )
                return candidate
            rows = [*existing, candidate]
            payload = "".join(row.model_dump_json() + "\n" for row in rows).encode()
            _durable_replace(registry, payload)
            return candidate
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def walk_forward_splits(
    n_observations: int,
    *,
    min_train: int,
    test_size: int,
    purge: int,
    embargo: int,
    rolling_train_size: int | None = None,
) -> list[WalkForwardSplit]:
    """Build chronological, purged walk-forward splits.

    ``purge`` separates the last training label from the test window. ``embargo`` separates one
    test window from the next training extension.  No future observation can enter a train set.
    """
    if min(n_observations, min_train, test_size) <= 0:
        raise ValueError("observation and window sizes must be positive")
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be non-negative")
    if rolling_train_size is not None and rolling_train_size < min_train:
        raise ValueError("rolling_train_size cannot be smaller than min_train")

    splits: list[WalkForwardSplit] = []
    test_start = min_train + purge
    while test_start + test_size <= n_observations:
        train_stop = test_start - purge
        train_start = 0 if rolling_train_size is None else max(0, train_stop - rolling_train_size)
        if train_stop - train_start >= min_train:
            splits.append(
                WalkForwardSplit(
                    train_start=train_start,
                    train_stop=train_stop,
                    test_start=test_start,
                    test_stop=test_start + test_size,
                )
            )
        test_start += test_size + embargo
    return splits


def _series_statistics(values: np.ndarray, periods_per_year: float) -> dict[str, object]:
    curve = np.cumprod(1.0 + values)
    annualized_return: float | None
    if curve[-1] <= 0.0:
        annualized_return = None
    else:
        annualized_return = float(curve[-1] ** (periods_per_year / len(values)) - 1.0)
    observed_sharpe = sharpe(values.tolist(), periods_per_year=periods_per_year)
    raw_skew = float(skew(values, bias=False)) if len(values) >= 3 else math.nan
    raw_kurtosis = (
        float(kurtosis(values, fisher=False, bias=False)) if len(values) >= 4 else math.nan
    )
    return {
        "observations": len(values),
        "cumulative_return": float(curve[-1] - 1.0),
        "annualized_return": annualized_return,
        "annualized_volatility": float(values.std(ddof=1) * math.sqrt(periods_per_year)),
        "annualized_sharpe": observed_sharpe,
        "max_drawdown": max_drawdown(curve.tolist()),
        "skewness": raw_skew if math.isfinite(raw_skew) else None,
        "kurtosis": raw_kurtosis if math.isfinite(raw_kurtosis) else None,
    }


def evaluate_frozen_trials(
    returns: dict[str, list[float]],
    registry: list[RegisteredTrial],
    *,
    benchmark_trial_id: str,
    periods_per_year: float = 365.0,
    n_groups: int = 6,
    n_test_groups: int = 2,
    purge_observations: int = 0,
    embargo_observations: int = 0,
    target_annualized_sharpe: float = 1.0,
) -> dict[str, object]:
    """Evaluate common-horizon frozen return streams without selecting a live policy."""
    if periods_per_year <= 0.0:
        raise ValueError("periods_per_year must be positive")
    registered_ids = [item.definition.trial_id for item in registry]
    if len(registered_ids) != len(set(registered_ids)):
        raise ValueError("registry contains duplicate trial ids")
    if set(returns) != set(registered_ids):
        raise ValueError("returns must cover every and only registered frozen trial")
    if benchmark_trial_id not in returns:
        raise ValueError("benchmark_trial_id is not registered")
    lengths = {len(values) for values in returns.values()}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) < 2:
        raise ValueError("all frozen trials require the same horizon with at least 2 observations")
    arrays = {name: np.asarray(values, dtype=float) for name, values in returns.items()}
    if any(not np.isfinite(values).all() for values in arrays.values()):
        raise ValueError("trial returns must contain only finite values")
    if any(np.any(values <= -1.0) for values in arrays.values()):
        raise ValueError("a simple return cannot be <= -100%")

    matrix = np.column_stack([arrays[trial_id] for trial_id in registered_ids])
    per_period_sharpe_dispersion = trial_sharpe_std(
        [arrays[trial_id].tolist() for trial_id in registered_ids], min_obs=5
    )
    n_observations = matrix.shape[0]
    trial_stats: dict[str, object] = {}
    benchmark = arrays[benchmark_trial_id]
    for trial_id in registered_ids:
        values = arrays[trial_id]
        stats = _series_statistics(values, periods_per_year)
        active = values - benchmark
        stats["active_cumulative_return_vs_benchmark"] = float(
            np.prod(1.0 + values) / np.prod(1.0 + benchmark) - 1.0
        )
        stats["information_ratio"] = sharpe(active.tolist(), periods_per_year=periods_per_year)
        if n_observations >= 10:
            sample_skew = float(stats["skewness"] or 0.0)
            sample_kurtosis = float(stats["kurtosis"] or 3.0)
            dsr = deflated_sharpe_ratio(
                observed_sr=float(stats["annualized_sharpe"]),
                num_trials=len(registered_ids),
                backtest_length=n_observations,
                skewness=sample_skew,
                kurtosis=sample_kurtosis,
                annualization=math.sqrt(periods_per_year),
                sigma_sr=per_period_sharpe_dispersion,
            )
            stats["deflated_sharpe"] = {
                "probability": dsr.dsr_pvalue,
                "expected_max_annualized_sharpe": dsr.expected_max_sr,
                "significant_95pct": dsr.is_significant,
            }
        else:
            stats["deflated_sharpe"] = {
                "probability": None,
                "expected_max_annualized_sharpe": None,
                "significant_95pct": False,
                "warning": "requires at least 10 common observations",
            }
        trial_stats[trial_id] = stats

    pbo: dict[str, object]
    try:
        result = probability_of_backtest_overfitting(
            matrix,
            n_groups=n_groups,
            n_test_groups=n_test_groups,
            purge_observations=purge_observations,
            embargo_observations=embargo_observations,
        )
        pbo = {
            "probability": result.pbo,
            "paths": result.n_paths,
            "overfit_paths": result.n_overfit_paths,
            "mean_oos_rank": result.mean_oos_rank,
            "overfit_gt_50pct": result.is_overfit,
        }
    except ValueError as exc:
        pbo = {"probability": None, "paths": 0, "warning": str(exc)}

    target_per_period = target_annualized_sharpe / math.sqrt(periods_per_year)
    minimum = minimum_backtest_length(target_per_period, confidence=0.95)
    registry_hash = _sha256([row.model_dump(mode="json") for row in registry])
    return {
        "schema_version": 1,
        "measurement_only": True,
        "common_observations": n_observations,
        "registered_trial_count": len(registry),
        "registry_sha256": registry_hash,
        "benchmark_trial_id": benchmark_trial_id,
        "periods_per_year": periods_per_year,
        "purge_observations": purge_observations,
        "embargo_observations": embargo_observations,
        "minimum_track_record": {
            "target_annualized_sharpe": target_annualized_sharpe,
            "confidence": minimum.confidence,
            "required_observations": minimum.min_length,
            "satisfied": n_observations >= minimum.min_length,
        },
        "pbo": pbo,
        "trials": trial_stats,
    }


def write_research_report(path: str | Path, report: dict[str, object]) -> str:
    """Write a canonical report and sibling digest durably; return its SHA-256."""
    target = Path(path)
    payload = _canonical_bytes(report) + b"\n"
    digest = hashlib.sha256(payload).hexdigest()
    _durable_replace(target, payload)
    _durable_replace(target.with_suffix(target.suffix + ".sha256"), f"{digest}\n".encode())
    return digest
