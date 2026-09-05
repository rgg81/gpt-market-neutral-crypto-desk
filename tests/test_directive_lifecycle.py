from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from futures_fund.account import PaperAccount
from futures_fund.directives import (
    DIRECTIVE_CLAIM_ROOT,
    DirectiveContext,
    build_directive_commit_expectation,
    build_directive_receipt,
    claim_next_directive,
    directive_lifecycle_status,
    finalize_cycle_directive,
    load_completed_directive_receipt,
    parse_directive_capabilities,
    recover_directive_lifecycle,
    validate_directive_receipt,
)
from futures_fund.durable_io import canonical_json_sha256
from futures_fund.reconcile_commit import (
    recover_reconcile_transaction,
    stage_reconcile_transaction,
    transaction_path,
)

NOW = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)
TEXT = (
    '<!-- desk-directive-capabilities: ["controlled_restart_graduation"] -->\n'
    "Authorize this one cycle only.\n"
)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "live_state"
    source = tmp_path / "ops" / "next-cycle-directive.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    return state, source


def _provenance() -> dict:
    body = {"schema_version": 1, "captured_at": NOW.isoformat(), "test": True}
    return {**body, "provenance_sha256": canonical_json_sha256(body)}


def _claim(state: Path, source: Path, *, cycle: int = 1, payload: bytes | None = None) -> dict:
    source.write_bytes(payload if payload is not None else TEXT.encode())
    result = claim_next_directive(state, cycle=cycle, now=NOW)
    assert result is not None
    return result


def _stage_claim(
    state: Path,
    claim: dict,
    *,
    cycle: int = 1,
    include_receipt: bool = True,
) -> dict:
    receipt = build_directive_receipt(claim, cycle=cycle)
    artifacts = {
        "book": {"legs": []},
        "evidence": [],
        "report": {"cycle": cycle, "ran_at": NOW.isoformat(), "equity": 20_000.0},
    }
    if include_receipt:
        artifacts["binding_user_directive"] = receipt
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=None,
        cycle=cycle,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0, last_funding_ts=NOW),
        artifacts=artifacts,
        equity_ts=NOW,
        equity=20_000.0,
        ledger={"cycle": cycle, "closing_equity": 20_000.0},
        runtime_provenance=_provenance(),
        directive_expectation=build_directive_commit_expectation(claim, cycle=cycle),
    )
    return receipt


def _complete_claim(state: Path, claim: dict, *, cycle: int = 1) -> dict:
    receipt = _stage_claim(state, claim, cycle=cycle)
    recover_reconcile_transaction(state)
    return receipt


_UNSET = object()


def _stage_generation(
    state: Path,
    *,
    cycle: int = 1,
    directive_expectation: object = _UNSET,
    directive_receipt: object = _UNSET,
) -> dict:
    artifacts = {
        "book": {"legs": []},
        "evidence": [],
        "report": {"cycle": cycle, "ran_at": NOW.isoformat(), "equity": 20_000.0},
    }
    if directive_receipt is not _UNSET:
        artifacts["binding_user_directive"] = directive_receipt
    kwargs = {}
    if directive_expectation is not _UNSET:
        kwargs["directive_expectation"] = directive_expectation
    return stage_reconcile_transaction(
        state,
        expected_base_account_sha256=None,
        cycle=cycle,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0, last_funding_ts=NOW),
        artifacts=artifacts,
        equity_ts=NOW,
        equity=20_000.0,
        ledger={"cycle": cycle, "closing_equity": 20_000.0},
        runtime_provenance=_provenance(),
        **kwargs,
    )


def _claim_payload_path(state: Path, claim: dict) -> Path:
    return state / DIRECTIVE_CLAIM_ROOT / f"{claim['claim_id']}.md"


def test_typed_header_is_exact_sorted_and_fail_closed():
    assert parse_directive_capabilities(TEXT) == ("controlled_restart_graduation",)
    assert parse_directive_capabilities("ordinary instruction\n") == ()
    with pytest.raises(ValueError, match="malformed"):
        parse_directive_capabilities(
            '<!-- desk-directive-capabilities:["controlled_restart_graduation"] -->\n'
        )
    with pytest.raises(ValueError, match="unknown"):
        parse_directive_capabilities('<!-- desk-directive-capabilities: ["unknown"] -->\n')


