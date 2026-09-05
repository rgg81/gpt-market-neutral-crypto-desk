from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from futures_fund.account import PaperAccount, load_account
from futures_fund.durable_io import canonical_json_sha256
from futures_fund.heartbeat import (
    HeartbeatError,
    recover_heartbeat_transaction,
    stage_heartbeat_transaction,
)
from futures_fund.reconcile_commit import (
    backfill_completion_manifest,
    completed_artifact_is_bound,
    completed_cycle_numbers,
    cycle_is_complete,
    finalize_manifest_migration,
    recover_reconcile_transaction,
    stage_reconcile_transaction,
    transaction_path,
)
from futures_fund.state_transaction import (
    current_account_sha256,
    load_account_events,
    load_account_with_sha256,
)

NOW = datetime(2026, 8, 19, 1, 2, tzinfo=UTC)


def _provenance():
    body = {"schema_version": 1, "captured_at": NOW.isoformat(), "test": True}
    return {**body, "provenance_sha256": canonical_json_sha256(body)}


def _stage(state):
    return stage_reconcile_transaction(
        state,
        expected_base_account_sha256=None,
        cycle=4,
        cadence="rebal",
        account=PaperAccount(cash=20_123.0, last_funding_ts=NOW),
        artifacts={
            "book": {"legs": []},
            "evidence": [],
            "report": {"cycle": 4, "ran_at": NOW.isoformat(), "equity": 20_123.0},
        },
        equity_ts=NOW,
        equity=20_123.0,
        ledger={"cycle": 4, "closing_equity": 20_123.0},
        runtime_provenance=_provenance(),
    )


def _downgrade_pending_reconcile_to_v3(state) -> None:
    path = transaction_path(state)
    transaction = json.loads(path.read_text())
    event = transaction["artifacts"]["account_event"]
    event.pop("event_sha256")
    event.pop("generation_root_sha256")
    event["schema_version"] = 1
    event["event_sha256"] = canonical_json_sha256(event)
    transaction.pop("manifest_version")
    transaction.pop("generation_root_sha256")
    transaction.pop("intent_sha256")
    transaction["intent_sha256"] = canonical_json_sha256(transaction)
    path.write_text(json.dumps(transaction))


