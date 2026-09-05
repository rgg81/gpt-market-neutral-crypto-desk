from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

import futures_fund.desk_health as desk_health
from futures_fund.account import PaperAccount
from futures_fund.desk_health import build_health_report
from futures_fund.durable_io import canonical_json_sha256
from futures_fund.heartbeat import recover_heartbeat_transaction, stage_heartbeat_transaction
from futures_fund.reconcile_commit import recover_reconcile_transaction, stage_reconcile_transaction
from futures_fund.state_transaction import (
    current_account_sha256,
    exclusive_state_transaction_lock,
)

T0 = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)
T1 = datetime(2026, 9, 5, 8, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def exact_proxy_is_live(monkeypatch):
    monkeypatch.setattr(
        desk_health,
        "_current_proxy_probe",
        lambda: {
            "status": "HEALTHY",
            "http_healthy": True,
            "identity_bound": True,
            "listener_owner_pids": [123],
        },
    )


def test_health_snapshot_waits_for_an_active_state_writer(tmp_path, monkeypatch):
    entered = threading.Event()
    started = threading.Event()

    def snapshot(*_args, **_kwargs):
        entered.set()
        return {"status": "HEALTHY"}

    monkeypatch.setattr(desk_health, "_build_health_report_unlocked", snapshot)

    def worker():
        started.set()
        return desk_health.build_health_report(tmp_path / "state", tmp_path / "logs")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with exclusive_state_transaction_lock(tmp_path / "state"):
            future = pool.submit(worker)
            assert started.wait(1.0)
            assert not entered.wait(0.1)
        assert future.result(timeout=1.0) == {"status": "HEALTHY"}


def test_health_of_absent_state_is_strictly_read_only(tmp_path):
    state = tmp_path / "never-created"

    report = build_health_report(
        state,
        tmp_path / "absent-logs",
        now=datetime(2026, 9, 5, 0, 0, tzinfo=UTC),
    )

    assert report["status"] == "CRITICAL"
    assert not state.exists()


def _provenance(ts):
    body = {"schema_version": 1, "captured_at": ts.isoformat(), "test": True}
    return {**body, "provenance_sha256": canonical_json_sha256(body)}


def _healthy_state(tmp_path):
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    account = PaperAccount(cash=20_000.0, last_funding_ts=T0)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=None,
        cycle=1,
        cadence="rebal",
        account=account,
        artifacts={
            "book": {"legs": []},
            "evidence": [],
            "report": {
                "cycle": 1,
                "ran_at": T0.isoformat(),
                "decision_ts": T0.isoformat(),
                "equity": 20_000.0,
                "n_legs": 0,
            },
        },
        equity_ts=T0,
        equity=20_000.0,
        ledger={"cycle": 1, "closing_equity": 20_000.0},
        runtime_provenance=_provenance(T0),
    )
    recover_reconcile_transaction(state)
    account.last_funding_ts = T1
    heartbeat = {
        "ts": T1.isoformat(),
        "schedule_slot": T1.isoformat(),
        "kind": "token_free_funding_heartbeat",
        "paper_only": True,
        "actions": "none",
        "positions": [],
        "equity": 20_000.0,
    }
    stage_heartbeat_transaction(
        state,
        account,
        heartbeat,
        expected_base_account_sha256=current_account_sha256(state),
        runtime_provenance=_provenance(T1),
    )
    recover_heartbeat_transaction(state)
    logs.mkdir()
    (logs / "binance-proxy-monitor.json").write_text(
        json.dumps(
            {
                "status": "HEALTHY",
                "checked_at": (T1 + timedelta(minutes=10)).timestamp(),
            }
        )
    )
    return state, logs


def test_health_report_verifies_deduplicated_state_and_latest_account(tmp_path):
    state, logs = _healthy_state(tmp_path)
    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))
    assert report["status"] == "HEALTHY"
    assert report["state"]["latest_completed_cycle"] == 1
    assert report["state"]["heartbeat_chain_valid"] is True
    assert report["state"]["latest_settlement_source"] == "heartbeat"
    assert report["state"]["account_manifest_consistent"] is True
    assert report["state"]["account_binding_source"] == "heartbeat_v4"
    assert report["state"]["deduplication"]["heartbeats"]["unique_rows"] == 1
    assert report["state"]["account_event_chain_valid"] is True
    assert report["state"]["account_event_count"] == 2
    assert report["state"]["directive_lifecycle"] == {
        "status": "idle",
        "queued_source_present": False,
    }