def test_claim_moves_one_exact_inbox_instance_into_state(tmp_path):
    state, source = _paths(tmp_path)
    payload = TEXT.replace("\n", "\r\n").encode()

    claim = _claim(state, source, payload=payload)

    claimed_path = _claim_payload_path(state, claim)
    assert not source.exists()
    assert claimed_path.read_bytes() == payload
    assert claim["text"].encode() == payload
    assert claim["payload_sha256"] == sha256(payload).hexdigest()
    assert directive_lifecycle_status(state) == {
        "status": "claimed_pending",
        "cycle": 1,
        "claim_id": claim["claim_id"],
        "payload_state": "claim",
        "queued_source_present": False,
    }


def test_same_cycle_retry_replays_claim_and_preserves_queued_reissue(tmp_path):
    state, source = _paths(tmp_path)
    first = _claim(state, source)
    source.write_text(TEXT)

    retry = claim_next_directive(state, cycle=1, now=NOW)

    assert retry == first
    assert source.read_text() == TEXT
    assert directive_lifecycle_status(state)["queued_source_present"] is True
    with pytest.raises(RuntimeError, match="different cycle"):
        claim_next_directive(state, cycle=2, now=NOW)


def test_restore_race_cannot_overwrite_newly_queued_inbox(tmp_path, monkeypatch):
    import futures_fund.directives as directives

    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    claim_path = _claim_payload_path(state, claim)
    context = DirectiveContext.from_state_dir(state)
    replacement = "new instruction queued during recovery\n"
    real_rename = directives._rename_noreplace

    def queue_before_atomic_rename(rename_source, destination):
        if Path(destination) == source:
            source.write_text(replacement)
        return real_rename(rename_source, destination)

    monkeypatch.setattr(directives, "_rename_noreplace", queue_before_atomic_rename)
    with pytest.raises(FileExistsError, match="destination already exists"):
        directives._restore_mismatched_claim(context, claim_path)

    assert source.read_text() == replacement
    assert claim_path.read_text() == TEXT
    assert context.active_path.exists()


def test_cross_directory_rename_fsyncs_destination_before_source(tmp_path, monkeypatch):
    import futures_fund.directives as directives

    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "instruction.md"
    destination = destination_dir / "claim.md"
    source.write_text(TEXT)
    fsync_calls: list[Path] = []
    monkeypatch.setattr(
        directives,
        "fsync_directory",
        lambda path: fsync_calls.append(Path(path)),
    )

    directives._rename_durable(source, destination)

    assert fsync_calls == [destination_dir, source_dir]
    assert destination.read_text() == TEXT
    assert not source.exists()


def test_claim_fsyncs_source_payload_before_renaming_it(tmp_path, monkeypatch):
    import futures_fund.directives as directives

    state, source = _paths(tmp_path)
    source.write_text(TEXT)
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    events: list[str] = []
    real_fsync = directives.os.fsync
    real_fsync_directory = directives.fsync_directory
    real_write = directives.durable_write_json
    real_rename = directives._rename_noreplace

    def observe_fsync(descriptor):
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == source_identity:
            events.append("source_file_fsync")
        return real_fsync(descriptor)

    def observe_directory_fsync(path):
        if Path(path) == source.parent:
            events.append("source_parent_fsync")
        if Path(path) == tmp_path:
            events.append("repo_root_fsync")
        return real_fsync_directory(path)

    def observe_active_write(path, value, **kwargs):
        if Path(path).name == "active.json":
            events.append("active_write")
        return real_write(path, value, **kwargs)

    def require_sync_before_rename(rename_source, destination):
        if Path(rename_source) == source:
            events.append("claim_rename")
        return real_rename(rename_source, destination)

    monkeypatch.setattr(directives.os, "fsync", observe_fsync)
    monkeypatch.setattr(directives, "fsync_directory", observe_directory_fsync)
    monkeypatch.setattr(directives, "durable_write_json", observe_active_write)
    monkeypatch.setattr(directives, "_rename_noreplace", require_sync_before_rename)

    assert claim_next_directive(state, cycle=1, now=NOW) is not None
    assert events[:5] == [
        "source_file_fsync",
        "source_parent_fsync",
        "repo_root_fsync",
        "active_write",
        "claim_rename",
    ]
    assert events[5:] == ["source_parent_fsync"]


