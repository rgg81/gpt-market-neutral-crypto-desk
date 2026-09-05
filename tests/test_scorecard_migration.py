from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import futures_fund.scorecard_migration as migration
from futures_fund.account import PaperAccount
from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.durable_io import canonical_json_bytes, canonical_json_sha256
from futures_fund.reconcile_commit import recover_reconcile_transaction, stage_reconcile_transaction
from futures_fund.reflection import (
    CANONICAL_BTC_SYMBOL,
    _build_score_record,
    canonical_daily_score_observation,
    score_record_is_manifest_bound,
)
from futures_fund.scorecard import CURRENT_SCORE_SCHEMA_VERSION, ScoreRecord
from futures_fund.state_transaction import current_account_sha256

ALPHA = "A/USDT:USDT"
START = datetime(2026, 8, 1, 0, 7, tzinfo=UTC)


def _runtime_provenance(timestamp: datetime) -> dict:
    body = {
        "schema_version": 1,
        "captured_at": timestamp.isoformat(),
        "test": True,
    }
    return {**body, "provenance_sha256": canonical_json_sha256(body)}


def _reads() -> dict[str, list[dict]]:
    return {
        role: [
            {
                "symbol": ALPHA,
                "lean": "long" if role == "technical" else "flat",
                "conviction": 0.7 if role == "technical" else 0.0,
                "rationale": "fixture",
                "evidence": [],
            },
            {
                "symbol": CANONICAL_BTC_SYMBOL,
                "lean": "flat",
                "conviction": 0.0,
                "rationale": "benchmark",
                "evidence": [],
            },
        ]
        for role in ("sentiment", "technical", "futures")
    }


def _commit_cycle(
    state: Path,
    cycle: int,
    timestamp: datetime,
    *,
    alpha_mark: float,
) -> None:
    evidence = [
        {
            "symbol": ALPHA,
            "mark": alpha_mark,
            "beta_btc": 0.5,
            "beta_clamped": 0.5,
            "expected_funding_8h_bps": -1.0,
            "as_of_ts": timestamp.isoformat(),
        },
        {
            "symbol": CANONICAL_BTC_SYMBOL,
            "mark": 50_000.0 + cycle * 100.0,
            "beta_btc": 1.0,
            "beta_clamped": 1.0,
            "expected_funding_8h_bps": 0.5,
            "as_of_ts": timestamp.isoformat(),
        },
    ]
    book = {
        "legs": (
            [
                {
                    "symbol": ALPHA,
                    "side": "long",
                    "target_notional": 1_000.0,
                    "seat_role": "alpha",
                    "rationale": "fixture alpha",
                }
            ]
            if cycle == 1
            else []
        ),
        "notes": "fixture book",
    }
    report = {
        "cycle": cycle,
        "decision_ts": timestamp.isoformat(),
        "fees_paid_cycle": 1.0 if cycle == 1 else 0.0,
        "slippage_paid_cycle": 0.5 if cycle == 1 else 0.0,
    }
    scoring_marks = {
        "as_of_ts": timestamp.isoformat(),
        "marks": {
            ALPHA: alpha_mark,
            CANONICAL_BTC_SYMBOL: 50_000.0 + cycle * 100.0,
        },
    }
    meta = {
        "cycle": cycle,
        "now": timestamp.isoformat(),
        "btc_symbol": CANONICAL_BTC_SYMBOL,
        "scoring_marks_sha256": canonical_json_sha256(scoring_marks),
        "evidence_sha256": canonical_json_sha256(evidence),
    }
    for name, value in (
        ("evidence", evidence),
        ("meta", meta),
        ("reads", _reads()),
        ("book", book),
        ("adversary", {"accept": True, "objections": []}),
        ("report", report),
    ):
        save_output(state, cycle, name, value, cadence="rebal")
    artifacts = {
        "evidence": evidence,
        "meta": meta,
        "reads": _reads(),
        "book": book,
        "adversary": {"accept": True, "objections": []},
        "report": report,
        "scoring_marks": scoring_marks,
    }
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=cycle,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0),
        artifacts=artifacts,
        equity_ts=timestamp,
        equity=20_000.0,
        ledger={
            "cycle": cycle,
            "opening_equity": 20_000.0,
            "closing_equity": 20_000.0,
        },
        runtime_provenance=_runtime_provenance(timestamp),
    )
    recover_reconcile_transaction(state)


