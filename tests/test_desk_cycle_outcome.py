from __future__ import annotations

from datetime import UTC, datetime, timedelta

from futures_fund.account import PaperAccount
from futures_fund.reconcile_commit import recover_reconcile_transaction, stage_reconcile_transaction
from futures_fund.state_transaction import current_account_sha256
from scripts.desk_cycle_outcome import attest_cycle_outcome

NOW = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)


def _commit(state, cycle: int, timestamp: datetime) -> None:
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=cycle,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0, last_funding_ts=timestamp),
        artifacts={
            "book": {"legs": []},
            "evidence": [],
            "report": {"cycle": cycle, "decision_ts": timestamp.isoformat()},
        },
        equity_ts=timestamp,
        equity=20_000.0,
        ledger={"cycle": cycle, "closing_equity": 20_000.0},
    )
    recover_reconcile_transaction(state)


def test_attestation_requires_a_new_manifest_complete_cycle(tmp_path):
    state = tmp_path / "state"
    _commit(state, 1, NOW)
    receipt, valid = attest_cycle_outcome(
        state, before_cycle=0, now=NOW + timedelta(minutes=1)
    )
    assert valid is True
    assert receipt["outcome"] == "COMPLETED"
    assert len(receipt["complete_marker_sha256"]) == 64


def test_attestation_reports_completed_cycle_with_retryable_directive_finalization(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    _commit(state, 1, NOW)
    monkeypatch.setattr(
        "scripts.desk_cycle_outcome.finalize_cycle_directive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unlink interrupted")),
    )

    receipt, valid = attest_cycle_outcome(
        state, before_cycle=0, now=NOW + timedelta(minutes=1)
    )

    assert valid is False
    assert receipt["outcome"] == "COMPLETED_DIRECTIVE_FINALIZATION_PENDING"
    assert receipt["after_cycle"] == 1
    assert receipt["error"] == "unlink interrupted"


def test_attestation_accepts_only_an_early_noop_without_a_new_cycle(tmp_path):
    state = tmp_path / "state"
    _commit(state, 1, NOW)
    early, valid = attest_cycle_outcome(
        state, before_cycle=1, now=NOW + timedelta(hours=1)
    )
    assert valid is True
    assert early["outcome"] == "EARLY_STAND_DOWN"

    due, valid = attest_cycle_outcome(
        state, before_cycle=1, now=NOW + timedelta(hours=24)
    )
    assert valid is False
    assert due["outcome"] == "UNATTESTED_EXIT_ZERO"


def test_attestation_rejects_advancing_more_than_exactly_one_cycle(tmp_path):
    state = tmp_path / "state"
    _commit(state, 1, NOW)
    _commit(state, 2, NOW + timedelta(hours=24))

    receipt, valid = attest_cycle_outcome(
        state,
        before_cycle=0,
        now=NOW + timedelta(hours=24, minutes=1),
    )

    assert valid is False
    assert receipt["outcome"] == "UNATTESTED_CYCLE_JUMP"
    assert receipt["before_cycle"] == 0
    assert receipt["after_cycle"] == 2