def test_claim_fails_closed_when_atomic_noreplace_is_unavailable(tmp_path, monkeypatch):
    import futures_fund.directives as directives

    state, source = _paths(tmp_path)
    source.write_text(TEXT)
    monkeypatch.setattr(directives, "_RENAMEAT2", None)

    with pytest.raises(RuntimeError, match="requires Linux renameat2"):
        claim_next_directive(state, cycle=1, now=NOW)

    assert source.read_text() == TEXT
    assert (state / DIRECTIVE_CLAIM_ROOT / "active.json").exists()
    assert list((state / DIRECTIVE_CLAIM_ROOT).glob("*.md")) == []


def test_recovery_retires_only_same_inode_source_duplicate(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    claim_path = _claim_payload_path(state, claim)
    os.link(claim_path, source)

    status = directive_lifecycle_status(state)
    assert status["payload_state"] == "duplicate_source"
    assert status["queued_source_present"] is True

    assert claim_next_directive(state, cycle=1, now=NOW) == claim
    assert not source.exists()
    assert claim_path.read_text() == TEXT

    _complete_claim(state, claim)
    assert finalize_cycle_directive(state, 1)["consumed"] is True
    assert claim_next_directive(state, cycle=2, now=NOW) is None


def test_recovery_finishes_state_owned_duplicate_quarantine(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    claim_path = _claim_payload_path(state, claim)
    duplicate = state / DIRECTIVE_CLAIM_ROOT / f"{claim['claim_id']}.source-duplicate"
    os.link(claim_path, source)
    os.rename(source, duplicate)

    assert directive_lifecycle_status(state)["payload_state"] == "duplicate_source"
    recovered = recover_directive_lifecycle(state)

    assert recovered["status"] == "claimed_pending"
    assert not duplicate.exists()
    assert not source.exists()
    assert claim_path.read_text() == TEXT


def test_no_inbox_is_a_noop_without_creating_claim_storage(tmp_path):
    state, _source = _paths(tmp_path)

    assert claim_next_directive(state, cycle=1, now=NOW) is None
    assert not (state / DIRECTIVE_CLAIM_ROOT).exists()


def test_reconcile_staging_rejects_claim_created_after_absence_was_observed(tmp_path):
    state, source = _paths(tmp_path)
    expectation = build_directive_commit_expectation(None, cycle=1)
    claim = _claim(state, source)

    with pytest.raises(RuntimeError, match="expected no directive"):
        stage_reconcile_transaction(
            state,
            expected_base_account_sha256=None,
            cycle=1,
            cadence="rebal",
            account=PaperAccount(cash=20_000.0, last_funding_ts=NOW),
            artifacts={
                "book": {"legs": []},
                "evidence": [],
                "report": {"cycle": 1, "ran_at": NOW.isoformat(), "equity": 20_000.0},
            },
            equity_ts=NOW,
            equity=20_000.0,
            ledger={"cycle": 1, "closing_equity": 20_000.0},
            runtime_provenance=_provenance(),
            directive_expectation=expectation,
        )

    assert not transaction_path(state).exists()
    assert not (state / "account.json").exists()
    assert not (state / "rebal" / "cycle" / "1" / "complete.json").exists()
    assert _claim_payload_path(state, claim).read_text() == TEXT


def test_new_reconcile_wal_always_binds_explicit_directive_absence(tmp_path):
    state, _source = _paths(tmp_path)

    transaction = _stage_generation(state)

    assert transaction["version"] == 2
    assert transaction["directive_expectation"] == (
        build_directive_commit_expectation(None, cycle=1)
    )


def test_reconcile_binding_rejects_cycle_receipt_and_expectation_mismatches(tmp_path):
    state, source = _paths(tmp_path)
    with pytest.raises(ValueError, match="expectation cycle"):
        _stage_generation(
            state,
            directive_expectation=build_directive_commit_expectation(None, cycle=2),
        )

    claim = _claim(state, source)
    expectation = build_directive_commit_expectation(claim, cycle=1)
    with pytest.raises(ValueError, match="receipt presence mismatch"):
        _stage_generation(state, directive_expectation=expectation)
    with pytest.raises(RuntimeError, match="expected no directive"):
        _stage_generation(
            state,
            directive_receipt=build_directive_receipt(claim, cycle=1),
        )

    other_state, other_source = _paths(tmp_path / "other")
    other_claim = _claim(other_state, other_source)
    other_receipt = build_directive_receipt(other_claim, cycle=1)
    with pytest.raises(ValueError, match="does not match reconcile expectation"):
        _stage_generation(
            state,
            directive_expectation=expectation,
            directive_receipt=other_receipt,
        )

    empty_state, _empty_source = _paths(tmp_path / "empty")
    with pytest.raises(ValueError, match="receipt presence mismatch"):
        _stage_generation(
            empty_state,
            directive_expectation=build_directive_commit_expectation(None, cycle=1),
            directive_receipt=other_receipt,
        )


def test_current_wal_requires_expectation_but_legacy_v1_remains_recoverable(tmp_path):
    state, _source = _paths(tmp_path / "current")
    _stage_generation(state)
    transaction = transaction_path(state)
    raw = json.loads(transaction.read_text())
    raw.pop("directive_expectation")
    raw.pop("intent_sha256")
    raw["intent_sha256"] = canonical_json_sha256(raw)
    transaction.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="lacks directive expectation"):
        recover_reconcile_transaction(state)
    assert not (state / "account.json").exists()

    legacy_state, _legacy_source = _paths(tmp_path / "legacy")
    _stage_generation(legacy_state)
    legacy_transaction = transaction_path(legacy_state)
    legacy = json.loads(legacy_transaction.read_text())
    legacy["version"] = 1
    legacy.pop("directive_expectation")
    legacy.pop("intent_sha256")
    legacy["intent_sha256"] = canonical_json_sha256(legacy)
    legacy_transaction.write_text(json.dumps(legacy))

    assert recover_reconcile_transaction(legacy_state)["recovered"] is True
    assert (legacy_state / "account.json").exists()

    conflicted_state, conflicted_source = _paths(tmp_path / "legacy-conflict")
    _stage_generation(conflicted_state)
    conflicted_transaction = transaction_path(conflicted_state)
    legacy = json.loads(conflicted_transaction.read_text())
    legacy["version"] = 1
    legacy.pop("directive_expectation")
    legacy.pop("intent_sha256")
    legacy["intent_sha256"] = canonical_json_sha256(legacy)
    conflicted_transaction.write_text(json.dumps(legacy))
    parked = conflicted_state / "parked-legacy-reconcile.json"
    conflicted_transaction.rename(parked)
    claim = _claim(conflicted_state, conflicted_source)
    parked.rename(conflicted_transaction)

    with pytest.raises(RuntimeError, match="expected no directive"):
        recover_reconcile_transaction(conflicted_state)
    assert not (conflicted_state / "account.json").exists()
    assert _claim_payload_path(conflicted_state, claim).read_text() == TEXT


def test_pending_reconcile_and_completed_cycle_refuse_a_late_claim(tmp_path):
    state, source = _paths(tmp_path)
    expectation = build_directive_commit_expectation(None, cycle=1)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=None,
        cycle=1,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0, last_funding_ts=NOW),
        artifacts={
            "book": {"legs": []},
            "evidence": [],
            "report": {"cycle": 1, "ran_at": NOW.isoformat(), "equity": 20_000.0},
        },
        equity_ts=NOW,
        equity=20_000.0,
        ledger={"cycle": 1, "closing_equity": 20_000.0},
        runtime_provenance=_provenance(),
        directive_expectation=expectation,
    )
    source.write_text("late instruction\n")

    with pytest.raises(RuntimeError, match="pending reconcile transaction"):
        claim_next_directive(state, cycle=1, now=NOW)
    assert source.read_text() == "late instruction\n"

    recover_reconcile_transaction(state)
    with pytest.raises(RuntimeError, match="already completed cycle"):
        claim_next_directive(state, cycle=1, now=NOW)
    assert source.read_text() == "late instruction\n"


