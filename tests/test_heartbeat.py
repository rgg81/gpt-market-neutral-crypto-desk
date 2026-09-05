from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from futures_fund.account import PaperAccount, Position
from futures_fund.durable_io import canonical_json_sha256
from futures_fund.heartbeat import (
    HeartbeatError,
    append_heartbeat,
    heartbeat_for_schedule_slot,
    heartbeat_generation_dir,
    recover_heartbeat_transaction,
    settle_funding_heartbeat,
    stage_heartbeat_transaction,
    verify_heartbeat_completion,
)
from futures_fund.reconcile_commit import (
    recover_reconcile_transaction,
    stage_reconcile_transaction,
    transaction_path,
)
from futures_fund.state_transaction import current_account_sha256, load_account_events
from scripts.desk_heartbeat import _latest_betas

T0 = datetime(2026, 8, 10, 0, 7, tzinfo=UTC)
T1 = datetime(2026, 8, 10, 8, 7, tzinfo=UTC)
F1 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def _account() -> PaperAccount:
    return PaperAccount(
        cash=20_000.0,
        last_funding_ts=T0,
        positions={
            "A/USDT:USDT": Position(
                symbol="A/USDT:USDT",
                direction="long",
                qty=100.0,
                entry_price=10.0,
                opened_ts=T0,
            ),
            "B/USDT:USDT": Position(
                symbol="B/USDT:USDT",
                direction="short",
                qty=50.0,
                entry_price=20.0,
                opened_ts=T0,
            ),
        },
    )


def _evidence() -> list[dict]:
    return [
        {
            "symbol": "A/USDT:USDT",
            "mark": 10.0,
            "funding_rate": -0.0001,
            "funding_interval_h": 8,
            "beta_clamped": 1.2,
            "funding_events": [{"timestamp": F1, "rate": -0.0001, "mark": 10.0}],
        },
        {
            "symbol": "B/USDT:USDT",
            "mark": 20.0,
            "funding_rate": 0.0002,
            "funding_interval_h": 8,
            "beta_clamped": 0.8,
            "funding_events": [{"timestamp": F1, "rate": 0.0002, "mark": 20.0}],
        },
    ]


def _provenance(ts=T1):
    body = {"schema_version": 1, "captured_at": ts.isoformat(), "test": True}
    return {**body, "provenance_sha256": canonical_json_sha256(body)}


def _publish_v3_heartbeat(state):
    account = _account()
    record = settle_funding_heartbeat(account, _evidence(), now=T1)
    record["schedule_slot"] = T1.isoformat()
    stage_heartbeat_transaction(
        state,
        account,
        record,
        expected_base_account_sha256=None,
        runtime_provenance=_provenance(),
    )
    recover_heartbeat_transaction(state)
    return account, record


def _downgrade_pending_heartbeat_to_v3(state) -> None:
    path = state / "heartbeat-transaction.json"
    transaction = json.loads(path.read_text())
    event = transaction["account_event"]
    event.pop("event_sha256")
    event.pop("generation_root_sha256")
    event["schema_version"] = 1
    event["event_sha256"] = canonical_json_sha256(event)
    record = transaction["record"]
    record["heartbeat_schema_version"] = 3
    record.pop("generation_root_sha256")
    record["account_event_sha256"] = event["event_sha256"]
    record.pop("record_sha256")
    record["record_sha256"] = canonical_json_sha256(record)
    transaction["version"] = 3
    transaction.pop("heartbeat_payload")
    transaction.pop("generation_root_sha256")
    transaction.pop("intent_sha256")
    transaction["intent_sha256"] = canonical_json_sha256(transaction)
    path.write_text(json.dumps(transaction))