def _fixture(tmp_path: Path, *, cycles: int = 2) -> tuple[Path, Path, Path, dict]:
    state = tmp_path / "state"
    memory = tmp_path / "memory"
    memory.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    lock = logs / "desk-cycle.lock"
    lock.write_bytes(b"")
    for cycle in range(1, cycles + 1):
        _commit_cycle(
            state,
            cycle,
            START + timedelta(days=cycle - 1),
            alpha_mark=100.0 + 5.0 * (cycle - 1),
        )
    observation = canonical_daily_score_observation(state, 1)
    assert observation is not None
    observation_cycle, observation_ts, marks, artifact_sha256 = observation
    target = _build_score_record(
        state,
        scored_cycle=1,
        cur_marks=marks,
        now=observation_ts.isoformat(),
        btc_symbol=CANONICAL_BTC_SYMBOL,
        cadence="rebal",
        outcome_observation_cycle=observation_cycle,
        outcome_scoring_marks_sha256=artifact_sha256,
        outcome_provenance="manifest_bound",
    ).model_dump(mode="json")
    assert target["score_schema_version"] == CURRENT_SCORE_SCHEMA_VERSION
    return state, memory, lock, target


def _sparse(target: dict) -> dict:
    return {
        key: (
            {
                book_key: target["book"][book_key]
                for book_key in migration.LEGACY_V1_BOOK_FIELDS
            }
            if key == "book"
            else target[key]
        )
        for key in migration.LEGACY_V1_TOP_FIELDS
    }


def _materialized_sparse(sparse: dict) -> dict:
    return ScoreRecord.model_validate(sparse).model_dump(
        mode="json", exclude={"score_schema_version"}
    )


def _write_source(
    state: Path,
    memory: Path,
    target: dict,
    *,
    attribution: dict,
    score: dict,
    extra_rows: list[dict] | None = None,
) -> tuple[bytes, bytes]:
    attribution_bytes = json.dumps(attribution, indent=2).encode() + b"\n"
    attribution_path = cycle_dir(state, 1, cadence="rebal") / "attribution.json"
    attribution_path.write_bytes(attribution_bytes)
    rows = [score, *(extra_rows or [])]
    source_scorecard = b"".join(
        json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows
    )
    (memory / "scorecard.jsonl").write_bytes(source_scorecard)
    return source_scorecard, attribution_bytes


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _migrate(state: Path, memory: Path, lock: Path) -> dict:
    return migration.migrate_scorecard(state, memory, desk_lock_path=lock)


def test_v1_enrichment_archives_exact_sources_and_preserves_unverified_rows(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    unverified = {"cycle": 99, "outcome_provenance": "legacy_unverified"}
    source_scorecard, source_attribution = _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
        extra_rows=[unverified],
    )
    untouched = {
        path: path.read_bytes()
        for path in (
            state / "account.json",
            state / "ledger.jsonl",
            state / "equity-history.jsonl",
            cycle_dir(state, 1, cadence="rebal") / "complete.json",
            cycle_dir(state, 2, cadence="rebal") / "complete.json",
        )
    }

    result = _migrate(state, memory, lock)

    assert result["migrated_cycles"] == [1]
    rows = [json.loads(line) for line in (memory / "scorecard.jsonl").read_text().splitlines()]
    normalized_unverified = ScoreRecord.model_validate(
        unverified, strict=True
    ).model_dump(mode="json")
    assert rows == [target, normalized_unverified]
    assert json.loads(
        (cycle_dir(state, 1, cadence="rebal") / "attribution.json").read_text()
    ) == target
    generation = (
        memory
        / migration.ARCHIVE_ROOT
        / "generations"
        / result["generation_id"]
    )
    assert (generation / "source-scorecard.jsonl").read_bytes() == source_scorecard
    assert (
        generation / "source-attributions" / "cycle-1.json"
    ).read_bytes() == source_attribution
    assert (generation / "complete.json").exists()
    assert (memory / migration.PROTOCOL_FILE).exists()
    assert not (memory / migration.WAL_FILE).exists()
    assert all(path.read_bytes() == content for path, content in untouched.items())
    assert score_record_is_manifest_bound(state, ScoreRecord.model_validate(rows[0]))