def test_reconcile_recovery_revalidates_durable_directive_absence(tmp_path):
    state, source = _paths(tmp_path)
    expectation = build_directive_commit_expectation(None, cycle=1)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=None,
        cycle=1,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0, last_funding_ts=NOW),
        artifacts={
            "book": {"legs": []},
            "evidence": [],
            "report": {"cycle": 1, "ran_at": NOW.isoformat(), "equity": 20_000.0},
        },
        equity_ts=NOW,
        equity=20_000.0,
        ledger={"cycle": 1, "closing_equity": 20_000.0},
        runtime_provenance=_provenance(),
        directive_expectation=expectation,
    )
    transaction = transaction_path(state)
    parked = state / "parked-reconcile-transaction.json"
    transaction.rename(parked)
    claim = _claim(state, source)
    parked.rename(transaction)

    with pytest.raises(RuntimeError, match="expected no directive"):
        recover_reconcile_transaction(state)

    assert transaction.exists()
    assert not (state / "account.json").exists()
    assert not (state / "rebal" / "cycle" / "1" / "complete.json").exists()
    assert _claim_payload_path(state, claim).read_text() == TEXT


def test_complete_marker_with_wal_cannot_finalize_claim_before_wal_recovery(
    tmp_path,
    monkeypatch,
):
    import futures_fund.reconcile_commit as commit

    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    receipt = build_directive_receipt(claim, cycle=1)
    _stage_generation(
        state,
        directive_expectation=build_directive_commit_expectation(claim, cycle=1),
        directive_receipt=receipt,
    )
    transaction = transaction_path(state)
    real_unlink = commit.durable_unlink

    def interrupt_wal_unlink(path):
        if Path(path) == transaction:
            raise RuntimeError("crash before WAL unlink")
        return real_unlink(path)

    monkeypatch.setattr(commit, "durable_unlink", interrupt_wal_unlink)
    with pytest.raises(RuntimeError, match="crash before WAL unlink"):
        recover_reconcile_transaction(state)
    assert transaction.exists()
    assert (state / "rebal" / "cycle" / "1" / "complete.json").exists()

    source.write_text("next-cycle instruction\n")
    with pytest.raises(RuntimeError, match="pending reconcile transaction"):
        claim_next_directive(state, cycle=1, now=NOW)
    with pytest.raises(RuntimeError, match="pending reconcile transaction"):
        finalize_cycle_directive(state, 1)
    assert _claim_payload_path(state, claim).read_text() == TEXT
    assert source.read_text() == "next-cycle instruction\n"

    monkeypatch.setattr(commit, "durable_unlink", real_unlink)
    assert recover_reconcile_transaction(state)["already_complete"] is True
    assert finalize_cycle_directive(state, 1)["consumed"] is True
    assert source.read_text() == "next-cycle instruction\n"


