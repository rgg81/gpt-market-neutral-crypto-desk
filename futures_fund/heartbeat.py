"""Durable token-free funding settlement and portfolio measurement for the PAPER desk."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from futures_fund.account import PaperAccount
from futures_fund.durable_io import (
    canonical_json_sha256,
    durable_unlink,
    durable_write_json,
    durable_write_text,
)
from futures_fund.runtime_provenance import default_runtime_provenance, verify_runtime_provenance
from futures_fund.state_transaction import (
    append_account_event,
    current_account_sha256,
    exclusive_state_transaction_lock,
    load_account_events,
    prepare_account_event,
    verify_account_event_references,
    verify_account_lineage,
)

HEARTBEAT_TRANSACTION_FILE = "heartbeat-transaction.json"
HEARTBEAT_GENERATIONS_DIR = "heartbeat-generations"
HEARTBEAT_SCHEMA_VERSION = 4
HASHED_HEARTBEAT_VERSIONS = {2, 3, HEARTBEAT_SCHEMA_VERSION}
UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION = 3
GENERATION_ROOT_SCHEMA_VERSION = 1
RECONCILE_TRANSACTION_FILE = "reconcile-transaction.json"

_HEARTBEAT_PROVENANCE_FIELDS = frozenset(
    {
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
    }
)


class HeartbeatError(ValueError):
    """The held paper book cannot be safely marked, settled, or committed."""


def _record_identity(record: dict) -> tuple[str, str]:
    identity = (record.get("schedule_slot") or record.get("ts"), record.get("kind"))
    if not all(identity):
        raise HeartbeatError("heartbeat record lacks ts/kind identity")
    return str(identity[0]), str(identity[1])


def _heartbeat_rows(state_dir: str | Path) -> list[dict]:
    """Parse the mixed historical portfolio ledger without upgrading old observations.

    Before scheduled heartbeats existed this file also held bare ``ts``/``equity`` observations.
    Those rows have no event identity, but they remain valid read-only performance history.
    Protocol consumers validate identities only for rows they own.
    """
    path = Path(state_dir) / "portfolio-heartbeats.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HeartbeatError(f"malformed heartbeat ledger {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise HeartbeatError(f"non-object heartbeat ledger row {path}:{line_number}")
        rows.append(row)
    return rows


def _record_sha256(record: dict) -> str:
    body = dict(record)
    body.pop("record_sha256", None)
    return canonical_json_sha256(body)


def _heartbeat_payload(record: dict) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key not in _HEARTBEAT_PROVENANCE_FIELDS
    }


def _heartbeat_generation_root_sha256(
    *,
    commit_id: str,
    payload: dict,
    account_state: dict,
    runtime_provenance: dict,
    previous_heartbeat_sha256: str | None,
) -> str:
    if not verify_runtime_provenance(runtime_provenance):
        raise HeartbeatError("generation root requires valid runtime provenance")
    return canonical_json_sha256(
        {
            "schema_version": GENERATION_ROOT_SCHEMA_VERSION,
            "kind": "heartbeat",
            "paper_only": True,
            "commit_id": commit_id,
            "identity": {
                "schedule_slot": payload.get("schedule_slot"),
                "ts": payload.get("ts"),
                "kind": payload.get("kind"),
            },
            "artifact_sha256": {
                "heartbeat_payload": canonical_json_sha256(payload),
                "account_state": canonical_json_sha256(account_state),
                "runtime_provenance": canonical_json_sha256(runtime_provenance),
            },
            "account_state_sha256": canonical_json_sha256(account_state),
            "runtime_provenance_sha256": canonical_json_sha256(runtime_provenance),
            "previous_heartbeat_sha256": previous_heartbeat_sha256,
        }
    )


def heartbeat_generation_dir(state_dir: str | Path, record: dict) -> Path:
    identity = _record_identity(record)
    generation = sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return Path(state_dir) / HEARTBEAT_GENERATIONS_DIR / generation


def save_account(state_dir: str | Path, account: PaperAccount) -> None:
    durable_write_json(Path(state_dir) / "account.json", account.to_dict())


def settle_funding_heartbeat(account: PaperAccount, evidence: list[dict], *, now: datetime) -> dict:
    """Settle one boundary snapshot and measure the unchanged book atomically."""
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    try:
        working = PaperAccount.from_dict(account.to_dict())
    except Exception as exc:  # noqa: BLE001 - normalize invalid mutable state at this boundary
        raise HeartbeatError(f"invalid PAPER account before heartbeat: {exc}") from exc
    if working.last_funding_ts is not None and now < working.last_funding_ts:
        raise HeartbeatError("heartbeat timestamp precedes the paper funding clock")
    if not all(isinstance(row, dict) for row in evidence):
        raise HeartbeatError("heartbeat evidence rows must be objects")
    symbols = [str(row.get("symbol", "")) for row in evidence]
    if any(not symbol for symbol in symbols):
        raise HeartbeatError("heartbeat evidence contains an empty symbol")
    if len(symbols) != len(set(symbols)):
        raise HeartbeatError("heartbeat evidence contains duplicate symbols")
    by_symbol = {row["symbol"]: row for row in evidence}
    missing = sorted(set(working.positions) - set(by_symbol))
    if missing:
        raise HeartbeatError(f"held symbols lack heartbeat evidence: {missing}")

    marks: dict[str, float] = {}
    rates: dict[str, float] = {}
    intervals: dict[str, int] = {}
    betas: dict[str, float] = {}
    for symbol in working.positions:
        row = by_symbol[symbol]
        try:
            mark_raw = row["mark"]
            rate_raw = row["funding_rate"]
            interval_value = row["funding_interval_h"]
            beta_raw = (
                row["beta_clamped"] if "beta_clamped" in row else row["beta_btc"]
            )
            if any(
                isinstance(value, bool)
                for value in (mark_raw, rate_raw, interval_value, beta_raw)
            ):
                raise TypeError("boolean is not a numeric observation")
            mark = float(mark_raw)
            rate = float(rate_raw)
            interval_raw = float(interval_value)
            beta = float(beta_raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise HeartbeatError(f"held symbol has incomplete numeric evidence: {symbol}") from exc
        interval = int(interval_raw) if math.isfinite(interval_raw) else 0
        if not math.isfinite(mark) or mark <= 0.0:
            raise HeartbeatError(f"held symbol has no finite positive mark: {symbol}")
        if not math.isfinite(rate):
            raise HeartbeatError(f"held symbol has a non-finite funding rate: {symbol}")
        if (
            not math.isfinite(interval_raw)
            or interval_raw != interval
            or interval not in {1, 2, 4, 8}
        ):
            raise HeartbeatError(f"held symbol has an invalid exact funding interval: {symbol}")
        if not math.isfinite(beta):
            raise HeartbeatError(f"held symbol has a non-finite beta: {symbol}")
        marks[symbol] = mark
        rates[symbol] = rate
        intervals[symbol] = interval
        betas[symbol] = beta
        if "funding_events" not in row or not isinstance(row["funding_events"], list):
            raise HeartbeatError(f"held symbol lacks exact funding history: {symbol}")

    previous_ts = working.last_funding_ts or now
    event_counts = {
        symbol: len(by_symbol[symbol]["funding_events"]) for symbol in working.positions
    }
    funding_before = working.funding_received - working.funding_paid
    try:
        working.settle_funding_events(
            {symbol: list(by_symbol[symbol]["funding_events"]) for symbol in working.positions},
            now=now,
            observed_intervals=intervals,
        )
    except ValueError as exc:
        raise HeartbeatError(str(exc)) from exc
    funding_after = working.funding_received - working.funding_paid
    long_usd = sum(p.qty * marks[s] for s, p in working.positions.items() if p.direction == "long")
    short_usd = sum(
        p.qty * marks[s] for s, p in working.positions.items() if p.direction == "short"
    )
    gross = long_usd + short_usd
    equity = working.equity(marks)
    beta_net = sum(
        (1.0 if p.direction == "long" else -1.0) * p.qty * marks[s] * betas[s]
        for s, p in working.positions.items()
    )
    numeric_outputs = {
        "funding_before": funding_before,
        "funding_after": funding_after,
        "longs_usd": long_usd,
        "shorts_usd": short_usd,
        "gross": gross,
        "equity": equity,
        "beta_net_usd": beta_net,
    }
    if not all(math.isfinite(float(value)) for value in numeric_outputs.values()):
        raise HeartbeatError("heartbeat calculation produced a non-finite portfolio metric")
    if equity <= 0.0:
        raise HeartbeatError(f"heartbeat PAPER equity is non-positive: {equity}")
    positions = [
        {
            "symbol": symbol,
            "side": position.direction,
            "qty": position.qty,
            "mark": marks[symbol],
            "notional": position.qty * marks[symbol],
            "beta_clamped": betas[symbol],
            "funding_rate": rates[symbol],
            "funding_interval_h": intervals[symbol],
            "funding_events": event_counts[symbol],
        }
        for symbol, position in working.positions.items()
    ]
    record = {
        "ts": now.isoformat(),
        "kind": "token_free_funding_heartbeat",
        "paper_only": True,
        "actions": "none",
        "previous_funding_ts": previous_ts.isoformat(),
        "elapsed_hours": (now - previous_ts).total_seconds() / 3600.0,
        "funding_settled": funding_after - funding_before,
        "funding_net_cumulative": funding_after,
        "equity": equity,
        "gross": gross,
        "deploy_frac": gross / equity,
        "longs_usd": long_usd,
        "shorts_usd": short_usd,
        "dollar_residual_frac": (abs(long_usd - short_usd) / gross) if gross > 0.0 else 0.0,
        "beta_net_usd": beta_net,
        "beta_residual": beta_net / equity,
        "positions": positions,
    }
    if not all(
        math.isfinite(float(record[field]))
        for field in (
            "elapsed_hours",
            "funding_settled",
            "funding_net_cumulative",
            "equity",
            "gross",
            "deploy_frac",
            "longs_usd",
            "shorts_usd",
            "dollar_residual_frac",
            "beta_net_usd",
            "beta_residual",
        )
    ):
        raise HeartbeatError("heartbeat record contains a non-finite portfolio metric")
    for field_name in PaperAccount.model_fields:
        setattr(account, field_name, getattr(working, field_name))
    return record


def verify_heartbeat_history(
    state_dir: str | Path,
    *,
    allow_pending_record: dict | None = None,
) -> None:
    """Verify the complete official heartbeat chain and every hashed completion.

    Recovery may allow one exact final row whose completion marker is the interrupted write still
    being replayed. No earlier missing/corrupt generation and no protocol downgrade is tolerated.
    """
    official = [
        row
        for row in _heartbeat_rows(state_dir)
        if row.get("kind") == "token_free_funding_heartbeat"
    ]
    official.sort(key=lambda row: (str(row.get("ts", "")), _record_identity(row)[0]))
    identities: set[tuple[str, str]] = set()
    hashed_seen = False
    previous: dict | None = None
    allowed_commit = allow_pending_record.get("commit_id") if allow_pending_record else None
    allowed_seen = False
    for index, row in enumerate(official):
        identity = _record_identity(row)
        if identity in identities:
            raise HeartbeatError(f"duplicate official heartbeat identity: {identity}")
        identities.add(identity)
        version = int(row.get("heartbeat_schema_version", 0))
        if version in HASHED_HEARTBEAT_VERSIONS:
            hashed_seen = True
            if row.get("record_sha256") != _record_sha256(row):
                raise HeartbeatError("heartbeat history contains a record hash mismatch")
            expected_previous = canonical_json_sha256(previous) if previous is not None else None
            if row.get("previous_heartbeat_sha256") != expected_previous:
                raise HeartbeatError("heartbeat history hash chain is broken")
            if row.get("commit_id") == allowed_commit:
                if row != allow_pending_record or index != len(official) - 1:
                    raise HeartbeatError("allowed pending heartbeat is not the exact final row")
                allowed_seen = True
            elif not verify_heartbeat_completion(state_dir, row):
                raise HeartbeatError(
                    f"heartbeat completion is missing or corrupt for {_record_identity(row)[0]}"
                )
        elif version in {0, 1}:
            if hashed_seen:
                raise HeartbeatError("legacy heartbeat appears after hashed heartbeat protocol")
        else:
            raise HeartbeatError(f"unsupported heartbeat history schema version: {version}")
        previous = row
    if allowed_commit is not None and any(
        row.get("commit_id") == allowed_commit for row in official
    ) and not allowed_seen:
        raise HeartbeatError("pending heartbeat allowance did not match the chain head")


def append_heartbeat(
    state_dir: str | Path,
    record: dict,
    *,
    allow_incomplete_replay: bool = False,
) -> None:
    """Durably insert one heartbeat, enforcing immutable slots and the v2 hash chain."""
    verify_heartbeat_history(
        state_dir,
        allow_pending_record=record if allow_incomplete_replay else None,
    )
    path = Path(state_dir) / "portfolio-heartbeats.jsonl"
    identity = _record_identity(record)
    rows: list[dict] = []
    for existing in _heartbeat_rows(state_dir):
        existing_identity = (
            (existing.get("schedule_slot") or existing.get("ts")),
            existing.get("kind"),
        )
        if all(existing_identity) and tuple(map(str, existing_identity)) == identity:
            if record.get("schedule_slot") and existing != record:
                raise HeartbeatError(
                    f"conflicting immutable heartbeat for schedule slot {record['schedule_slot']}"
                )
            if existing == record:
                return
            continue
        rows.append(existing)
    if int(record.get("heartbeat_schema_version", 0)) in HASHED_HEARTBEAT_VERSIONS:
        if record.get("record_sha256") != _record_sha256(record):
            raise HeartbeatError("heartbeat record hash mismatch")
        official = [row for row in rows if row.get("kind") == "token_free_funding_heartbeat"]
        official.sort(key=lambda row: (str(row.get("ts", "")), _record_identity(row)[0]))
        expected_previous = canonical_json_sha256(official[-1]) if official else None
        if record.get("previous_heartbeat_sha256") != expected_previous:
            raise HeartbeatError("heartbeat hash chain does not extend the latest official mark")
    rows.append(record)
    rows.sort(key=lambda row: (str(row.get("ts", "")), str(row.get("kind", ""))))
    durable_write_text(
        path,
        "".join(
            json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n" for row in rows
        ),
    )


def verify_heartbeat_completion(state_dir: str | Path, record: dict) -> bool:
    version = int(record.get("heartbeat_schema_version", 0))
    if version not in HASHED_HEARTBEAT_VERSIONS:
        return False
    if record.get("record_sha256") != _record_sha256(record):
        return False
    directory = heartbeat_generation_dir(state_dir, record)
    try:
        heartbeat_artifact = json.loads((directory / "heartbeat.json").read_text())
        account_state = json.loads((directory / "account_state.json").read_text())
        provenance = json.loads((directory / "runtime_provenance.json").read_text())
        complete = json.loads((directory / "complete.json").read_text())
        account_event = (
            json.loads((directory / "account_event.json").read_text())
            if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION
            else None
        )
        heartbeat_payload = (
            json.loads((directory / "heartbeat_payload.json").read_text())
            if version == HEARTBEAT_SCHEMA_VERSION
            else None
        )
    except (OSError, json.JSONDecodeError):
        return False
    manifest = complete.get("manifest") if isinstance(complete, dict) else None
    common_valid = bool(
        isinstance(manifest, dict)
        and complete.get("paper_only") is True
        and complete.get("commit_id") == record.get("commit_id")
        and heartbeat_artifact == record
        and canonical_json_sha256(record) == manifest.get("heartbeat_sha256")
        and canonical_json_sha256(account_state) == manifest.get("account_state_sha256")
        and canonical_json_sha256(provenance) == manifest.get("runtime_provenance_sha256")
        and verify_runtime_provenance(provenance)
        and record.get("account_state_sha256") == canonical_json_sha256(account_state)
        and record.get("runtime_provenance_sha256") == canonical_json_sha256(provenance)
        and record.get("previous_heartbeat_sha256") == manifest.get("previous_heartbeat_sha256")
    )
    if not common_valid or version == 2:
        return common_valid
    try:
        account_events = load_account_events(state_dir)
    except (OSError, TypeError, ValueError):
        return False
    event_valid = bool(
        isinstance(account_event, dict)
        and account_event in account_events
        and account_event.get("kind") == "heartbeat"
        and account_event.get("event_id") == record.get("commit_id")
        and account_event.get("event_sha256") == record.get("account_event_sha256")
        and account_event.get("event_sha256") == manifest.get("account_event_sha256")
        and account_event.get("base_account_sha256") == record.get("base_account_sha256")
        and account_event.get("previous_event_id")
        == record.get("previous_account_event_id")
        and account_event.get("previous_event_sha256")
        == record.get("previous_account_event_sha256")
        and account_event.get("account_state_sha256") == record.get("account_state_sha256")
    )
    if not event_valid or version == UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION:
        return event_valid
    if not isinstance(heartbeat_payload, dict):
        return False
    expected_artifact_sha256 = {
        "account_event": canonical_json_sha256(account_event),
        "account_state": canonical_json_sha256(account_state),
        "heartbeat": canonical_json_sha256(record),
        "heartbeat_payload": canonical_json_sha256(heartbeat_payload),
        "runtime_provenance": canonical_json_sha256(provenance),
    }
    try:
        expected_root = _heartbeat_generation_root_sha256(
            commit_id=str(record["commit_id"]),
            payload=heartbeat_payload,
            account_state=account_state,
            runtime_provenance=provenance,
            previous_heartbeat_sha256=record.get("previous_heartbeat_sha256"),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        isinstance(heartbeat_payload, dict)
        and heartbeat_payload == _heartbeat_payload(record)
        and int(complete.get("version", 0)) == HEARTBEAT_SCHEMA_VERSION
        and manifest.get("artifact_sha256") == expected_artifact_sha256
        and manifest.get("generation_root_sha256") == expected_root
        and record.get("generation_root_sha256") == expected_root
        and account_event.get("schema_version") == 2
        and account_event.get("generation_root_sha256") == expected_root
    )


def heartbeat_for_schedule_slot(state_dir: str | Path, slot: str) -> dict | None:
    for row in _heartbeat_rows(state_dir):
        if row.get("kind") == "token_free_funding_heartbeat" and row.get("schedule_slot") == slot:
            if (
                int(row.get("heartbeat_schema_version", 0)) in HASHED_HEARTBEAT_VERSIONS
                and not verify_heartbeat_completion(state_dir, row)
            ):
                raise HeartbeatError(
                    f"heartbeat completion is missing or corrupt for schedule slot {slot}"
                )
            return row
    return None


def _heartbeat_transaction_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / HEARTBEAT_TRANSACTION_FILE


def stage_heartbeat_transaction(
    state_dir: str | Path,
    account: PaperAccount,
    record: dict,
    *,
    expected_base_account_sha256: str | None,
    runtime_provenance: dict | None = None,
) -> dict:
    """Durably stage complete post-settlement state and hash-chain metadata."""
    with exclusive_state_transaction_lock(state_dir):
        if current_account_sha256(state_dir) != expected_base_account_sha256:
            raise HeartbeatError("heartbeat staging lost its optimistic account-state lock")
        return _stage_heartbeat_transaction_unlocked(
            state_dir,
            account,
            record,
            expected_base_account_sha256=expected_base_account_sha256,
            runtime_provenance=runtime_provenance,
        )


def _stage_heartbeat_transaction_unlocked(
    state_dir: str | Path,
    account: PaperAccount,
    record: dict,
    *,
    expected_base_account_sha256: str | None,
    runtime_provenance: dict | None = None,
) -> dict:
    path = _heartbeat_transaction_path(state_dir)
    if path.exists():
        raise HeartbeatError(f"unfinished heartbeat transaction already exists: {path}")
    reconcile_path = Path(state_dir) / RECONCILE_TRANSACTION_FILE
    if reconcile_path.exists():
        raise HeartbeatError(
            f"cannot stage heartbeat while reconcile transaction is unfinished: {reconcile_path}"
        )
    verify_heartbeat_history(state_dir)
    prior = [
        row
        for row in _heartbeat_rows(state_dir)
        if row.get("kind") == "token_free_funding_heartbeat"
    ]
    prior.sort(key=lambda row: (str(row.get("ts", "")), _record_identity(row)[0]))
    provenance = runtime_provenance or default_runtime_provenance(
        captured_at=datetime.fromisoformat(str(record["ts"]))
    )
    if not verify_runtime_provenance(provenance):
        raise HeartbeatError("invalid heartbeat runtime provenance")
    prepared = dict(record)
    if _HEARTBEAT_PROVENANCE_FIELDS.intersection(prepared):
        raise HeartbeatError("caller supplied reserved heartbeat provenance fields")
    commit_id = uuid4().hex
    base_account_sha256 = expected_base_account_sha256
    account_state = account.to_dict()
    previous_heartbeat_sha256 = canonical_json_sha256(prior[-1]) if prior else None
    heartbeat_payload = dict(prepared)
    generation_root_sha256 = _heartbeat_generation_root_sha256(
        commit_id=commit_id,
        payload=heartbeat_payload,
        account_state=account_state,
        runtime_provenance=provenance,
        previous_heartbeat_sha256=previous_heartbeat_sha256,
    )
    account_event = prepare_account_event(
        state_dir,
        event_id=commit_id,
        kind="heartbeat",
        event_ts=str(record["ts"]),
        base_account_sha256=base_account_sha256,
        account_state_sha256=canonical_json_sha256(account_state),
        generation_root_sha256=generation_root_sha256,
    )
    prepared.update(
        {
            "heartbeat_schema_version": HEARTBEAT_SCHEMA_VERSION,
            "commit_id": commit_id,
            "previous_heartbeat_sha256": previous_heartbeat_sha256,
            "account_state_sha256": canonical_json_sha256(account_state),
            "runtime_provenance_sha256": canonical_json_sha256(provenance),
            "account_event_sha256": account_event["event_sha256"],
            "base_account_sha256": base_account_sha256,
            "previous_account_event_id": account_event["previous_event_id"],
            "previous_account_event_sha256": account_event["previous_event_sha256"],
            "generation_root_sha256": generation_root_sha256,
        }
    )
    prepared["record_sha256"] = _record_sha256(prepared)
    transaction = {
        "version": HEARTBEAT_SCHEMA_VERSION,
        "paper_only": True,
        "commit_id": prepared["commit_id"],
        "base_account_sha256": base_account_sha256,
        "account": account_state,
        "record": prepared,
        "heartbeat_payload": heartbeat_payload,
        "runtime_provenance": provenance,
        "account_event": account_event,
        "generation_root_sha256": generation_root_sha256,
    }
    transaction["intent_sha256"] = canonical_json_sha256(transaction)
    durable_write_json(path, transaction)
    record.clear()
    record.update(prepared)
    return prepared


def recover_heartbeat_transaction(state_dir: str | Path) -> dict:
    """Replay an interrupted account+heartbeat publication idempotently."""
    with exclusive_state_transaction_lock(state_dir):
        return _recover_heartbeat_transaction_unlocked(state_dir)


def _recover_heartbeat_transaction_unlocked(state_dir: str | Path) -> dict:
    path = _heartbeat_transaction_path(state_dir)
    if not path.exists():
        return {"recovered": False}
    reconcile_path = Path(state_dir) / RECONCILE_TRANSACTION_FILE
    if reconcile_path.exists():
        raise HeartbeatError(
            "both heartbeat and reconcile transactions are pending; refusing ambiguous replay"
        )
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HeartbeatError(f"malformed heartbeat transaction: {path}") from exc
    if not isinstance(raw, dict):
        raise HeartbeatError(f"invalid heartbeat transaction: {path}")
    version = raw.get("version")
    if version not in {1, *HASHED_HEARTBEAT_VERSIONS} or raw.get("paper_only") is not True:
        raise HeartbeatError(f"invalid heartbeat transaction: {path}")
    if version not in {UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION, HEARTBEAT_SCHEMA_VERSION}:
        raise HeartbeatError(
            "legacy heartbeat transaction cannot be replayed safely; "
            "retain it for explicit audited recovery"
        )
    record = raw.get("record")
    if (
        not isinstance(record, dict)
        or record.get("paper_only") is not True
        or record.get("kind") != "token_free_funding_heartbeat"
        or not record.get("ts")
    ):
        raise HeartbeatError(f"invalid heartbeat record in transaction: {path}")
    if version in HASHED_HEARTBEAT_VERSIONS:
        body = dict(raw)
        expected_intent = body.pop("intent_sha256", None)
        if canonical_json_sha256(body) != expected_intent:
            raise HeartbeatError(f"heartbeat transaction hash mismatch: {path}")
        if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION and (
            "base_account_sha256" not in raw
            or (
                raw["base_account_sha256"] is not None
                and not isinstance(raw["base_account_sha256"], str)
            )
        ):
            raise HeartbeatError(f"heartbeat transaction lacks valid account lineage: {path}")
        provenance = raw.get("runtime_provenance")
        if not verify_runtime_provenance(provenance):
            raise HeartbeatError(f"invalid heartbeat provenance in transaction: {path}")
        if record.get("record_sha256") != _record_sha256(record):
            raise HeartbeatError(f"heartbeat record hash mismatch in transaction: {path}")
        if record.get("account_state_sha256") != canonical_json_sha256(raw.get("account")):
            raise HeartbeatError(f"heartbeat account hash mismatch in transaction: {path}")
        if record.get("runtime_provenance_sha256") != canonical_json_sha256(provenance):
            raise HeartbeatError(f"heartbeat provenance hash mismatch in transaction: {path}")
        if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION:
            account_event = raw.get("account_event")
            if (
                not isinstance(account_event, dict)
                or record.get("account_event_sha256") != account_event.get("event_sha256")
                or record.get("base_account_sha256") != account_event.get("base_account_sha256")
                or record.get("previous_account_event_id")
                != account_event.get("previous_event_id")
                or record.get("previous_account_event_sha256")
                != account_event.get("previous_event_sha256")
                or record.get("account_state_sha256")
                != account_event.get("account_state_sha256")
            ):
                raise HeartbeatError(f"heartbeat account-event binding mismatch: {path}")
        if version == HEARTBEAT_SCHEMA_VERSION:
            heartbeat_payload = raw.get("heartbeat_payload")
            if not isinstance(heartbeat_payload, dict):
                raise HeartbeatError(f"heartbeat transaction lacks its caller payload: {path}")
            expected_generation_root = _heartbeat_generation_root_sha256(
                commit_id=str(raw["commit_id"]),
                payload=heartbeat_payload,
                account_state=raw["account"],
                runtime_provenance=provenance,
                previous_heartbeat_sha256=record.get("previous_heartbeat_sha256"),
            )
            if (
                heartbeat_payload != _heartbeat_payload(record)
                or raw.get("generation_root_sha256") != expected_generation_root
                or record.get("generation_root_sha256") != expected_generation_root
                or account_event.get("generation_root_sha256") != expected_generation_root
            ):
                raise HeartbeatError(f"heartbeat generation-root mismatch: {path}")
        complete_path = heartbeat_generation_dir(state_dir, record) / "complete.json"
        verify_heartbeat_history(state_dir, allow_pending_record=record)
        if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION:
            try:
                verify_account_event_references(
                    state_dir,
                    allow_pending_event=raw["account_event"],
                )
            except (OSError, TypeError, ValueError) as exc:
                raise HeartbeatError(
                    f"heartbeat prior account-event generation is not intact: {exc}"
                ) from exc
        completion_intact = verify_heartbeat_completion(state_dir, record)
        if complete_path.exists():
            try:
                existing_complete = json.loads(complete_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise HeartbeatError("existing heartbeat completion is unreadable") from exc
            if (
                not isinstance(existing_complete, dict)
                or existing_complete.get("commit_id") != record.get("commit_id")
            ):
                raise HeartbeatError("existing heartbeat completion conflicts with durable intent")
        if completion_intact:
            try:
                persisted = json.loads((Path(state_dir) / "account.json").read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise HeartbeatError("completed heartbeat account is unreadable") from exc
            if canonical_json_sha256(persisted) != record["account_state_sha256"]:
                raise HeartbeatError("completed heartbeat account differs from durable intent")
            if record not in _heartbeat_rows(state_dir):
                raise HeartbeatError("completed heartbeat is absent from the heartbeat index")
            durable_unlink(path)
            return {"recovered": True, "ts": record["ts"], "already_complete": True}
    account = PaperAccount.from_dict(raw["account"])
    if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION:
        try:
            verify_account_lineage(
                state_dir,
                base_sha256=raw.get("base_account_sha256"),
                target_sha256=canonical_json_sha256(account.to_dict()),
                label="heartbeat recovery",
            )
        except RuntimeError as exc:
            raise HeartbeatError(str(exc)) from exc
    save_account(state_dir, account)
    if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION:
        directory = heartbeat_generation_dir(state_dir, record)
        durable_write_json(directory / "account_state.json", raw["account"])
        durable_write_json(directory / "runtime_provenance.json", raw["runtime_provenance"])
        durable_write_json(directory / "heartbeat.json", record)
        durable_write_json(directory / "account_event.json", raw["account_event"])
        if version == HEARTBEAT_SCHEMA_VERSION:
            durable_write_json(directory / "heartbeat_payload.json", raw["heartbeat_payload"])
    append_heartbeat(state_dir, record, allow_incomplete_replay=True)
    if version in HASHED_HEARTBEAT_VERSIONS:
        if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION:
            append_account_event(state_dir, raw["account_event"])
        durable_write_json(
            heartbeat_generation_dir(state_dir, record) / "complete.json",
            {
                "version": version,
                "paper_only": True,
                "commit_id": record["commit_id"],
                "completed_at": record["ts"],
                "manifest": {
                    "heartbeat_sha256": canonical_json_sha256(record),
                    "account_state_sha256": record["account_state_sha256"],
                    "runtime_provenance_sha256": record["runtime_provenance_sha256"],
                    "previous_heartbeat_sha256": record["previous_heartbeat_sha256"],
                    **(
                        {"account_event_sha256": record["account_event_sha256"]}
                        if version >= UNIFIED_ACCOUNT_EVENT_HEARTBEAT_VERSION
                        else {}
                    ),
                    **(
                        {
                            "generation_root_sha256": record["generation_root_sha256"],
                            "artifact_sha256": {
                                "account_event": canonical_json_sha256(raw["account_event"]),
                                "account_state": canonical_json_sha256(raw["account"]),
                                "heartbeat": canonical_json_sha256(record),
                                "heartbeat_payload": canonical_json_sha256(
                                    raw["heartbeat_payload"]
                                ),
                                "runtime_provenance": canonical_json_sha256(
                                    raw["runtime_provenance"]
                                ),
                            },
                        }
                        if version == HEARTBEAT_SCHEMA_VERSION
                        else {}
                    ),
                },
            },
        )
        if not verify_heartbeat_completion(state_dir, record):
            raise HeartbeatError("published heartbeat completion failed verification")
    try:
        persisted = json.loads((Path(state_dir) / "account.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HeartbeatError("published heartbeat account is unreadable") from exc
    expected_account_hash = (
        record.get("account_state_sha256")
        if version in HASHED_HEARTBEAT_VERSIONS
        else canonical_json_sha256(raw["account"])
    )
    if canonical_json_sha256(persisted) != expected_account_hash:
        raise HeartbeatError("published heartbeat account differs from durable intent")
    durable_unlink(path)
    return {"recovered": True, "ts": record["ts"], "already_complete": False}