def test_reconcile_transaction_replays_idempotently_and_marks_complete(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    result = recover_reconcile_transaction(state)
    assert result == {"recovered": True, "cycle": 4, "already_complete": False}
    assert load_account(state, 0.0).cash == 20_123.0
    assert completed_cycle_numbers(state) == [4]
    assert json.loads((state / "ledger.jsonl").read_text())["cycle"] == 4
    assert not transaction_path(state).exists()
    assert recover_reconcile_transaction(state) == {"recovered": False}
    marker = json.loads((state / "rebal" / "cycle" / "4" / "complete.json").read_text())
    assert marker["manifest"]["version"] == 4
    assert "account_state" in marker["manifest"]["artifact_sha256"]
    assert "runtime_provenance" in marker["manifest"]["artifact_sha256"]
    assert marker["account_event_id"] == load_account_events(state)[-1]["event_id"]


def test_v3_completion_requires_unified_account_event_history(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    assert cycle_is_complete(state, 4, require_manifest=True)

    (state / "account-events.jsonl").unlink()
    assert not cycle_is_complete(state, 4, require_manifest=True)
    with pytest.raises(ValueError, match="orphan v3 generation"):
        stage_reconcile_transaction(
            state,
            expected_base_account_sha256=current_account_sha256(state),
            cycle=5,
            cadence="rebal",
            account=load_account(state, 0.0),
            artifacts={"book": {}, "evidence": [], "report": {}},
            equity_ts=NOW,
            equity=20_123.0,
            ledger={"cycle": 5},
            runtime_provenance=_provenance(),
        )


def test_stale_reconcile_computation_cannot_stage_over_newer_account(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    base = PaperAccount(cash=20_000.0)
    (state / "account.json").write_text(json.dumps(base.to_dict()))
    stale_account, stale_hash = load_account_with_sha256(state, default_cash=0.0)
    assert stale_account.cash == 20_000.0
    (state / "account.json").write_text(json.dumps(PaperAccount(cash=20_100.0).to_dict()))

    with pytest.raises(RuntimeError, match="optimistic account-state lock"):
        stage_reconcile_transaction(
            state,
            expected_base_account_sha256=stale_hash,
            cycle=4,
            cadence="rebal",
            account=PaperAccount(cash=19_900.0),
            artifacts={"book": {}, "evidence": [], "report": {}},
            equity_ts=NOW,
            equity=19_900.0,
            ledger={"cycle": 4},
            runtime_provenance=_provenance(),
        )
    assert load_account(state, 0.0).cash == 20_100.0


def test_manifest_binds_post_cycle_account_snapshot(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    snapshot = state / "rebal" / "cycle" / "4" / "account_state.json"
    value = json.loads(snapshot.read_text())
    value["cash"] += 1
    snapshot.write_text(json.dumps(value))
    assert not cycle_is_complete(state, 4, require_manifest=True)


def test_completed_recovery_refuses_root_account_mismatch(tmp_path, monkeypatch):
    import futures_fund.reconcile_commit as commit

    state = tmp_path / "state"
    _stage(state)
    real_unlink = commit.durable_unlink

    def crash_before_intent_cleanup(_path):
        raise RuntimeError("power loss before transaction unlink")

    monkeypatch.setattr(commit, "durable_unlink", crash_before_intent_cleanup)
    with pytest.raises(RuntimeError, match="power loss"):
        recover_reconcile_transaction(state)
    account_path = state / "account.json"
    account = json.loads(account_path.read_text())
    account["cash"] += 10
    account_path.write_text(json.dumps(account))
    monkeypatch.setattr(commit, "durable_unlink", real_unlink)
    with pytest.raises(RuntimeError, match="account does not match"):
        recover_reconcile_transaction(state)


def test_completion_manifest_detects_mutated_durable_artifact(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    assert cycle_is_complete(state, 4, require_manifest=True)

    (state / "rebal" / "cycle" / "4" / "book.json").write_text('{"legs":[{"bad":1}]}')
    assert not cycle_is_complete(state, 4)
    assert not cycle_is_complete(state, 4, require_manifest=True)


def test_v4_generation_root_rejects_rehashed_forged_artifact(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    directory = state / "rebal" / "cycle" / "4"
    evidence = {"forged": True}
    (directory / "evidence.json").write_text(json.dumps(evidence))
    marker_path = directory / "complete.json"
    marker = json.loads(marker_path.read_text())
    marker["manifest"]["artifact_sha256"]["evidence"] = canonical_json_sha256(evidence)
    marker_path.write_text(json.dumps(marker))

    assert not cycle_is_complete(state, 4, require_manifest=True)


def test_v4_generation_root_rejects_removed_manifest_member(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    marker_path = state / "rebal" / "cycle" / "4" / "complete.json"
    marker = json.loads(marker_path.read_text())
    marker["manifest"]["artifact_sha256"].pop("evidence")
    marker_path.write_text(json.dumps(marker))

    assert not cycle_is_complete(state, 4, require_manifest=True)


def test_v4_generation_root_rejects_cross_cycle_swap_after_rehash(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    account = load_account(state, 0.0)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=5,
        cadence="rebal",
        account=account,
        artifacts={
            "book": {"legs": [{"symbol": "OTHER"}]},
            "evidence": [{"symbol": "OTHER"}],
            "report": {"cycle": 5},
        },
        equity_ts=NOW + timedelta(hours=1),
        equity=account.cash,
        ledger={"cycle": 5, "closing_equity": account.cash},
        runtime_provenance=_provenance(),
    )
    recover_reconcile_transaction(state)
    directories = [state / "rebal" / "cycle" / str(cycle) for cycle in (4, 5)]
    books = [json.loads((directory / "book.json").read_text()) for directory in directories]
    for directory, swapped in zip(directories, reversed(books), strict=True):
        (directory / "book.json").write_text(json.dumps(swapped))
        marker_path = directory / "complete.json"
        marker = json.loads(marker_path.read_text())
        marker["manifest"]["artifact_sha256"]["book"] = canonical_json_sha256(swapped)
        marker_path.write_text(json.dumps(marker))

    assert not cycle_is_complete(state, 4, require_manifest=True)
    assert not cycle_is_complete(state, 5, require_manifest=True)


def test_pending_v3_reconcile_recovers_and_first_v4_extends_its_event_chain(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    _downgrade_pending_reconcile_to_v3(state)
    recover_reconcile_transaction(state)
    assert cycle_is_complete(state, 4, require_manifest=True)
    marker = json.loads((state / "rebal" / "cycle" / "4" / "complete.json").read_text())
    assert marker["manifest"]["version"] == 3

    account = load_account(state, 0.0)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=5,
        cadence="rebal",
        account=account,
        artifacts={"book": {}, "evidence": [], "report": {"cycle": 5}},
        equity_ts=NOW + timedelta(hours=1),
        equity=account.cash,
        ledger={"cycle": 5},
        runtime_provenance=_provenance(),
    )
    recover_reconcile_transaction(state)

    assert [event["schema_version"] for event in load_account_events(state)] == [1, 2]
    assert cycle_is_complete(state, 5, require_manifest=True)


def test_historical_v2_reconcile_manifest_remains_readable(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    directory = state / "rebal" / "cycle" / "4"
    marker_path = directory / "complete.json"
    marker = json.loads(marker_path.read_text())
    marker["manifest"]["version"] = 2
    marker["manifest"]["artifact_sha256"].pop("account_event")
    marker["manifest"].pop("generation_root_sha256")
    marker_path.write_text(json.dumps(marker))
    (directory / "account_event.json").unlink()
    (state / "account-events.jsonl").unlink()

    assert cycle_is_complete(state, 4, require_manifest=True)


def test_file_added_after_commit_is_not_a_manifest_bound_artifact(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    directory = state / "rebal" / "cycle" / "4"
    (directory / "scoring_marks.json").write_text(
        json.dumps({"as_of_ts": NOW.isoformat(), "marks": {"A": 100.0}})
    )

    assert cycle_is_complete(state, 4, require_manifest=True)
    assert not completed_artifact_is_bound(state, 4, "scoring_marks")


def test_protocol_completion_marker_must_remain_paper_only(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    marker_path = state / "rebal" / "cycle" / "4" / "complete.json"
    marker = json.loads(marker_path.read_text())
    marker["paper_only"] = False
    marker_path.write_text(json.dumps(marker))

    assert not cycle_is_complete(state, 4, require_manifest=True)


def test_pre_manifest_protocol_cycle_can_be_auditably_backfilled(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    marker_path = state / "rebal" / "cycle" / "4" / "complete.json"
    marker = json.loads(marker_path.read_text())
    marker.pop("manifest")
    marker_path.write_text(json.dumps(marker))
    protocol_path = state / "reconcile-protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["manifest_required"] = False
    protocol_path.write_text(json.dumps(protocol))

    assert cycle_is_complete(state, 4)
    assert not cycle_is_complete(state, 4, require_manifest=True)
    assert backfill_completion_manifest(state, 4) is True
    assert cycle_is_complete(state, 4, require_manifest=True)
    assert backfill_completion_manifest(state, 4) is False
    finalize_manifest_migration(state)

    marker = json.loads(marker_path.read_text())
    marker.pop("manifest")
    marker_path.write_text(json.dumps(marker))
    assert not cycle_is_complete(state, 4)
    assert completed_cycle_numbers(state) == []
    with pytest.raises(ValueError, match="migration is already closed"):
        backfill_completion_manifest(state, 4)


def test_crash_mid_commit_is_recovered_without_duplicate_ledger(tmp_path, monkeypatch):
    import futures_fund.reconcile_commit as commit

    state = tmp_path / "state"
    _stage(state)
    real_save = commit.save_output

    def crash_on_report(state_dir, cycle, name, value, *, cadence):
        if name == "report":
            raise RuntimeError("injected crash")
        return real_save(state_dir, cycle, name, value, cadence=cadence)

    monkeypatch.setattr(commit, "save_output", crash_on_report)
    with pytest.raises(RuntimeError, match="injected crash"):
        recover_reconcile_transaction(state)
    assert transaction_path(state).exists()
    assert completed_cycle_numbers(state) == []

    monkeypatch.setattr(commit, "save_output", real_save)
    recover_reconcile_transaction(state)
    ledger = (state / "ledger.jsonl").read_text().strip().splitlines()
    assert len(ledger) == 1
    assert completed_cycle_numbers(state) == [4]


@pytest.mark.parametrize(
    ("target", "artifact"),
    [
        ("save_account", None),
        ("save_output", "book"),
        ("save_output", "account_event"),
        ("record_equity", None),
        ("append_ledger", None),
        ("append_account_event", None),
        ("save_output", "report"),
        ("save_output", "complete"),
    ],
)
def test_every_reconcile_persistence_boundary_is_replayable(
    tmp_path, monkeypatch, target, artifact
):
    import futures_fund.reconcile_commit as commit

    state = tmp_path / "state"
    _stage(state)
    original = getattr(commit, target)
    failed = False

    def injected(*args, **kwargs):
        nonlocal failed
        name = args[2] if target == "save_output" else None
        should_fail = not failed and (artifact is None or name == artifact)
        if should_fail:
            failed = True
            raise RuntimeError(f"injected {target}:{artifact}")
        return original(*args, **kwargs)

    monkeypatch.setattr(commit, target, injected)
    with pytest.raises(RuntimeError, match="injected"):
        recover_reconcile_transaction(state)
    assert transaction_path(state).exists()
    assert completed_cycle_numbers(state) == []

    monkeypatch.setattr(commit, target, original)
    recover_reconcile_transaction(state)
    assert completed_cycle_numbers(state) == [4]
    assert len((state / "ledger.jsonl").read_text().splitlines()) == 1


def test_reconcile_retains_intent_when_published_completion_fails_verification(
    tmp_path, monkeypatch
):
    import futures_fund.reconcile_commit as commit

    state = tmp_path / "state"
    _stage(state)
    real_save = commit.save_output

    def corrupt_complete(state_dir, cycle, name, value, *, cadence):
        if name == "complete":
            value = {**value, "paper_only": False}
        return real_save(state_dir, cycle, name, value, cadence=cadence)

    monkeypatch.setattr(commit, "save_output", corrupt_complete)
    with pytest.raises(RuntimeError, match="published reconcile completion failed verification"):
        recover_reconcile_transaction(state)
    assert transaction_path(state).exists()
    assert not cycle_is_complete(state, 4, require_manifest=True)

    monkeypatch.setattr(commit, "save_output", real_save)
    assert recover_reconcile_transaction(state)["recovered"] is True
    assert cycle_is_complete(state, 4, require_manifest=True)
    assert not transaction_path(state).exists()


def test_reconcile_post_publish_verification_rejects_wrong_commit_identity(
    tmp_path, monkeypatch
):
    import futures_fund.reconcile_commit as commit

    state = tmp_path / "state"
    _stage(state)
    real_save = commit.save_output

    def corrupt_commit_id(state_dir, cycle, name, value, *, cadence):
        if name == "complete":
            value = {**value, "commit_id": "wrong-commit"}
        return real_save(state_dir, cycle, name, value, cadence=cadence)

    monkeypatch.setattr(commit, "save_output", corrupt_commit_id)
    with pytest.raises(RuntimeError, match="published reconcile completion failed verification"):
        recover_reconcile_transaction(state)

    assert transaction_path(state).exists()
    assert not cycle_is_complete(state, 4, require_manifest=True)


def test_reconcile_recovery_requires_an_intact_intent_hash(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    path = transaction_path(state)
    raw = json.loads(path.read_text())
    raw.pop("intent_sha256")
    path.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="required intent hash"):
        recover_reconcile_transaction(state)
    assert not (state / "account.json").exists()


def test_reconcile_recovery_refuses_unrelated_account_lineage(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    (state / "account.json").write_text(json.dumps(PaperAccount(cash=19_900.0).to_dict()))

    with pytest.raises(RuntimeError, match="account lineage conflict"):
        recover_reconcile_transaction(state)
    assert load_account(state, 0.0).cash == 19_900.0


def test_reconcile_and_heartbeat_transactions_are_mutually_exclusive(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    account = PaperAccount(cash=20_000.0, last_funding_ts=NOW)
    record = {
        "ts": NOW.isoformat(),
        "kind": "token_free_funding_heartbeat",
        "paper_only": True,
    }
    with pytest.raises(HeartbeatError, match="reconcile transaction is unfinished"):
        stage_heartbeat_transaction(
            state,
            account,
            record,
            expected_base_account_sha256=None,
            runtime_provenance=_provenance(),
        )


def test_ambiguous_dual_intents_fail_before_account_mutation(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    (state / "heartbeat-transaction.json").write_text("{}")

    with pytest.raises(RuntimeError, match="both reconcile and heartbeat"):
        recover_reconcile_transaction(state)
    assert not (state / "account.json").exists()


def test_heartbeat_recovery_refuses_prior_reconcile_corrupted_after_staging(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    account = load_account(state, 0.0)
    baseline = (state / "account.json").read_text()
    later = NOW + timedelta(hours=8)
    record = {
        "ts": later.isoformat(),
        "schedule_slot": later.isoformat(),
        "kind": "token_free_funding_heartbeat",
        "paper_only": True,
    }
    stage_heartbeat_transaction(
        state,
        account,
        record,
        expected_base_account_sha256=current_account_sha256(state),
        runtime_provenance=_provenance(),
    )
    (state / "rebal" / "cycle" / "4" / "report.json").write_text("{}")

    with pytest.raises(HeartbeatError, match="prior account-event generation is not intact"):
        recover_heartbeat_transaction(state)

    assert (state / "heartbeat-transaction.json").exists()
    assert (state / "account.json").read_text() == baseline


def test_dropped_account_event_tail_cannot_fork_around_intact_generation(tmp_path):
    state = tmp_path / "state"
    _stage(state)
    recover_reconcile_transaction(state)
    account = load_account(state, 0.0)
    later = NOW + timedelta(hours=1)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=5,
        cadence="rebal",
        account=account,
        artifacts={"book": {}, "evidence": [], "report": {"cycle": 5}},
        equity_ts=later,
        equity=account.cash,
        ledger={"cycle": 5},
        runtime_provenance=_provenance(),
    )
    recover_reconcile_transaction(state)
    events_path = state / "account-events.jsonl"
    rows = events_path.read_text().splitlines()
    events_path.write_text(rows[0] + "\n")

    with pytest.raises(ValueError, match="orphan v3 generation"):
        stage_reconcile_transaction(
            state,
            expected_base_account_sha256=current_account_sha256(state),
            cycle=6,
            cadence="rebal",
            account=account,
            artifacts={"book": {}, "evidence": [], "report": {"cycle": 6}},
            equity_ts=later + timedelta(hours=1),
            equity=account.cash,
            ledger={"cycle": 6},
            runtime_provenance=_provenance(),
        )