def test_full_preversion_current_row_is_stamped_without_relabelling(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    implicit = {key: value for key, value in target.items() if key != "score_schema_version"}
    _write_source(state, memory, target, attribution=implicit, score=implicit)

    result = _migrate(state, memory, lock)

    row = json.loads((memory / "scorecard.jsonl").read_text())
    assert row == target
    manifest_path = (
        memory
        / migration.ARCHIVE_ROOT
        / "generations"
        / result["generation_id"]
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["attributions"]["1"]["classification"] == "implicit_current"


def test_canonicalizes_legacy_z_timestamp_without_changing_the_instant(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    sparse["scored_at"] = sparse["scored_at"].replace("+00:00", "Z")
    # evaluation_horizon is unchanged because Z names the exact same instant.
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )

    _migrate(state, memory, lock)

    migrated = json.loads((memory / "scorecard.jsonl").read_text())
    assert migrated["scored_at"] == target["scored_at"]


@pytest.mark.parametrize("target_name", ["scorecard", "attribution"])
def test_completed_protocol_rejects_exact_source_downgrade(tmp_path, target_name):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    source_scorecard, source_attribution = _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    _migrate(state, memory, lock)
    if target_name == "scorecard":
        (memory / "scorecard.jsonl").write_bytes(source_scorecard)
    else:
        (cycle_dir(state, 1, cadence="rebal") / "attribution.json").write_bytes(
            source_attribution
        )

    with pytest.raises(ValueError, match="changed or downgraded"):
        _migrate(state, memory, lock)


def test_manifest_attribution_cannot_be_downgraded_to_unverified_score(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    downgraded = {**_materialized_sparse(sparse), "outcome_provenance": "legacy_unverified"}
    _write_source(state, memory, target, attribution=sparse, score=downgraded)
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="canonical bound outcome|downgrade a manifest"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


def test_score_and_attribution_cannot_jointly_downgrade_a_bound_outcome(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = {
        **_sparse(target),
        "outcome_provenance": "legacy_unverified",
    }
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="canonical bound outcome"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


def test_shared_legacy_field_tamper_fails_before_any_write(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    sparse["book"] = {**sparse["book"], "gross_pnl": sparse["book"]["gross_pnl"] + 1.0}
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="does not replay canonical fields"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("tamper", ["origin", "outcome", "identity"])
def test_origin_outcome_or_identity_tamper_fails_without_migration_writes(tmp_path, tamper):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    if tamper == "identity":
        sparse["outcome_observation_cycle"] = 3
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    if tamper == "origin":
        report = cycle_dir(state, 1, cadence="rebal") / "report.json"
        report.write_text('{"tampered":true}')
    elif tamper == "outcome":
        marks = cycle_dir(state, 2, cadence="rebal") / "scoring_marks.json"
        marks.write_text('{"as_of_ts":"2026-08-02T00:07:00+00:00","marks":{}}')
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="canonical|outcome|replay"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize(
    "corruption",
    [
        b'{"cycle":1}\n{malformed}\n',
        b'{"cycle":1,"cycle":1}\n',
    ],
)
def test_malformed_or_duplicate_key_scorecard_fails_without_writes(tmp_path, corruption):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(state, memory, target, attribution=sparse, score=sparse)
    (memory / "scorecard.jsonl").write_bytes(corruption)
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="malformed|duplicate"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


def test_duplicate_cycle_scorecard_fails_without_writes(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(state, memory, target, attribution=sparse, score=sparse)
    path = memory / "scorecard.jsonl"
    path.write_bytes(path.read_bytes() * 2)
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="duplicate scorecard cycle"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("coercible_cycle", [1.0, "1", True])
def test_coercible_noninteger_cycle_is_never_sealed(tmp_path, coercible_cycle):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = {**_sparse(target), "cycle": coercible_cycle}
    _write_source(state, memory, target, attribution=sparse, score=sparse)
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="invalid score cycle"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


def test_completed_migration_is_idempotent(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    first = _migrate(state, memory, lock)
    after_first = _tree_bytes(tmp_path)

    second = _migrate(state, memory, lock)

    assert second == {
        "migrated": False,
        "recovered": False,
        "already_complete": True,
        "generation_id": first["generation_id"],
        "migrated_cycles": [1],
    }
    assert _tree_bytes(tmp_path) == after_first


@pytest.mark.parametrize("failure_boundary", range(1, 12))
def test_every_durable_boundary_recovers_to_one_complete_generation(
    tmp_path, monkeypatch, failure_boundary
):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    original_write = migration.durable_write_bytes
    original_unlink = migration.durable_unlink
    boundaries = 0

    def fail_after(call, *args, **kwargs):
        nonlocal boundaries
        result = call(*args, **kwargs)
        boundaries += 1
        if boundaries == failure_boundary:
            raise OSError(f"injected crash {failure_boundary}")
        return result

    monkeypatch.setattr(
        migration,
        "durable_write_bytes",
        lambda *args, **kwargs: fail_after(original_write, *args, **kwargs),
    )
    monkeypatch.setattr(
        migration,
        "durable_unlink",
        lambda *args, **kwargs: fail_after(original_unlink, *args, **kwargs),
    )
    with pytest.raises(OSError, match=f"injected crash {failure_boundary}"):
        _migrate(state, memory, lock)

    monkeypatch.setattr(migration, "durable_write_bytes", original_write)
    monkeypatch.setattr(migration, "durable_unlink", original_unlink)
    recovered = _migrate(state, memory, lock)
    assert recovered["generation_id"]
    assert not (memory / migration.WAL_FILE).exists()
    row = json.loads((memory / "scorecard.jsonl").read_text())
    assert row == target
    assert json.loads(
        (cycle_dir(state, 1, cadence="rebal") / "attribution.json").read_text()
    ) == target
    assert _migrate(state, memory, lock)["already_complete"] is True


def test_wal_recovery_rejects_third_state_without_overwriting_it(tmp_path, monkeypatch):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    original_write = migration.durable_write_bytes
    writes = 0

    def crash_after_wal(*args, **kwargs):
        nonlocal writes
        result = original_write(*args, **kwargs)
        writes += 1
        if writes == 6:
            raise OSError("crash after WAL")
        return result

    monkeypatch.setattr(migration, "durable_write_bytes", crash_after_wal)
    with pytest.raises(OSError, match="crash after WAL"):
        _migrate(state, memory, lock)
    monkeypatch.setattr(migration, "durable_write_bytes", original_write)
    attribution = cycle_dir(state, 1, cadence="rebal") / "attribution.json"
    attribution.write_bytes(b'{"third":"state"}\n')
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="neither staged source nor target"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


def test_wal_recovery_rejects_conflicting_terminal_receipt_before_canonical_writes(
    tmp_path, monkeypatch
):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    original_write = migration.durable_write_bytes
    writes = 0

    def crash_after_wal(*args, **kwargs):
        nonlocal writes
        result = original_write(*args, **kwargs)
        writes += 1
        if writes == 6:
            raise OSError("crash after WAL")
        return result

    monkeypatch.setattr(migration, "durable_write_bytes", crash_after_wal)
    with pytest.raises(OSError, match="crash after WAL"):
        _migrate(state, memory, lock)
    monkeypatch.setattr(migration, "durable_write_bytes", original_write)
    (memory / migration.PROTOCOL_FILE).write_text('{"conflicting":true}\n')
    before = _tree_bytes(tmp_path)

    with pytest.raises(ValueError, match="conflicting score migration protocol"):
        _migrate(state, memory, lock)

    assert _tree_bytes(tmp_path) == before


def test_host_lock_refuses_overlap_before_reading_or_writing(tmp_path):
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(state, memory, target, attribution=sparse, score=sparse)
    before = _tree_bytes(tmp_path)
    descriptor = os.open(lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RuntimeError, match="another desk cycle"):
            _migrate(state, memory, lock)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert _tree_bytes(tmp_path) == before


def test_recovery_preserves_strict_v2_row_appended_after_target_publish(tmp_path, monkeypatch):
    state, memory, lock, target = _fixture(tmp_path, cycles=3)
    sparse = _sparse(target)
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
    )
    original_write = migration.durable_write_bytes
    writes = 0

    def crash_after_scorecard(*args, **kwargs):
        nonlocal writes
        result = original_write(*args, **kwargs)
        writes += 1
        if writes == 8:
            raise OSError("crash after target scorecard")
        return result

    monkeypatch.setattr(migration, "durable_write_bytes", crash_after_scorecard)
    with pytest.raises(OSError, match="crash after target scorecard"):
        _migrate(state, memory, lock)
    monkeypatch.setattr(migration, "durable_write_bytes", original_write)

    observation = canonical_daily_score_observation(state, 2)
    assert observation is not None
    observation_cycle, observation_ts, marks, artifact_sha256 = observation
    appended = _build_score_record(
        state,
        scored_cycle=2,
        cur_marks=marks,
        now=observation_ts.isoformat(),
        btc_symbol=CANONICAL_BTC_SYMBOL,
        cadence="rebal",
        outcome_observation_cycle=observation_cycle,
        outcome_scoring_marks_sha256=artifact_sha256,
        outcome_provenance="manifest_bound",
    ).model_dump(mode="json")
    with (memory / "scorecard.jsonl").open("ab") as handle:
        handle.write(canonical_json_bytes(appended) + b"\n")
    (cycle_dir(state, 2, cadence="rebal") / "attribution.json").write_bytes(
        canonical_json_bytes(appended) + b"\n"
    )

    result = _migrate(state, memory, lock)

    assert result["recovered"] is True
    rows = [json.loads(line) for line in (memory / "scorecard.jsonl").read_text().splitlines()]
    assert rows == [target, appended]
    assert _migrate(state, memory, lock)["already_complete"] is True


def test_normal_scorecard_rewrite_and_append_remain_valid_after_migration(tmp_path):
    from futures_fund.reflection import _read_scorecard, _write_scorecard

    state, memory, lock, target = _fixture(tmp_path, cycles=3)
    sparse = _sparse(target)
    unverified = {"cycle": 99, "outcome_provenance": "legacy_unverified"}
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
        extra_rows=[unverified],
    )
    _migrate(state, memory, lock)

    observation = canonical_daily_score_observation(state, 2)
    assert observation is not None
    observation_cycle, observation_ts, marks, artifact_sha256 = observation
    appended = _build_score_record(
        state,
        scored_cycle=2,
        cur_marks=marks,
        now=observation_ts.isoformat(),
        btc_symbol=CANONICAL_BTC_SYMBOL,
        cadence="rebal",
        outcome_observation_cycle=observation_cycle,
        outcome_scoring_marks_sha256=artifact_sha256,
        outcome_provenance="manifest_bound",
    )
    (cycle_dir(state, 2, cadence="rebal") / "attribution.json").write_text(
        appended.model_dump_json(indent=2)
    )
    records = _read_scorecard(memory / "scorecard.jsonl", state_dir=state)
    _write_scorecard(
        memory / "scorecard.jsonl",
        sorted([*records, appended], key=lambda record: record.cycle),
    )

    verified = _migrate(state, memory, lock)

    assert verified["already_complete"] is True
    by_cycle = {
        row["cycle"]: row
        for row in map(json.loads, (memory / "scorecard.jsonl").read_text().splitlines())
    }
    assert by_cycle[99]["score_schema_version"] is None
    assert by_cycle[2]["score_schema_version"] == CURRENT_SCORE_SCHEMA_VERSION


def test_completed_protocol_allows_legacy_row_to_upgrade_after_late_outcome(tmp_path):
    from futures_fund.reflection import _read_scorecard, _write_scorecard

    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    waiting = {"cycle": 2, "outcome_provenance": "legacy_unverified"}
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
        extra_rows=[waiting],
    )
    _migrate(state, memory, lock)

    _commit_cycle(
        state,
        3,
        START + timedelta(days=2),
        alpha_mark=112.0,
    )
    observation = canonical_daily_score_observation(state, 2)
    assert observation is not None
    observation_cycle, observation_ts, marks, artifact_sha256 = observation
    upgraded = _build_score_record(
        state,
        scored_cycle=2,
        cur_marks=marks,
        now=observation_ts.isoformat(),
        btc_symbol=CANONICAL_BTC_SYMBOL,
        cadence="rebal",
        outcome_observation_cycle=observation_cycle,
        outcome_scoring_marks_sha256=artifact_sha256,
        outcome_provenance="manifest_bound",
    )
    (cycle_dir(state, 2, cadence="rebal") / "attribution.json").write_text(
        upgraded.model_dump_json(indent=2)
    )
    records = [
        record
        for record in _read_scorecard(memory / "scorecard.jsonl", state_dir=state)
        if record.cycle != 2
    ]
    _write_scorecard(
        memory / "scorecard.jsonl",
        sorted([*records, upgraded], key=lambda record: record.cycle),
    )

    verified = _migrate(state, memory, lock)

    assert verified["already_complete"] is True
    by_cycle = {
        row["cycle"]: row
        for row in map(json.loads, (memory / "scorecard.jsonl").read_text().splitlines())
    }
    assert by_cycle[2]["score_schema_version"] == CURRENT_SCORE_SCHEMA_VERSION
    assert by_cycle[2]["outcome_provenance"] == "manifest_bound"


def test_completed_protocol_rejects_score_only_downgrade_after_legacy_upgrade(tmp_path):
    from futures_fund.reflection import _read_scorecard, _write_scorecard

    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    waiting = {"cycle": 2, "outcome_provenance": "legacy_unverified"}
    _write_source(
        state,
        memory,
        target,
        attribution=sparse,
        score=_materialized_sparse(sparse),
        extra_rows=[waiting],
    )
    first = _migrate(state, memory, lock)
    generation = memory / migration.ARCHIVE_ROOT / "generations" / first["generation_id"]
    archived_rows = {
        row["cycle"]: row
        for row in map(
            json.loads, (generation / "target-scorecard.jsonl").read_text().splitlines()
        )
    }

    _commit_cycle(
        state,
        3,
        START + timedelta(days=2),
        alpha_mark=112.0,
    )
    observation = canonical_daily_score_observation(state, 2)
    assert observation is not None
    observation_cycle, observation_ts, marks, artifact_sha256 = observation
    upgraded = _build_score_record(
        state,
        scored_cycle=2,
        cur_marks=marks,
        now=observation_ts.isoformat(),
        btc_symbol=CANONICAL_BTC_SYMBOL,
        cadence="rebal",
        outcome_observation_cycle=observation_cycle,
        outcome_scoring_marks_sha256=artifact_sha256,
        outcome_provenance="manifest_bound",
    )
    (cycle_dir(state, 2, cadence="rebal") / "attribution.json").write_text(
        upgraded.model_dump_json(indent=2)
    )
    records = [
        record
        for record in _read_scorecard(memory / "scorecard.jsonl", state_dir=state)
        if record.cycle != 2
    ]
    _write_scorecard(
        memory / "scorecard.jsonl",
        sorted([*records, upgraded], key=lambda record: record.cycle),
    )
    assert _migrate(state, memory, lock)["already_complete"] is True

    restored = [
        record
        for record in _read_scorecard(memory / "scorecard.jsonl", state_dir=state)
        if record.cycle != 2
    ]
    restored.append(ScoreRecord.model_validate(archived_rows[2]))
    _write_scorecard(
        memory / "scorecard.jsonl",
        sorted(restored, key=lambda record: record.cycle),
    )

    with pytest.raises(ValueError, match="changed or downgraded"):
        _migrate(state, memory, lock)


def test_active_shared_wal_blocks_reflection_and_performance_readers(tmp_path, monkeypatch):
    from futures_fund.performance import _score_jsonl
    from futures_fund.reflection import scored_cycles
    from futures_fund.scorecard import SCORECARD_MIGRATION_WAL_FILE

    assert migration.WAL_FILE == SCORECARD_MIGRATION_WAL_FILE
    state, memory, lock, target = _fixture(tmp_path)
    sparse = _sparse(target)
    _write_source(state, memory, target, attribution=sparse, score=sparse)
    original_write = migration.durable_write_bytes
    writes = 0

    def crash_after_wal(*args, **kwargs):
        nonlocal writes
        result = original_write(*args, **kwargs)
        writes += 1
        if writes == 6:
            raise OSError("leave valid WAL")
        return result

    monkeypatch.setattr(migration, "durable_write_bytes", crash_after_wal)
    with pytest.raises(OSError, match="leave valid WAL"):
        _migrate(state, memory, lock)
    monkeypatch.setattr(migration, "durable_write_bytes", original_write)
    assert (memory / SCORECARD_MIGRATION_WAL_FILE).exists()

    with pytest.raises(ValueError, match="migration is incomplete"):
        scored_cycles(memory, state_dir=state)
    with pytest.raises(ValueError, match="migration is incomplete"):
        _score_jsonl(memory / "scorecard.jsonl")

    migration.recover_scorecard_migration(
        state, memory, desk_lock_path=lock
    )
    assert not (memory / SCORECARD_MIGRATION_WAL_FILE).exists()
    assert scored_cycles(memory, state_dir=state) == {1}