def test_heartbeat_settles_one_boundary_without_changing_positions():
    account = _account()
    before = {symbol: position.model_dump() for symbol, position in account.positions.items()}
    record = settle_funding_heartbeat(account, _evidence(), now=T1)

    # Long A receives $0.10 at negative funding; short B receives $0.20 at positive funding.
    assert record["funding_settled"] == pytest.approx(0.30)
    assert account.cash == pytest.approx(20_000.30)
    assert account.last_funding_ts == T1
    assert account.funding_intervals_observed == {
        "A/USDT:USDT": 8,
        "B/USDT:USDT": 8,
    }
    assert {symbol: position.qty for symbol, position in account.positions.items()} == {
        symbol: position["qty"] for symbol, position in before.items()
    }
    assert record["actions"] == "none"
    assert record["paper_only"] is True
    assert record["gross"] == pytest.approx(2_000.0)
    assert record["dollar_residual_frac"] == 0.0
    assert record["beta_net_usd"] == pytest.approx(400.0)
    assert record["beta_residual"] == pytest.approx(400.0 / 20_000.30)
    assert {row["funding_events"] for row in record["positions"]} == {1}


def test_heartbeat_missing_held_mark_fails_before_mutation():
    account = _account()
    snapshot = account.model_dump()
    with pytest.raises(HeartbeatError, match="lack heartbeat evidence"):
        settle_funding_heartbeat(account, _evidence()[:1], now=T1)
    assert account.model_dump() == snapshot


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("mark", float("nan"), "finite positive mark"),
        ("mark", float("inf"), "finite positive mark"),
        ("mark", 0.0, "finite positive mark"),
        ("funding_rate", float("nan"), "non-finite funding rate"),
        ("beta_clamped", float("inf"), "non-finite beta"),
        ("funding_interval_h", float("nan"), "invalid exact funding interval"),
        ("funding_interval_h", 4.5, "invalid exact funding interval"),
        ("funding_interval_h", 3, "invalid exact funding interval"),
    ],
)
def test_heartbeat_rejects_invalid_numeric_evidence_before_mutation(field, value, message):
    account = _account()
    snapshot = account.model_dump()
    evidence = _evidence()
    evidence[0][field] = value

    with pytest.raises(HeartbeatError, match=message):
        settle_funding_heartbeat(account, evidence, now=T1)

    assert account.model_dump() == snapshot


def test_heartbeat_rejects_nonpositive_equity_without_advancing_funding_clock():
    account = PaperAccount(
        cash=1.0,
        last_funding_ts=T0,
        positions={
            "A/USDT:USDT": Position(
                symbol="A/USDT:USDT",
                direction="long",
                qty=100.0,
                entry_price=10.0,
                opened_ts=T0,
            )
        },
    )
    snapshot = account.model_dump()
    evidence = [
        {
            "symbol": "A/USDT:USDT",
            "mark": 1.0,
            "funding_rate": 0.0,
            "funding_interval_h": 8,
            "beta_clamped": 1.0,
            "funding_events": [{"timestamp": F1, "rate": 0.0, "mark": 1.0}],
        }
    ]

    with pytest.raises(HeartbeatError, match="equity is non-positive"):
        settle_funding_heartbeat(account, evidence, now=T1)

    assert account.model_dump() == snapshot


def test_append_heartbeat_refuses_nonstandard_nan_json(tmp_path):
    with pytest.raises(ValueError, match="Out of range float values"):
        append_heartbeat(
            tmp_path,
            {"ts": T1.isoformat(), "kind": "test", "equity": float("nan")},
        )
    assert not (tmp_path / "portfolio-heartbeats.jsonl").exists()


def test_append_heartbeat_uses_separate_jsonl(tmp_path):
    append_heartbeat(tmp_path, {"ts": T1.isoformat(), "kind": "test"})
    path = tmp_path / "portfolio-heartbeats.jsonl"
    assert json.loads(path.read_text()) == {"ts": T1.isoformat(), "kind": "test"}
    assert not (tmp_path / "ledger.jsonl").exists()