def test_health_report_surfaces_retryable_claim_and_queued_inbox_without_text(
    tmp_path, monkeypatch
):
    state, logs = _healthy_state(tmp_path)
    monkeypatch.setattr(
        desk_health,
        "directive_lifecycle_status",
        lambda _state: {
            "status": "claimed_pending",
            "cycle": 2,
            "claim_id": "a" * 32,
            "payload_state": "claim",
            "queued_source_present": True,
            "text": "must never appear in health output",
        },
    )

    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))

    assert report["status"] == "CAUTION"
    assert report["state"]["directive_lifecycle"] == {
        "status": "claimed_pending",
        "queued_source_present": True,
        "cycle": 2,
        "claim_id": "a" * 32,
        "payload_state": "claim",
    }
    codes = {issue["code"] for issue in report["issues"]}
    assert "DIRECTIVE_CLAIM_PENDING" in codes
    assert "DIRECTIVE_INBOX_QUEUED_BEHIND_ACTIVE" in codes
    assert "must never appear" not in json.dumps(report)


@pytest.mark.parametrize(
    "payload_state", ["tombstone", "duplicate_source", "missing", "conflict"]
)
def test_health_report_treats_nonretryable_claim_payload_state_as_critical(
    tmp_path, monkeypatch, payload_state
):
    state, logs = _healthy_state(tmp_path)
    monkeypatch.setattr(
        desk_health,
        "directive_lifecycle_status",
        lambda _state: {
            "status": "claimed_pending",
            "cycle": 2,
            "claim_id": "b" * 32,
            "payload_state": payload_state,
            "queued_source_present": False,
        },
    )

    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))

    assert report["status"] == "CRITICAL"
    assert any(issue["code"] == "DIRECTIVE_LIFECYCLE_CONFLICT" for issue in report["issues"])


def test_health_report_treats_directive_finalization_pending_as_critical(
    tmp_path, monkeypatch
):
    state, logs = _healthy_state(tmp_path)
    monkeypatch.setattr(
        desk_health,
        "directive_lifecycle_status",
        lambda _state: {
            "status": "cleanup_pending",
            "cycle": 2,
            "claim_id": "c" * 32,
            "payload_state": "tombstone",
            "queued_source_present": False,
        },
    )

    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))

    assert report["status"] == "CRITICAL"
    assert any(
        issue["code"] == "DIRECTIVE_FINALIZATION_PENDING" for issue in report["issues"]
    )


def test_health_report_redacts_directive_conflict_diagnostics(tmp_path, monkeypatch):
    state, logs = _healthy_state(tmp_path)
    monkeypatch.setattr(
        desk_health,
        "directive_lifecycle_status",
        lambda _state: {
            "status": "conflict",
            "queued_source_present": False,
            "error": "directive body secret-token",
        },
    )

    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))

    assert report["status"] == "CRITICAL"
    assert any(issue["code"] == "DIRECTIVE_LIFECYCLE_CONFLICT" for issue in report["issues"])
    serialized = json.dumps(report)
    assert "secret-token" not in serialized
    assert "read-only lifecycle validation failed" in serialized


def test_health_report_flags_stale_funding_flat_book_and_proxy_monitor(tmp_path):
    state, logs = _healthy_state(tmp_path)
    report = build_health_report(state, logs, now=T1 + timedelta(hours=80))
    codes = {issue["code"] for issue in report["issues"]}
    assert report["status"] == "CRITICAL"
    assert "FUNDING_CLOCK_STALE" in codes
    assert "FLAT_BOOK_PROLONGED" in codes
    assert "PROXY_MONITOR_INVALID" in codes


