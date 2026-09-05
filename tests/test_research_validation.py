from __future__ import annotations

import math

import numpy as np
import pytest

from futures_fund.research_validation import (
    FrozenReturnMatrix,
    TrialDefinition,
    evaluate_frozen_trials,
    load_trial_registry,
    register_frozen_trial,
    validate_frozen_trial_window,
    walk_forward_splits,
)
from futures_fund.vendor.overfit_detector import probability_of_backtest_overfitting


def _trial(trial_id: str, role: str = "challenger", digest: str = "a" * 64):
    return TrialDefinition(
        trial_id=trial_id,
        role=role,
        policy_sha256=digest,
        source_tree_sha256="d" * 64,
        dependency_lock_sha256="e" * 64,
        config_sha256="f" * 64,
        registered_at="2026-09-05T00:00:00Z",
        evaluation_start="2026-09-06T00:00:00Z",
        max_label_horizon_hours=168,
        description=f"frozen {trial_id}",
        frozen_parameters={"gross": 0.95},
    )


def test_trial_registry_is_append_only_and_idempotent(tmp_path):
    path = tmp_path / "trials.jsonl"
    first = register_frozen_trial(path, _trial("champion-v1", "champion"))
    assert register_frozen_trial(path, _trial("champion-v1", "champion")) == first
    assert len(path.read_text().splitlines()) == 1
    register_frozen_trial(path, _trial("cash-v1", "benchmark", "b" * 64))
    assert [row.definition.trial_id for row in load_trial_registry(path)] == [
        "champion-v1",
        "cash-v1",
    ]

    with pytest.raises(ValueError, match="already frozen differently"):
        register_frozen_trial(path, _trial("champion-v1", "champion", "c" * 64))


def test_walk_forward_splits_are_chronological_purged_and_embargoed():
    splits = walk_forward_splits(
        90,
        min_train=30,
        test_size=10,
        purge=5,
        embargo=2,
    )
    assert splits[0].model_dump() == {
        "train_start": 0,
        "train_stop": 30,
        "test_start": 35,
        "test_stop": 45,
    }
    assert splits[1].test_start == 47
    assert all(split.train_stop + 5 == split.test_start for split in splits)
    assert all(
        left.test_stop + 2 == right.test_start
        for left, right in zip(splits, splits[1:], strict=False)
    )


def test_pbo_validates_matrix_and_supports_purge_embargo():
    with pytest.raises(ValueError, match="2D"):
        probability_of_backtest_overfitting(np.ones(40))
    with pytest.raises(ValueError, match="finite"):
        probability_of_backtest_overfitting(np.array([[0.0, math.nan]] * 40))

    rng = np.random.default_rng(7)
    result = probability_of_backtest_overfitting(
        rng.normal(0.0, 0.01, size=(60, 3)),
        n_groups=6,
        n_test_groups=2,
        purge_observations=2,
        embargo_observations=2,
    )
    assert result.n_paths == 15
    assert 0.0 <= result.pbo <= 1.0


def test_frozen_trial_evaluation_reports_active_and_overfit_statistics(tmp_path):
    path = tmp_path / "trials.jsonl"
    register_frozen_trial(path, _trial("champion-v1", "champion"))
    register_frozen_trial(path, _trial("cash-v1", "benchmark", "b" * 64))
    register_frozen_trial(path, _trial("challenger-v1", "challenger", "c" * 64))
    registry = load_trial_registry(path)
    alternating = [0.004 if idx % 2 == 0 else -0.002 for idx in range(60)]
    result = evaluate_frozen_trials(
        {
            "champion-v1": alternating,
            "cash-v1": [0.0] * 60,
            "challenger-v1": [value * 0.5 for value in alternating],
        },
        registry,
        benchmark_trial_id="cash-v1",
        purge_observations=1,
        embargo_observations=1,
    )
    assert result["measurement_only"] is True
    assert result["registered_trial_count"] == 3
    assert result["common_observations"] == 60
    assert result["pbo"]["paths"] == 15
    assert result["trials"]["champion-v1"]["information_ratio"] > 0.0
    assert result["trials"]["cash-v1"]["information_ratio"] == 0.0
    assert result["minimum_track_record"]["satisfied"] is False


def test_evaluation_refuses_unregistered_or_misaligned_returns(tmp_path):
    path = tmp_path / "trials.jsonl"
    register_frozen_trial(path, _trial("cash-v1", "benchmark"))
    registry = load_trial_registry(path)
    with pytest.raises(ValueError, match="every and only"):
        evaluate_frozen_trials(
            {"cash-v1": [0.0, 0.0], "invented": [0.0, 0.0]},
            registry,
            benchmark_trial_id="cash-v1",
        )


def test_frozen_return_matrix_requires_one_strict_common_clock():
    payload = {
        "schema_version": 1,
        "as_of_ts": "2026-09-07T00:00:00Z",
        "observation_timestamps": ["2026-09-06T00:00:00Z", "2026-09-07T00:00:00Z"],
        "return_basis": "net_after_fees_funding_slippage_execution",
        "data_lineage_sha256": "a" * 64,
        "universe_lineage_sha256": "b" * 64,
        "execution_model_sha256": "c" * 64,
        "returns": {"champion-v1": [0.01, -0.01]},
    }
    assert FrozenReturnMatrix.model_validate(payload).returns["champion-v1"] == [0.01, -0.01]
    payload["observation_timestamps"] = [
        "2026-09-07T00:00:00Z",
        "2026-09-06T00:00:00Z",
    ]
    with pytest.raises(ValueError, match="strictly increasing"):
        FrozenReturnMatrix.model_validate(payload)


def test_trials_must_be_frozen_before_the_common_forward_window(tmp_path):
    with pytest.raises(ValueError, match="no later"):
        TrialDefinition.model_validate(
            {
                **_trial("late-v1").model_dump(),
                "registered_at": "2026-09-07T00:00:00Z",
                "evaluation_start": "2026-09-06T00:00:00Z",
            }
        )

    registry_path = tmp_path / "trials.jsonl"
    register_frozen_trial(registry_path, _trial("champion-v1", "champion"))
    matrix = FrozenReturnMatrix.model_validate(
        {
            "schema_version": 1,
            "as_of_ts": "2026-09-07T00:00:00Z",
            "observation_timestamps": [
                "2026-09-05T12:00:00Z",
                "2026-09-07T00:00:00Z",
            ],
            "return_basis": "net_after_fees_funding_slippage_execution",
            "data_lineage_sha256": "a" * 64,
            "universe_lineage_sha256": "b" * 64,
            "execution_model_sha256": "c" * 64,
            "returns": {"champion-v1": [0.0, 0.01]},
        }
    )
    with pytest.raises(ValueError, match="begins before"):
        validate_frozen_trial_window(matrix, load_trial_registry(registry_path))


def test_return_matrix_as_of_cannot_precede_observations():
    with pytest.raises(ValueError, match="as_of_ts cannot precede"):
        FrozenReturnMatrix.model_validate(
            {
                "schema_version": 1,
                "as_of_ts": "2026-09-06T00:00:00Z",
                "observation_timestamps": [
                    "2026-09-06T00:00:00Z",
                    "2026-09-07T00:00:00Z",
                ],
                "return_basis": "net_after_fees_funding_slippage_execution",
                "data_lineage_sha256": "a" * 64,
                "universe_lineage_sha256": "b" * 64,
                "execution_model_sha256": "c" * 64,
                "returns": {"champion-v1": [0.0, 0.01]},
            }
        )