def test_append_heartbeat_is_idempotent_by_timestamp_and_kind(tmp_path):
    append_heartbeat(tmp_path, {"ts": T1.isoformat(), "kind": "test", "equity": 1.0})
    append_heartbeat(tmp_path, {"ts": T1.isoformat(), "kind": "test", "equity": 2.0})
    rows = (tmp_path / "portfolio-heartbeats.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["equity"] == 2.0


def test_append_heartbeat_preserves_legacy_identity_free_observations(tmp_path):
    path = tmp_path / "portfolio-heartbeats.jsonl"
    legacy = {"ts": T0.isoformat(), "equity": 19_900.0}
    path.write_text(json.dumps(legacy) + "\n")

    append_heartbeat(tmp_path, {"ts": T1.isoformat(), "kind": "test", "equity": 20_000.0})

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert legacy in rows
    assert {"ts": T1.isoformat(), "kind": "test", "equity": 20_000.0} in rows


def test_scheduled_heartbeat_is_idempotent_by_claimed_slot(tmp_path):
    slot = "2026-08-10T08:07:00+00:00"
    first = {
        "ts": T1.isoformat(),
        "schedule_slot": slot,
        "kind": "token_free_funding_heartbeat",
        "funding_settled": 1.0,
    }
    retry = {
        "ts": datetime(2026, 8, 10, 8, 19, tzinfo=UTC).isoformat(),
        "schedule_slot": slot,
        "kind": "token_free_funding_heartbeat",
        "funding_settled": 0.0,
    }
    append_heartbeat(tmp_path, first)
    assert heartbeat_for_schedule_slot(tmp_path, slot) == first
    # A scheduled slot is an immutable official mark; only an exact transaction replay is legal.
    with pytest.raises(HeartbeatError, match="conflicting immutable heartbeat"):
        append_heartbeat(tmp_path, retry)
    rows = (tmp_path / "portfolio-heartbeats.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0]) == first
    append_heartbeat(tmp_path, first)
    assert len((tmp_path / "portfolio-heartbeats.jsonl").read_text().splitlines()) == 1


def test_heartbeat_transaction_recovers_account_and_audit_together(tmp_path, monkeypatch):
    import futures_fund.heartbeat as heartbeat

    account = _account()
    record = settle_funding_heartbeat(account, _evidence(), now=T1)
    stage_heartbeat_transaction(
        tmp_path,
        account,
        record,
        expected_base_account_sha256=None,
        runtime_provenance=_provenance(),
    )
    real_append = heartbeat.append_heartbeat

    def crash(*_args, **_kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(heartbeat, "append_heartbeat", crash)
    with pytest.raises(RuntimeError, match="injected"):
        recover_heartbeat_transaction(tmp_path)
    monkeypatch.setattr(heartbeat, "append_heartbeat", real_append)
    assert recover_heartbeat_transaction(tmp_path)["recovered"] is True
    assert len((tmp_path / "portfolio-heartbeats.jsonl").read_text().splitlines()) == 1
    assert verify_heartbeat_completion(tmp_path, record)


def test_v4_heartbeat_root_rejects_rehashed_forged_payload(tmp_path):
    account, record = _publish_v3_heartbeat(tmp_path)
    del account
    directory = heartbeat_generation_dir(tmp_path, record)
    payload_path = directory / "heartbeat_payload.json"
    payload = json.loads(payload_path.read_text())
    payload["equity"] = float(payload["equity"]) + 10.0
    payload_path.write_text(json.dumps(payload))
    complete_path = directory / "complete.json"
    complete = json.loads(complete_path.read_text())
    complete["manifest"]["artifact_sha256"]["heartbeat_payload"] = canonical_json_sha256(
        payload
    )
    complete_path.write_text(json.dumps(complete))

    assert not verify_heartbeat_completion(tmp_path, record)


def test_v4_heartbeat_root_rejects_removed_manifest_member(tmp_path):
    account, record = _publish_v3_heartbeat(tmp_path)
    del account
    complete_path = heartbeat_generation_dir(tmp_path, record) / "complete.json"
    complete = json.loads(complete_path.read_text())
    complete["manifest"]["artifact_sha256"].pop("heartbeat_payload")
    complete_path.write_text(json.dumps(complete))

    assert not verify_heartbeat_completion(tmp_path, record)


def test_pending_v3_heartbeat_recovers_and_first_v4_extends_its_event_chain(tmp_path):
    account = _account()
    first = settle_funding_heartbeat(account, _evidence(), now=T1)
    first["schedule_slot"] = T1.isoformat()
    stage_heartbeat_transaction(
        tmp_path,
        account,
        first,
        expected_base_account_sha256=None,
        runtime_provenance=_provenance(),
    )
    _downgrade_pending_heartbeat_to_v3(tmp_path)
    recover_heartbeat_transaction(tmp_path)
    historical = json.loads((tmp_path / "portfolio-heartbeats.jsonl").read_text())
    assert historical["heartbeat_schema_version"] == 3
    assert verify_heartbeat_completion(tmp_path, historical)

    later = datetime(2026, 8, 10, 16, 7, tzinfo=UTC)
    second = {
        "ts": later.isoformat(),
        "schedule_slot": later.isoformat(),
        "kind": "token_free_funding_heartbeat",
        "paper_only": True,
    }
    stage_heartbeat_transaction(
        tmp_path,
        account,
        second,
        expected_base_account_sha256=current_account_sha256(tmp_path),
        runtime_provenance=_provenance(later),
    )
    recover_heartbeat_transaction(tmp_path)

    assert [event["schema_version"] for event in load_account_events(tmp_path)] == [1, 2]
    assert verify_heartbeat_completion(tmp_path, second)


def test_historical_v2_heartbeat_generation_remains_readable(tmp_path):
    account, record = _publish_v3_heartbeat(tmp_path)
    del account
    directory = heartbeat_generation_dir(tmp_path, record)
    historical = dict(record)
    for field in (
        "account_event_sha256",
        "base_account_sha256",
        "previous_account_event_id",
        "previous_account_event_sha256",
        "generation_root_sha256",
        "record_sha256",
    ):
        historical.pop(field)
    historical["heartbeat_schema_version"] = 2
    historical["record_sha256"] = canonical_json_sha256(historical)
    (directory / "heartbeat.json").write_text(json.dumps(historical))
    (tmp_path / "portfolio-heartbeats.jsonl").write_text(json.dumps(historical) + "\n")
    complete_path = directory / "complete.json"
    complete = json.loads(complete_path.read_text())
    complete["version"] = 2
    complete["manifest"]["heartbeat_sha256"] = canonical_json_sha256(historical)
    for field in ("account_event_sha256", "generation_root_sha256", "artifact_sha256"):
        complete["manifest"].pop(field)
    complete_path.write_text(json.dumps(complete))
    (directory / "account_event.json").unlink()
    (directory / "heartbeat_payload.json").unlink()
    (tmp_path / "account-events.jsonl").unlink()

    assert verify_heartbeat_completion(tmp_path, historical)


def test_heartbeat_retains_intent_and_repairs_its_failed_completion(tmp_path, monkeypatch):
    import futures_fund.heartbeat as heartbeat

    account = _account()
    record = settle_funding_heartbeat(account, _evidence(), now=T1)
    record["schedule_slot"] = T1.isoformat()
    stage_heartbeat_transaction(
        tmp_path,
        account,
        record,
        expected_base_account_sha256=None,
        runtime_provenance=_provenance(),
    )
    real_write = heartbeat.durable_write_json

    def corrupt_completion(path, value):
        if path.name == "complete.json":
            value = {**value, "paper_only": False}
        return real_write(path, value)

    monkeypatch.setattr(heartbeat, "durable_write_json", corrupt_completion)
    with pytest.raises(HeartbeatError, match="published heartbeat completion failed verification"):
        recover_heartbeat_transaction(tmp_path)
    assert (tmp_path / "heartbeat-transaction.json").exists()

    monkeypatch.setattr(heartbeat, "durable_write_json", real_write)
    assert recover_heartbeat_transaction(tmp_path)["recovered"] is True
    assert verify_heartbeat_completion(tmp_path, record)
    assert not (tmp_path / "heartbeat-transaction.json").exists()


@pytest.mark.parametrize("legacy_version", [1, 2])
def test_legacy_heartbeat_intent_halts_before_any_state_mutation(tmp_path, legacy_version):
    account_path = tmp_path / "account.json"
    account_path.write_text(json.dumps(PaperAccount(cash=21_000.0).to_dict()))
    baseline = account_path.read_bytes()
    (tmp_path / "heartbeat-transaction.json").write_text(
        json.dumps({"version": legacy_version, "paper_only": True})
    )

    with pytest.raises(HeartbeatError, match="cannot be replayed safely"):
        recover_heartbeat_transaction(tmp_path)

    assert account_path.read_bytes() == baseline
    assert not (tmp_path / "portfolio-heartbeats.jsonl").exists()
    assert not (tmp_path / "account-events.jsonl").exists()


def test_heartbeat_generations_form_a_verified_hash_chain(tmp_path):
    account = _account()
    first = settle_funding_heartbeat(account, _evidence(), now=T1)
    first["schedule_slot"] = T1.isoformat()
    stage_heartbeat_transaction(
        tmp_path,
        account,
        first,
        expected_base_account_sha256=None,
        runtime_provenance=_provenance(),
    )
    recover_heartbeat_transaction(tmp_path)

    t2 = datetime(2026, 8, 10, 16, 7, tzinfo=UTC)
    second = {
        **first,
        "ts": t2.isoformat(),
        "schedule_slot": t2.isoformat(),
        "elapsed_hours": 8.0,
    }
    for field in (
        "heartbeat_schema_version",
        "commit_id",
        "previous_heartbeat_sha256",
        "account_state_sha256",
        "runtime_provenance_sha256",
        "account_event_sha256",
        "base_account_sha256",
        "previous_account_event_id",
            "previous_account_event_sha256",
            "generation_root_sha256",
            "record_sha256",
    ):
        second.pop(field, None)
    stage_heartbeat_transaction(
        tmp_path,
        account,
        second,
        expected_base_account_sha256=current_account_sha256(tmp_path),
        runtime_provenance=_provenance(t2),
    )
    recover_heartbeat_transaction(tmp_path)
    assert second["previous_heartbeat_sha256"] == canonical_json_sha256(first)
    assert verify_heartbeat_completion(tmp_path, first)
    assert verify_heartbeat_completion(tmp_path, second)


def test_scheduled_lookup_rejects_tampered_v2_completion(tmp_path):
    account = _account()
    record = settle_funding_heartbeat(account, _evidence(), now=T1)
    record["schedule_slot"] = T1.isoformat()
    stage_heartbeat_transaction(
        tmp_path,
        account,
        record,
        expected_base_account_sha256=None,
        runtime_provenance=_provenance(),
    )
    recover_heartbeat_transaction(tmp_path)
    (heartbeat_generation_dir(tmp_path, record) / "heartbeat.json").write_text("{}")
    with pytest.raises(HeartbeatError, match="completion is missing or corrupt"):
        heartbeat_for_schedule_slot(tmp_path, T1.isoformat())


def test_heartbeat_uses_each_historical_rate_and_mark_not_latest_rate():
    account = _account()
    t2 = datetime(2026, 8, 10, 16, 7, tzinfo=UTC)
    evidence = _evidence()
    evidence[0]["funding_rate"] = 0.009  # deliberately unrelated current rate
    evidence[0]["funding_events"] = [
        {"timestamp": F1, "rate": -0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 10, 16, 0, tzinfo=UTC), "rate": 0.0002, "mark": 12.0},
    ]
    evidence[1]["funding_events"] = [
        {"timestamp": F1, "rate": 0.0002, "mark": 20.0},
        {"timestamp": datetime(2026, 8, 10, 16, 0, tzinfo=UTC), "rate": -0.0001, "mark": 18.0},
    ]
    record = settle_funding_heartbeat(account, evidence, now=t2)
    # A: +0.10 then pays 0.24. B short: +0.20 then pays 0.09.
    assert record["funding_settled"] == pytest.approx(-0.03)
    assert {row["funding_events"] for row in record["positions"]} == {2}


def test_heartbeat_refuses_fabricated_default_beta_for_a_held_book(tmp_path):
    assert _latest_betas(str(tmp_path), set()) == {}
    with pytest.raises(ValueError, match="no completed evidence snapshot"):
        _latest_betas(str(tmp_path), {"A/USDT:USDT"})


def test_heartbeat_beta_ignores_incomplete_cycle_artifact(tmp_path):
    state = tmp_path / "state"
    complete = state / "rebal" / "cycle" / "1"
    incomplete = state / "rebal" / "cycle" / "2"
    complete.mkdir(parents=True)
    incomplete.mkdir(parents=True)
    symbol = "A/USDT:USDT"
    (complete / "report.json").write_text('{"cycle": 1}')
    (complete / "evidence.json").write_text(json.dumps([{"symbol": symbol, "beta_clamped": 1.1}]))
    (incomplete / "evidence.json").write_text(json.dumps([{"symbol": symbol, "beta_clamped": 9.9}]))
    assert _latest_betas(str(state), {symbol}) == {symbol: 1.1}


def test_heartbeat_recovery_refuses_unrelated_account_lineage(tmp_path):
    account = _account()
    record = settle_funding_heartbeat(account, _evidence(), now=T1)
    stage_heartbeat_transaction(
        tmp_path,
        account,
        record,
        expected_base_account_sha256=None,
        runtime_provenance=_provenance(),
    )
    (tmp_path / "account.json").write_text(json.dumps(PaperAccount(cash=19_900.0).to_dict()))

    with pytest.raises(HeartbeatError, match="account lineage conflict"):
        recover_heartbeat_transaction(tmp_path)
    assert json.loads((tmp_path / "account.json").read_text())["cash"] == 19_900.0


def test_stale_heartbeat_computation_cannot_stage_over_newer_account(tmp_path):
    base = _account()
    (tmp_path / "account.json").write_text(json.dumps(base.to_dict()))
    stale_hash = current_account_sha256(tmp_path)
    (tmp_path / "account.json").write_text(json.dumps(PaperAccount(cash=20_500.0).to_dict()))
    record = settle_funding_heartbeat(base, _evidence(), now=T1)

    with pytest.raises(HeartbeatError, match="optimistic account-state lock"):
        stage_heartbeat_transaction(
            tmp_path,
            base,
            record,
            expected_base_account_sha256=stale_hash,
            runtime_provenance=_provenance(),
        )
    assert json.loads((tmp_path / "account.json").read_text())["cash"] == 20_500.0


def test_missing_prior_heartbeat_completion_blocks_next_heartbeat_before_staging(tmp_path):
    account, first = _publish_v3_heartbeat(tmp_path)
    complete = heartbeat_generation_dir(tmp_path, first) / "complete.json"
    complete.unlink()
    later = datetime(2026, 8, 10, 16, 7, tzinfo=UTC)
    second = {
        "ts": later.isoformat(),
        "schedule_slot": later.isoformat(),
        "kind": "token_free_funding_heartbeat",
        "paper_only": True,
    }

    with pytest.raises(HeartbeatError, match="completion is missing or corrupt"):
        stage_heartbeat_transaction(
            tmp_path,
            account,
            second,
            expected_base_account_sha256=current_account_sha256(tmp_path),
            runtime_provenance=_provenance(later),
        )

    assert not (tmp_path / "heartbeat-transaction.json").exists()


def test_reconcile_recovery_refuses_prior_heartbeat_deleted_after_staging(tmp_path):
    account, first = _publish_v3_heartbeat(tmp_path)
    baseline = (tmp_path / "account.json").read_text()
    later = datetime(2026, 8, 10, 16, 7, tzinfo=UTC)
    stage_reconcile_transaction(
        tmp_path,
        expected_base_account_sha256=current_account_sha256(tmp_path),
        cycle=4,
        cadence="rebal",
        account=account,
        artifacts={"book": {}, "evidence": [], "report": {"cycle": 4}},
        equity_ts=later,
        equity=account.cash,
        ledger={"cycle": 4},
        runtime_provenance=_provenance(later),
    )
    (heartbeat_generation_dir(tmp_path, first) / "complete.json").unlink()

    with pytest.raises(ValueError, match="heartbeat account event .* not intact"):
        recover_reconcile_transaction(tmp_path)

    assert transaction_path(tmp_path).exists()
    assert (tmp_path / "account.json").read_text() == baseline