def test_current_proxy_failure_overrides_a_fresh_manager_receipt(tmp_path, monkeypatch):
    state, logs = _healthy_state(tmp_path)
    monkeypatch.setattr(
        desk_health,
        "_current_proxy_probe",
        lambda: {
            "status": "UNHEALTHY",
            "http_healthy": False,
            "identity_bound": False,
            "listener_owner_pids": [],
        },
    )

    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))

    assert report["status"] == "CRITICAL"
    assert report["slo"]["proxy_current"]["status"] == "CRITICAL"
    assert any(issue["code"] == "PROXY_CURRENT_UNHEALTHY" for issue in report["issues"])


def test_health_report_detects_conflicting_duplicate_heartbeat_identity(tmp_path):
    state, logs = _healthy_state(tmp_path)
    path = state / "portfolio-heartbeats.jsonl"
    row = json.loads(path.read_text())
    row["equity"] += 1
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))
    assert report["status"] == "CRITICAL"
    assert any(issue["code"] == "HEARTBEAT_IDENTITY_CONFLICT" for issue in report["issues"])


def test_health_report_flags_nonpositive_latest_portfolio_equity(tmp_path):
    state, logs = _healthy_state(tmp_path)
    path = state / "portfolio-heartbeats.jsonl"
    row = json.loads(path.read_text())
    row["equity"] = 0.0
    path.write_text(json.dumps(row) + "\n")

    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))

    assert report["status"] == "CRITICAL"
    assert any(issue["code"] == "PORTFOLIO_EQUITY_INVALID" for issue in report["issues"])


def test_health_report_rejects_nonstandard_nan_state_json(tmp_path):
    state, logs = _healthy_state(tmp_path)
    path = state / "portfolio-heartbeats.jsonl"
    row = json.loads(path.read_text())
    row["equity"] = float("nan")
    path.write_text(json.dumps(row) + "\n")

    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))

    assert report["status"] == "CRITICAL"
    assert any(issue["code"] == "MALFORMED_STATE" for issue in report["issues"])


def test_health_report_rejects_missing_account_event_chain(tmp_path):
    state, logs = _healthy_state(tmp_path)
    (state / "account-events.jsonl").unlink()
    report = build_health_report(state, logs, now=T1 + timedelta(hours=1))
    assert report["status"] == "CRITICAL"
    assert report["state"]["account_event_chain_valid"] is False
    assert any(issue["code"] == "ACCOUNT_EVENT_CHAIN_INVALID" for issue in report["issues"])
    assert 1 in report["state"]["invalid_published_cycles"]


def test_health_report_flags_future_clock_and_exact_duplicates(tmp_path):
    state, logs = _healthy_state(tmp_path)
    path = state / "portfolio-heartbeats.jsonl"
    with path.open("a") as handle:
        handle.write(path.read_text())
    report = build_health_report(state, logs, now=T0 - timedelta(hours=1))
    codes = {issue["code"] for issue in report["issues"]}
    assert report["status"] == "CRITICAL"
    assert "HEARTBEAT_EXACT_DUPLICATES" in codes
    assert report["slo"]["cycle"]["status"] == "CRITICAL"
    assert report["slo"]["heartbeat"]["status"] == "CRITICAL"


def test_fresh_full_cycle_prevents_expected_overnight_heartbeat_warning(tmp_path):
    state, logs = _healthy_state(tmp_path)
    now = T1 + timedelta(hours=16)
    account = PaperAccount(cash=20_000.0, last_funding_ts=now)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=2,
        cadence="rebal",
        account=account,
        artifacts={
            "book": {"legs": []},
            "evidence": [],
            "report": {
                "cycle": 2,
                "ran_at": now.isoformat(),
                "decision_ts": now.isoformat(),
                "equity": 20_000.0,
                "n_legs": 0,
            },
        },
        equity_ts=now,
        equity=20_000.0,
        ledger={"cycle": 2, "closing_equity": 20_000.0},
        runtime_provenance=_provenance(now),
    )
    recover_reconcile_transaction(state)
    report = build_health_report(state, logs, now=now + timedelta(hours=7))
    assert report["slo"]["heartbeat"]["status"] == "HEALTHY"
    assert report["state"]["latest_settlement_source"] == "full_cycle"