def test_reconcile_wal_accepts_exact_already_consumed_terminal_claim(tmp_path, monkeypatch):
    import futures_fund.reconcile_commit as commit

    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _stage_generation(
        state,
        directive_expectation=build_directive_commit_expectation(claim, cycle=1),
        directive_receipt=build_directive_receipt(claim, cycle=1),
    )
    transaction = transaction_path(state)
    real_unlink = commit.durable_unlink

    def interrupt_wal_unlink(path):
        if Path(path) == transaction:
            raise RuntimeError("crash before WAL unlink")
        return real_unlink(path)

    monkeypatch.setattr(commit, "durable_unlink", interrupt_wal_unlink)
    with pytest.raises(RuntimeError, match="crash before WAL unlink"):
        recover_reconcile_transaction(state)
    monkeypatch.setattr(commit, "durable_unlink", real_unlink)

    parked = state / "parked-reconcile-transaction.json"
    transaction.rename(parked)
    assert finalize_cycle_directive(state, 1)["consumed"] is True
    parked.rename(transaction)

    assert recover_reconcile_transaction(state)["already_complete"] is True
    assert not transaction.exists()


def test_uncommitted_generation_never_consumes_claim(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _stage_claim(state, claim)

    with pytest.raises(RuntimeError, match="pending reconcile transaction"):
        finalize_cycle_directive(state, 1)

    assert _claim_payload_path(state, claim).read_text() == TEXT
    assert transaction_path(state).exists()


def test_completed_generation_consumes_only_claim_and_not_new_identical_inbox(tmp_path):
    state, source = _paths(tmp_path)
    first = _claim(state, source)
    receipt = _complete_claim(state, first)
    source.write_text(TEXT)

    result = finalize_cycle_directive(state, 1)

    assert result["consumed"] is True
    assert result["receipt_sha256"] == receipt["receipt_sha256"]
    assert source.read_text() == TEXT
    assert not _claim_payload_path(state, first).exists()
    second = claim_next_directive(state, cycle=2, now=NOW)
    assert second is not None
    assert second["claim_id"] != first["claim_id"]


def test_finalization_is_idempotent_and_receipt_stays_manifest_bound(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    receipt = _complete_claim(state, claim)

    assert finalize_cycle_directive(state, 1)["consumed"] is True
    assert finalize_cycle_directive(state, 1) == {
        "consumed": False,
        "reason": "claim_absent",
        "cycle": 1,
    }
    assert load_completed_directive_receipt(state, 1) == receipt
    archived = json.loads(
        (state / "rebal" / "cycle" / "1" / "binding_user_directive.json").read_text()
    )
    assert validate_directive_receipt(archived, expected_cycle=1) == archived


def test_manifest_receipt_without_active_claim_or_ack_is_a_conflict(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _complete_claim(state, claim)
    _claim_payload_path(state, claim).unlink()
    (state / DIRECTIVE_CLAIM_ROOT / "active.json").unlink()

    with pytest.raises(RuntimeError, match="consumption acknowledgement"):
        finalize_cycle_directive(state, 1)
    status = directive_lifecycle_status(state)
    assert status["status"] == "conflict"
    assert "consumption acknowledgement" in status["error"]
    with pytest.raises(RuntimeError, match="consumption acknowledgement"):
        recover_directive_lifecycle(state)

    source.write_text("a genuinely new instruction\n")
    with pytest.raises(RuntimeError, match="consumption acknowledgement"):
        claim_next_directive(state, cycle=2, now=NOW)
    assert source.read_text() == "a genuinely new instruction\n"


def test_reconcile_cannot_stage_active_claim_without_bound_receipt(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)

    with pytest.raises(ValueError, match="receipt presence mismatch"):
        _stage_claim(state, claim, include_receipt=False)

    assert not transaction_path(state).exists()
    assert not (state / "account.json").exists()
    assert _claim_payload_path(state, claim).read_text() == TEXT


def test_recovery_completes_cleanup_after_tombstone_rename_crash(tmp_path, monkeypatch):
    import futures_fund.directives as directives

    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _complete_claim(state, claim)
    real_unlink = directives.durable_unlink

    monkeypatch.setattr(
        directives,
        "durable_unlink",
        lambda _path: (_ for _ in ()).throw(RuntimeError("power loss")),
    )
    with pytest.raises(RuntimeError, match="power loss"):
        finalize_cycle_directive(state, 1)
    tomb = state / DIRECTIVE_CLAIM_ROOT / f"{claim['claim_id']}.consuming"
    assert tomb.read_text() == TEXT
    assert directive_lifecycle_status(state)["status"] == "cleanup_pending"

    monkeypatch.setattr(directives, "durable_unlink", real_unlink)
    recovered = recover_directive_lifecycle(state)
    assert recovered["status"] == "recovered_cleanup"
    assert recovered["consumed"] is True
    assert not tomb.exists()


def test_recovery_handles_crash_after_tomb_unlink_before_ack(tmp_path, monkeypatch):
    import futures_fund.directives as directives

    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _complete_claim(state, claim)
    real_write = directives.durable_write_json

    def fail_ack(path, value, **kwargs):
        if Path(path).parent.name == "consumptions":
            raise RuntimeError("ack interrupted")
        return real_write(path, value, **kwargs)

    monkeypatch.setattr(directives, "durable_write_json", fail_ack)
    with pytest.raises(RuntimeError, match="ack interrupted"):
        finalize_cycle_directive(state, 1)
    assert not _claim_payload_path(state, claim).exists()
    assert (state / DIRECTIVE_CLAIM_ROOT / "active.json").exists()

    monkeypatch.setattr(directives, "durable_write_json", real_write)
    assert recover_directive_lifecycle(state)["status"] == "recovered_cleanup"


def test_recovery_handles_crash_after_ack_before_active_unlink(tmp_path, monkeypatch):
    import futures_fund.directives as directives

    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _complete_claim(state, claim)
    active = state / DIRECTIVE_CLAIM_ROOT / "active.json"
    real_unlink = directives.durable_unlink

    def fail_active(path):
        if Path(path) == active:
            raise RuntimeError("active cleanup interrupted")
        return real_unlink(path)

    monkeypatch.setattr(directives, "durable_unlink", fail_active)
    with pytest.raises(RuntimeError, match="active cleanup interrupted"):
        finalize_cycle_directive(state, 1)
    assert active.exists()
    assert (state / DIRECTIVE_CLAIM_ROOT / "consumptions" / "cycle-1.json").exists()

    monkeypatch.setattr(directives, "durable_unlink", real_unlink)
    assert finalize_cycle_directive(state, 1)["consumed"] is True
    assert not active.exists()


def test_receipt_rejects_identity_text_capability_cycle_and_hash_tampering(tmp_path):
    state, source = _paths(tmp_path)
    receipt = build_directive_receipt(_claim(state, source), cycle=1)
    updates = (
        {"text": TEXT + "tamper"},
        {"capabilities": []},
        {"cycle": 2},
        {"claim_id": "f" * 32},
        {"claim_intent_sha256": "not-a-hash"},
        {"source_relpath": "/tmp/attacker-controlled"},
        {"receipt_sha256": "0" * 64},
    )
    for update in updates:
        with pytest.raises(ValueError):
            validate_directive_receipt({**receipt, **update}, expected_cycle=1)


def test_lifecycle_schemas_reject_bool_and_float_integer_lookalikes(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    receipt = build_directive_receipt(claim, cycle=1)

    for field, invalid in (("schema_version", True), ("cycle", 1.0)):
        body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        body[field] = invalid
        forged = {**body, "receipt_sha256": canonical_json_sha256(body)}
        with pytest.raises(ValueError, match="identity/hash/capability mismatch"):
            validate_directive_receipt(forged, expected_cycle=1)

    active_path = state / DIRECTIVE_CLAIM_ROOT / "active.json"
    active = json.loads(active_path.read_text())
    active_body = {
        key: value for key, value in active.items() if key != "claim_intent_sha256"
    }
    active_body["schema_version"] = True
    active_path.write_text(
        json.dumps(
            {
                **active_body,
                "claim_intent_sha256": canonical_json_sha256(active_body),
            }
        )
    )
    assert directive_lifecycle_status(state)["status"] == "conflict"


def test_consumption_schema_rejects_bool_even_with_resealed_hash(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _complete_claim(state, claim)
    finalize_cycle_directive(state, 1)
    ack_path = state / DIRECTIVE_CLAIM_ROOT / "consumptions" / "cycle-1.json"
    ack = json.loads(ack_path.read_text())
    body = {key: value for key, value in ack.items() if key != "consumption_sha256"}
    body["schema_version"] = True
    ack_path.write_text(
        json.dumps({**body, "consumption_sha256": canonical_json_sha256(body)})
    )

    status = directive_lifecycle_status(state)
    assert status["status"] == "conflict"
    assert "consumption receipt mismatch" in status["error"]
    with pytest.raises(ValueError, match="consumption receipt mismatch"):
        recover_directive_lifecycle(state)


def test_receipt_second_read_detects_manifest_toctou(tmp_path, monkeypatch):
    import futures_fund.reconcile_commit as commit

    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _complete_claim(state, claim)
    artifact = state / "rebal" / "cycle" / "1" / "binding_user_directive.json"
    real_completed_hash = commit.completed_artifact_sha256

    def mutate_after_manifest_check(*args, **kwargs):
        result = real_completed_hash(*args, **kwargs)
        value = json.loads(artifact.read_text())
        artifact.write_text(json.dumps({**value, "text": value["text"] + "changed"}))
        return result

    monkeypatch.setattr(commit, "completed_artifact_sha256", mutate_after_manifest_check)
    with pytest.raises(ValueError, match="changed after manifest verification"):
        load_completed_directive_receipt(state, 1)


def test_symlinked_inbox_is_rejected_without_touching_target(tmp_path):
    state, source = _paths(tmp_path)
    target = tmp_path / "outside.md"
    target.write_text(TEXT)
    source.symlink_to(target)

    with pytest.raises(ValueError, match="non-symlink inbox"):
        claim_next_directive(state, cycle=1, now=NOW)
    assert target.read_text() == TEXT
    assert source.is_symlink()
    status = directive_lifecycle_status(state)
    assert status["status"] == "conflict"
    assert status["queued_source_present"] is True


def test_symlinked_claim_root_is_rejected_without_touching_target(tmp_path):
    state, source = _paths(tmp_path)
    state.mkdir()
    target = tmp_path / "outside-directory"
    target.mkdir()
    (state / DIRECTIVE_CLAIM_ROOT).symlink_to(target, target_is_directory=True)
    source.write_text(TEXT)

    with pytest.raises(ValueError, match="symlink"):
        claim_next_directive(state, cycle=1, now=NOW)
    assert source.read_text() == TEXT
    assert list(target.iterdir()) == []


def test_symlinked_claim_payload_cannot_authorize_external_cleanup(tmp_path):
    state, source = _paths(tmp_path)
    claim = _claim(state, source)
    _complete_claim(state, claim)
    claim_path = _claim_payload_path(state, claim)
    target = tmp_path / "outside.md"
    target.write_text("must survive\n")
    claim_path.unlink()
    claim_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlinked claim"):
        finalize_cycle_directive(state, 1)
    assert target.read_text() == "must survive\n"
    assert claim_path.is_symlink()


def test_orphaned_claim_payload_halts_and_is_never_laundered(tmp_path):
    state, source = _paths(tmp_path)
    claim_root = state / DIRECTIVE_CLAIM_ROOT
    claim_root.mkdir(parents=True)
    orphan = claim_root / f"{'a' * 32}.md"
    orphan.write_text("orphaned prior instruction\n")
    source.write_text(TEXT)

    assert directive_lifecycle_status(state)["status"] == "conflict"
    with pytest.raises(RuntimeError, match="orphaned"):
        claim_next_directive(state, cycle=1, now=NOW)
    assert orphan.read_text() == "orphaned prior instruction\n"
    assert source.read_text() == TEXT


def test_fifo_inbox_is_reported_as_conflict_without_blocking(tmp_path):
    state, source = _paths(tmp_path)
    os.mkfifo(source)

    status = directive_lifecycle_status(state)

    assert status["status"] == "conflict"
    assert status["queued_source_present"] is True
    with pytest.raises(ValueError, match="regular non-symlink inbox"):
        claim_next_directive(state, cycle=1, now=NOW)


def test_read_only_status_on_absent_roots_creates_nothing(tmp_path):
    state = tmp_path / "live_state"

    assert directive_lifecycle_status(state) == {
        "status": "idle",
        "queued_source_present": False,
    }
    assert list(tmp_path.iterdir()) == []


def test_claim_rejects_noncanonical_source_argument_and_bad_cycle(tmp_path):
    state, source = _paths(tmp_path)
    source.write_text(TEXT)

    with pytest.raises(ValueError, match="canonical repository inbox"):
        claim_next_directive(
            state,
            cycle=1,
            now=NOW,
            source_path=tmp_path / "somewhere-else.md",
        )
    with pytest.raises(ValueError, match="positive integer"):
        claim_next_directive(state, cycle=True, now=NOW)
    assert source.read_text() == TEXT
