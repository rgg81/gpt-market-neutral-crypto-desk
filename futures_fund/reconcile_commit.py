"""Durable, replayable commit protocol for one PAPER reconcile.

No collection of ordinary files can be renamed atomically as a group.  The desk therefore writes
one durable intent containing the complete post-reconcile state and explicit one-shot-directive
expectation, replays every write idempotently, and publishes ``complete.json`` last. Heartbeats and
new cycles recover an unfinished intent before touching the account, so a crash cannot strand
fills/funding outside their audit chain or publish around a concurrently claimed instruction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from futures_fund.account import PaperAccount
from futures_fund.cycle_io import cycle_dir
from futures_fund.durable_io import (
    canonical_json_sha256,
    durable_unlink,
    durable_write_json,
    durable_write_text,
)
from futures_fund.runtime_provenance import (
    default_runtime_provenance,
    verify_runtime_provenance,
)
from futures_fund.state_transaction import (
    append_account_event,
    current_account_sha256,
    exclusive_state_transaction_lock,
    load_account_events,
    prepare_account_event,
    verify_account_event_references,
    verify_account_lineage,
)

PROTOCOL_FILE = "reconcile-protocol.json"
TRANSACTION_FILE = "reconcile-transaction.json"
HEARTBEAT_TRANSACTION_FILE = "heartbeat-transaction.json"
COMPLETE_ARTIFACT = "complete"
PROTOCOL_VERSION = 1
RECONCILE_TRANSACTION_VERSION = 2
SUPPORTED_RECONCILE_TRANSACTION_VERSIONS = {1, RECONCILE_TRANSACTION_VERSION}
MANIFEST_VERSION = 4
SUPPORTED_MANIFEST_VERSIONS = {1, 2, 3, MANIFEST_VERSION}
GENERATION_ROOT_SCHEMA_VERSION = 1


def _atomic_json(path: Path, value: object) -> None:
    durable_write_json(path, value)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _canonical_sha256(value: object) -> str:
    return canonical_json_sha256(value)


def _reconcile_generation_root_sha256(
    *,
    commit_id: str,
    cycle: int,
    cadence: str,
    artifacts: dict[str, object],
    ledger: dict,
    equity_row: dict,
) -> str:
    """Bind a complete pre-event generation without introducing an event-hash cycle."""
    account_state = artifacts.get("account_state")
    runtime_provenance = artifacts.get("runtime_provenance")
    if not isinstance(account_state, dict) or not verify_runtime_provenance(runtime_provenance):
        raise ValueError("generation root requires account state and runtime provenance")
    return _canonical_sha256(
        {
            "schema_version": GENERATION_ROOT_SCHEMA_VERSION,
            "kind": "reconcile",
            "paper_only": True,
            "commit_id": commit_id,
            "cycle": int(cycle),
            "cadence": cadence,
            "artifact_sha256": {
                name: _canonical_sha256(value) for name, value in sorted(artifacts.items())
            },
            "ledger_sha256": _canonical_sha256(ledger),
            "equity_sha256": _canonical_sha256(equity_row),
            "account_state_sha256": _canonical_sha256(account_state),
            "runtime_provenance_sha256": _canonical_sha256(runtime_provenance),
        }
    )


def save_account(state_dir, account: PaperAccount) -> None:
    """Durably publish the root PAPER account (local wrapper kept as a test seam)."""
    durable_write_json(Path(state_dir) / "account.json", account.to_dict())


def save_output(state_dir, cycle: int, name: str, value: object, *, cadence: str) -> Path:
    """Durably publish one cycle artifact (local wrapper kept as a test seam)."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"invalid cycle artifact name: {name!r}")
    path = cycle_dir(state_dir, cycle, cadence=cadence) / f"{name}.json"
    return durable_write_json(path, value)


def _jsonl_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSONL row {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"non-object JSONL row {path}:{line_number}")
        rows.append(row)
    return rows


def append_ledger(state_dir, record: dict) -> None:
    """Durably insert the immutable per-cycle ledger row."""
    path = Path(state_dir) / "ledger.jsonl"
    try:
        record_cycle = int(record["cycle"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ledger record lacks a valid integer cycle") from exc
    rows: list[dict] = []
    seen: set[int] = set()
    for line_number, row in enumerate(_jsonl_rows(path), start=1):
        try:
            cycle = int(row["cycle"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid ledger cycle {path}:{line_number}") from exc
        if cycle in seen:
            raise ValueError(f"duplicate ledger cycle {cycle}")
        seen.add(cycle)
        if cycle == record_cycle:
            if row != record:
                raise ValueError(f"conflicting ledger replay for cycle {record_cycle}")
            continue
        rows.append(row)
    rows.append(record)
    rows.sort(key=lambda row: int(row["cycle"]))
    durable_write_text(path, "".join(json.dumps(row) + "\n" for row in rows))


def record_equity(state_dir, ts: datetime, equity: float, cycle: int) -> None:
    """Durably insert the immutable, monotonic per-cycle equity row."""
    path = Path(state_dir) / "equity-history.jsonl"
    rows: list[dict] = []
    seen: set[int] = set()
    for line_number, row in enumerate(_jsonl_rows(path), start=1):
        try:
            row_cycle = int(row["cycle"])
            row_ts = datetime.fromisoformat(str(row["ts"]))
            row_equity = float(row["equity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid equity row {path}:{line_number}") from exc
        if row_cycle in seen:
            raise ValueError(f"duplicate equity cycle {row_cycle}")
        seen.add(row_cycle)
        if row_cycle == cycle:
            if row_ts != ts or row_equity != float(equity):
                raise ValueError(f"conflicting equity replay for cycle {cycle}")
            continue
        rows.append(row)
    if rows and ts < datetime.fromisoformat(str(rows[-1]["ts"])):
        raise ValueError("record_equity: non-monotonic timestamp")
    rows.append({"ts": ts.isoformat(), "equity": float(equity), "cycle": cycle})
    durable_write_text(path, "".join(json.dumps(row, default=str) + "\n" for row in rows))


def _cycle_row(path: Path, cycle: int) -> dict:
    matches: list[dict] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSONL row {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"non-object JSONL row {path}:{line_number}")
        try:
            row_cycle = int(row["cycle"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid cycle row {path}:{line_number}") from exc
        if row_cycle == cycle:
            matches.append(row)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one cycle {cycle} row in {path}")
    return matches[0]


def _verify_completion_manifest(
    state_dir,
    directory: Path,
    cycle: int,
    marker: dict,
    *,
    cadence: str,
) -> bool:
    manifest = marker.get("manifest")
    if not isinstance(manifest, dict):
        return False
    try:
        version = int(manifest.get("version", 0))
    except (TypeError, ValueError):
        return False
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        return False
    artifacts = manifest.get("artifact_sha256")
    if not isinstance(artifacts, dict) or not artifacts:
        return False
    try:
        values: dict[str, object] = {}
        for name, expected_hash in artifacts.items():
            value = json.loads((directory / f"{name}.json").read_text())
            if _canonical_sha256(value) != str(expected_hash):
                return False
            values[str(name)] = value
        ledger_row = _cycle_row(Path(state_dir) / "ledger.jsonl", cycle)
        equity_row = _cycle_row(Path(state_dir) / "equity-history.jsonl", cycle)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    common_valid = _canonical_sha256(ledger_row) == manifest.get(
        "ledger_sha256"
    ) and _canonical_sha256(equity_row) == manifest.get("equity_sha256")
    if not common_valid or version == 1:
        return common_valid
    account_state = values.get("account_state")
    runtime_provenance = values.get("runtime_provenance")
    durable_valid = (
        isinstance(account_state, dict)
        and _canonical_sha256(account_state) == manifest.get("account_state_sha256")
        and verify_runtime_provenance(runtime_provenance)
        and _canonical_sha256(runtime_provenance) == manifest.get("runtime_provenance_sha256")
    )
    if not durable_valid or version == 2:
        return durable_valid
    account_event = values.get("account_event")
    try:
        account_events = load_account_events(state_dir)
    except (OSError, TypeError, ValueError):
        return False
    event_valid = bool(
        isinstance(account_event, dict)
        and account_event in account_events
        and account_event.get("kind") == "reconcile"
        and marker.get("commit_id") == marker.get("account_event_id")
        and account_event.get("event_id") == marker.get("account_event_id")
        and account_event.get("previous_event_id") == marker.get("previous_account_event_id")
        and account_event.get("account_state_sha256") == manifest.get("account_state_sha256")
    )
    if not event_valid or version == 3:
        return event_valid
    if (
        marker.get("cadence") != cadence
        or "account_event" not in values
        or account_event.get("schema_version") != 2
        or account_event.get("generation_root_sha256")
        != manifest.get("generation_root_sha256")
    ):
        return False
    root_artifacts = dict(values)
    root_artifacts.pop("account_event")
    try:
        expected_root = _reconcile_generation_root_sha256(
            commit_id=str(marker["commit_id"]),
            cycle=cycle,
            cadence=cadence,
            artifacts=root_artifacts,
            ledger=ledger_row,
            equity_row=equity_row,
        )
    except (KeyError, TypeError, ValueError):
        return False
    return expected_root == manifest.get("generation_root_sha256")


def _protocol_path(state_dir) -> Path:
    return Path(state_dir) / PROTOCOL_FILE


def transaction_path(state_dir) -> Path:
    return Path(state_dir) / TRANSACTION_FILE


def _protocol(state_dir) -> dict | None:
    path = _protocol_path(state_dir)
    if not path.exists():
        return None
    raw = _read_json(path)
    if int(raw.get("version", 0)) != PROTOCOL_VERSION:
        raise ValueError(f"unsupported reconcile protocol in {path}")
    int(raw["first_cycle"])
    return raw


def protocol_first_cycle(state_dir) -> int | None:
    raw = _protocol(state_dir)
    return int(raw["first_cycle"]) if raw is not None else None


def protocol_manifest_required(state_dir) -> bool:
    """Whether every post-protocol completion marker must carry an intact manifest.

    The absent field means an older installation is still inside the explicit one-time migration.
    Fresh installations start strict and can never silently downgrade if one marker is damaged.
    """
    raw = _protocol(state_dir)
    return bool(raw is not None and raw.get("manifest_required") is True)


def _ensure_protocol(state_dir, cycle: int) -> None:
    path = _protocol_path(state_dir)
    if path.exists():
        protocol_first_cycle(state_dir)
        return
    _atomic_json(
        path,
        {
            "version": PROTOCOL_VERSION,
            "first_cycle": int(cycle),
            "manifest_required": True,
        },
    )


def cycle_is_complete(
    state_dir,
    cycle: int,
    *,
    cadence: str = "rebal",
    require_manifest: bool = False,
) -> bool:
    """Use the durable marker for new-protocol cycles and parseable report for legacy cycles."""
    directory = cycle_dir(state_dir, cycle, cadence=cadence)
    first = protocol_first_cycle(state_dir)
    if first is not None and cycle >= first:
        path = directory / f"{COMPLETE_ARTIFACT}.json"
    else:
        path = directory / "report.json"
    if not path.exists():
        return False
    try:
        raw = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if int(raw.get("cycle", -1)) != cycle:
        return False
    # Pre-protocol directories are immutable legacy records without a completion manifest. New
    # generations verify one whenever present; provenance-sensitive consumers can require it.
    if first is None or cycle < first:
        return True
    if raw.get("paper_only") is not True:
        return False
    has_manifest = isinstance(raw.get("manifest"), dict)
    if (require_manifest or protocol_manifest_required(state_dir)) and not has_manifest:
        return False
    return not has_manifest or _verify_completion_manifest(
        state_dir,
        directory,
        cycle,
        raw,
        cadence=cadence,
    )


def completed_cycle_numbers(state_dir, *, cadence: str = "rebal") -> list[int]:
    root = Path(state_dir) / cadence / "cycle"
    if not root.exists():
        return []
    numbers = sorted(
        int(path.name) for path in root.iterdir() if path.is_dir() and path.name.isdigit()
    )
    return [cycle for cycle in numbers if cycle_is_complete(state_dir, cycle, cadence=cadence)]


def completed_artifact_sha256(
    state_dir, cycle: int, artifact: str, *, cadence: str = "rebal"
) -> str | None:
    """Prove an artifact existed at commit and its exact content is in the manifest.

    A valid completion marker proves only the files it lists. Merely adding a new file beside an
    old completed cycle must never make that file eligible for immutable learning.
    """
    first = protocol_first_cycle(state_dir)
    if first is None or cycle < first:
        return None
    if not cycle_is_complete(state_dir, cycle, cadence=cadence, require_manifest=True):
        return None
    directory = cycle_dir(state_dir, cycle, cadence=cadence)
    try:
        marker = _read_json(directory / "complete.json")
        expected = marker["manifest"]["artifact_sha256"].get(artifact)
        if not isinstance(expected, str):
            return None
        value = json.loads((directory / f"{artifact}.json").read_text())
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return expected if _canonical_sha256(value) == expected else None


def completed_artifact_is_bound(
    state_dir, cycle: int, artifact: str, *, cadence: str = "rebal"
) -> bool:
    return completed_artifact_sha256(state_dir, cycle, artifact, cadence=cadence) is not None


def backfill_completion_manifest(state_dir, cycle: int, *, cadence: str = "rebal") -> bool:
    """Bind a pre-manifest protocol generation to its currently audited durable files once.

    ``attribution.json`` is deliberately excluded: reflection is a post-completion learning
    artifact, not part of the reconcile transaction. The marker records that this was a one-time
    migration rather than pretending the hashes existed at the original commit.
    """
    first = protocol_first_cycle(state_dir)
    if first is None or cycle < first:
        raise ValueError(f"cycle {cycle} is not a durable-protocol generation")
    directory = cycle_dir(state_dir, cycle, cadence=cadence)
    marker_path = directory / "complete.json"
    marker = _read_json(marker_path)
    if int(marker.get("cycle", -1)) != cycle or marker.get("paper_only") is not True:
        raise ValueError(f"invalid completion marker for cycle {cycle}")
    if isinstance(marker.get("manifest"), dict):
        if not _verify_completion_manifest(
            state_dir,
            directory,
            cycle,
            marker,
            cadence=cadence,
        ):
            raise ValueError(f"existing completion manifest is not intact for cycle {cycle}")
        return False
    if protocol_manifest_required(state_dir):
        raise ValueError(
            f"cycle {cycle} lost its required completion manifest; migration is already closed"
        )
    artifact_values = {
        path.stem: json.loads(path.read_text())
        for path in sorted(directory.glob("*.json"))
        if path.name not in {"complete.json", "attribution.json"}
    }
    if not {"book", "evidence", "report"}.issubset(artifact_values):
        raise ValueError(f"cycle {cycle} lacks required durable artifacts for manifest backfill")
    ledger_row = _cycle_row(Path(state_dir) / "ledger.jsonl", cycle)
    equity_row = _cycle_row(Path(state_dir) / "equity-history.jsonl", cycle)
    marker["manifest"] = {
        # A backfill cannot truthfully reconstruct a post-commit account/runtime snapshot. Keep
        # the historical v1 semantics rather than upgrading provenance after the fact.
        "version": 1,
        "artifact_sha256": {
            name: _canonical_sha256(value) for name, value in sorted(artifact_values.items())
        },
        "ledger_sha256": _canonical_sha256(ledger_row),
        "equity_sha256": _canonical_sha256(equity_row),
    }
    marker["manifest_backfilled_at"] = datetime.now(UTC).isoformat()
    marker["manifest_provenance"] = (
        "one-time migration binding the pre-manifest generation's current audited files"
    )
    _atomic_json(marker_path, marker)
    return True


def finalize_manifest_migration(state_dir, *, cadence: str = "rebal") -> None:
    """Close the one-time pre-manifest migration after every published marker verifies.

    Once set, ``manifest_required`` is irreversible through normal runtime code. A later missing
    or corrupt manifest is damage, not an invitation to trust and re-hash mutable artifacts.
    """
    raw = _protocol(state_dir)
    if raw is None:
        raise ValueError("cannot finalize manifest migration without a reconcile protocol")
    first = int(raw["first_cycle"])
    root = Path(state_dir) / cadence / "cycle"
    published = (
        sorted(
            int(path.name)
            for path in root.iterdir()
            if path.is_dir()
            and path.name.isdigit()
            and int(path.name) >= first
            and (path / "complete.json").exists()
        )
        if root.exists()
        else []
    )
    invalid = [
        cycle
        for cycle in published
        if not cycle_is_complete(state_dir, cycle, cadence=cadence, require_manifest=True)
    ]
    if invalid:
        raise ValueError(f"cannot require manifests; invalid protocol cycles: {invalid}")
    if raw.get("manifest_required") is True:
        return
    raw["manifest_required"] = True
    raw["manifest_migration_completed_at"] = datetime.now(UTC).isoformat()
    _atomic_json(_protocol_path(state_dir), raw)


def _validate_reconcile_directive_binding(
    state_dir,
    *,
    cycle: int,
    artifacts: object,
    expectation: object,
    allow_consumed: bool = False,
) -> dict:
    """Couple WAL state, explicit claim presence, and its exact schema-v2 artifact."""

    from futures_fund.directives import (
        DIRECTIVE_RECEIPT_ARTIFACT,
        validate_directive_commit_expectation,
        validate_directive_receipt,
    )

    if not isinstance(artifacts, dict):
        raise ValueError("reconcile transaction artifacts must be an object")
    expected = validate_directive_commit_expectation(
        state_dir,
        expectation,
        allow_consumed=allow_consumed,
    )
    if expected["cycle"] != cycle:
        raise ValueError("directive expectation cycle does not match reconcile cycle")
    receipt_present = DIRECTIVE_RECEIPT_ARTIFACT in artifacts
    if expected["present"] is not receipt_present:
        raise ValueError("directive expectation/receipt presence mismatch")
    if not receipt_present:
        return expected
    receipt = validate_directive_receipt(
        artifacts[DIRECTIVE_RECEIPT_ARTIFACT],
        expected_cycle=cycle,
    )
    bound_fields = (
        "claim_id",
        "claim_intent_sha256",
        "source_relpath",
        "payload_sha256",
        "directive_sha256",
        "capabilities",
        "capabilities_sha256",
    )
    if any(receipt[field] != expected[field] for field in bound_fields):
        raise ValueError("directive receipt does not match reconcile expectation")
    return expected


def stage_reconcile_transaction(
    state_dir,
    *,
    expected_base_account_sha256: str | None,
    cycle: int,
    cadence: str,
    account: PaperAccount,
    artifacts: dict[str, object],
    equity_ts: datetime,
    equity: float,
    ledger: dict,
    runtime_provenance: dict | None = None,
    directive_expectation: dict | None = None,
) -> dict:
    """Durably record the entire intended post-reconcile generation before any state write."""
    if type(cycle) is not int or cycle < 1:
        raise ValueError("reconcile cycle must be a positive integer")
    with exclusive_state_transaction_lock(state_dir):
        from futures_fund.directives import build_directive_commit_expectation

        candidate_expectation = (
            directive_expectation
            if directive_expectation is not None
            else build_directive_commit_expectation(None, cycle=int(cycle))
        )
        validated_directive_expectation = _validate_reconcile_directive_binding(
            state_dir,
            cycle=int(cycle),
            artifacts=artifacts,
            expectation=candidate_expectation,
        )
        if current_account_sha256(state_dir) != expected_base_account_sha256:
            raise RuntimeError("reconcile staging lost its optimistic account-state lock")
        return _stage_reconcile_transaction_unlocked(
            state_dir,
            expected_base_account_sha256=expected_base_account_sha256,
            cycle=cycle,
            cadence=cadence,
            account=account,
            artifacts=artifacts,
            equity_ts=equity_ts,
            equity=equity,
            ledger=ledger,
            runtime_provenance=runtime_provenance,
            directive_expectation=validated_directive_expectation,
        )


def _stage_reconcile_transaction_unlocked(
    state_dir,
    *,
    expected_base_account_sha256: str | None,
    cycle: int,
    cadence: str,
    account: PaperAccount,
    artifacts: dict[str, object],
    equity_ts: datetime,
    equity: float,
    ledger: dict,
    directive_expectation: dict,
    runtime_provenance: dict | None = None,
) -> dict:
    path = transaction_path(state_dir)
    if path.exists():
        raise RuntimeError(f"unfinished reconcile transaction already exists: {path}")
    heartbeat_path = Path(state_dir) / HEARTBEAT_TRANSACTION_FILE
    if heartbeat_path.exists():
        raise RuntimeError(
            f"cannot stage reconcile while heartbeat transaction is unfinished: {heartbeat_path}"
        )
    _ensure_protocol(state_dir, cycle)
    account_state = account.to_dict()
    base_account_sha256 = expected_base_account_sha256
    provenance = runtime_provenance or default_runtime_provenance(captured_at=equity_ts)
    if not verify_runtime_provenance(provenance):
        raise ValueError("invalid runtime provenance")
    durable_artifacts = dict(artifacts)
    for reserved in ("account_event", "account_state", "runtime_provenance", "complete"):
        if reserved in durable_artifacts:
            raise ValueError(f"reserved reconcile artifact supplied by caller: {reserved}")
    durable_artifacts["account_state"] = account_state
    durable_artifacts["runtime_provenance"] = provenance
    commit_id = uuid4().hex
    equity_row = {
        "ts": equity_ts.isoformat(),
        "equity": float(equity),
        "cycle": int(cycle),
    }
    generation_root_sha256 = _reconcile_generation_root_sha256(
        commit_id=commit_id,
        cycle=cycle,
        cadence=cadence,
        artifacts=durable_artifacts,
        ledger=ledger,
        equity_row=equity_row,
    )
    account_event = prepare_account_event(
        state_dir,
        event_id=commit_id,
        kind="reconcile",
        event_ts=equity_ts.isoformat(),
        base_account_sha256=base_account_sha256,
        account_state_sha256=_canonical_sha256(account_state),
        generation_root_sha256=generation_root_sha256,
    )
    durable_artifacts["account_event"] = account_event
    transaction = {
        "version": RECONCILE_TRANSACTION_VERSION,
        "paper_only": True,
        "manifest_version": MANIFEST_VERSION,
        "commit_id": commit_id,
        "cycle": int(cycle),
        "cadence": cadence,
        "base_account_sha256": base_account_sha256,
        "account": account_state,
        "artifacts": durable_artifacts,
        "equity": {"ts": equity_ts.isoformat(), "value": float(equity)},
        "ledger": ledger,
        "generation_root_sha256": generation_root_sha256,
    }
    transaction["directive_expectation"] = dict(directive_expectation)
    transaction["intent_sha256"] = _canonical_sha256(transaction)
    _atomic_json(path, transaction)
    return transaction


def recover_reconcile_transaction(state_dir) -> dict:
    """Replay an unfinished generation idempotently; publish completion last."""
    with exclusive_state_transaction_lock(state_dir):
        return _recover_reconcile_transaction_unlocked(state_dir)


def _recover_reconcile_transaction_unlocked(state_dir) -> dict:
    path = transaction_path(state_dir)
    if not path.exists():
        return {"recovered": False}
    heartbeat_path = Path(state_dir) / HEARTBEAT_TRANSACTION_FILE
    if heartbeat_path.exists():
        raise RuntimeError(
            "both reconcile and heartbeat transactions are pending; refusing ambiguous replay"
        )
    transaction = _read_json(path)
    transaction_version = transaction.get("version")
    if (
        type(transaction_version) is not int
        or transaction_version not in SUPPORTED_RECONCILE_TRANSACTION_VERSIONS
        or transaction.get("paper_only") is not True
    ):
        raise ValueError(f"invalid reconcile transaction: {path}")
    expected_intent = transaction.get("intent_sha256")
    if not isinstance(expected_intent, str):
        raise ValueError(f"reconcile transaction lacks required intent hash: {path}")
    intent_body = dict(transaction)
    intent_body.pop("intent_sha256", None)
    if _canonical_sha256(intent_body) != expected_intent:
        raise ValueError(f"reconcile transaction hash mismatch: {path}")
    if "base_account_sha256" not in transaction or (
        transaction["base_account_sha256"] is not None
        and not isinstance(transaction["base_account_sha256"], str)
    ):
        raise ValueError(f"reconcile transaction lacks valid account lineage: {path}")
    try:
        manifest_version = int(transaction.get("manifest_version", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"reconcile transaction has invalid manifest version: {path}") from exc
    if manifest_version not in {3, MANIFEST_VERSION}:
        raise ValueError(f"reconcile transaction has unsupported manifest version: {path}")
    raw_cycle = transaction.get("cycle")
    if transaction_version >= RECONCILE_TRANSACTION_VERSION and (
        type(raw_cycle) is not int or raw_cycle < 1
    ):
        raise ValueError("current reconcile transaction has invalid cycle")
    cycle = int(raw_cycle)
    cadence = str(transaction["cadence"])
    if transaction_version >= RECONCILE_TRANSACTION_VERSION:
        if "directive_expectation" not in transaction:
            raise ValueError("current reconcile transaction lacks directive expectation")
        _validate_reconcile_directive_binding(
            state_dir,
            cycle=cycle,
            artifacts=transaction.get("artifacts"),
            expectation=transaction["directive_expectation"],
            allow_consumed=True,
        )
    elif "directive_expectation" in transaction:
        _validate_reconcile_directive_binding(
            state_dir,
            cycle=cycle,
            artifacts=transaction.get("artifacts"),
            expectation=transaction["directive_expectation"],
            allow_consumed=True,
        )
    else:
        # A genuine v1 WAL predates claimed-instance directives. It may recover only while that
        # newer lifecycle is explicitly absent; otherwise legacy replay could publish around a
        # claim that current code correctly refuses to create while any WAL exists.
        from futures_fund.directives import build_directive_commit_expectation

        _validate_reconcile_directive_binding(
            state_dir,
            cycle=cycle,
            artifacts=transaction.get("artifacts"),
            expectation=build_directive_commit_expectation(None, cycle=cycle),
            allow_consumed=True,
        )
    commit_id = str(transaction["commit_id"])
    complete_path = cycle_dir(state_dir, cycle, cadence=cadence) / "complete.json"
    pending_event = transaction.get("artifacts", {}).get("account_event")
    if not isinstance(pending_event, dict):
        raise ValueError("reconcile transaction lacks its unified account event")
    verify_account_event_references(state_dir, allow_pending_event=pending_event)
    if complete_path.exists():
        complete = _read_json(complete_path)
        if (
            complete.get("commit_id") != commit_id
            or int(complete.get("cycle", -1)) != cycle
        ):
            raise RuntimeError(
                f"cycle {cycle} completion marker conflicts with unfinished transaction"
            )
        completion_intact = bool(
            complete.get("paper_only") is True
            and _verify_completion_manifest(
                state_dir,
                cycle_dir(state_dir, cycle, cadence=cadence),
                cycle,
                complete,
                cadence=cadence,
            )
        )
        if completion_intact:
            root_account = _read_json(Path(state_dir) / "account.json")
            if _canonical_sha256(root_account) != _canonical_sha256(transaction["account"]):
                raise RuntimeError("completed reconcile account does not match durable intent")
            durable_unlink(path)
            return {"recovered": True, "cycle": cycle, "already_complete": True}
        # A same-cycle, same-commit marker that failed verification is a derived partial write.
        # Retain and replay the hash-bound intent; a conflicting identity above always halts.

    account = PaperAccount.from_dict(transaction["account"])
    target_account_sha256 = _canonical_sha256(account.to_dict())
    if target_account_sha256 != _canonical_sha256(transaction["account"]):
        raise ValueError("reconcile transaction account does not round-trip canonically")
    verify_account_lineage(
        state_dir,
        base_sha256=transaction.get("base_account_sha256"),
        target_sha256=target_account_sha256,
        label="reconcile recovery",
    )
    artifacts = dict(transaction["artifacts"])
    report = artifacts.pop("report")
    equity = dict(transaction["equity"])
    equity_ts = datetime.fromisoformat(str(equity["ts"]))
    equity_value = float(equity["value"])
    ledger = dict(transaction["ledger"])
    if int(ledger.get("cycle", -1)) != cycle:
        raise ValueError("reconcile transaction ledger cycle mismatch")
    equity_row = {"ts": equity_ts.isoformat(), "equity": equity_value, "cycle": cycle}
    if manifest_version == MANIFEST_VERSION:
        root_artifacts = dict(transaction["artifacts"])
        root_event = root_artifacts.pop("account_event", None)
        expected_root = _reconcile_generation_root_sha256(
            commit_id=commit_id,
            cycle=cycle,
            cadence=cadence,
            artifacts=root_artifacts,
            ledger=ledger,
            equity_row=equity_row,
        )
        if (
            not isinstance(root_event, dict)
            or transaction.get("generation_root_sha256") != expected_root
            or root_event.get("generation_root_sha256") != expected_root
        ):
            raise ValueError("reconcile transaction generation-root mismatch")

    # All operations below are idempotent. The completion marker is the only public commit point.
    save_account(state_dir, account)
    for name, value in artifacts.items():
        save_output(state_dir, cycle, name, value, cadence=cadence)
    record_equity(
        state_dir,
        equity_ts,
        equity_value,
        cycle,
    )
    append_ledger(state_dir, ledger)
    append_account_event(state_dir, artifacts["account_event"])
    save_output(state_dir, cycle, "report", report, cadence=cadence)
    durable_artifacts = {**artifacts, "report": report}
    save_output(
        state_dir,
        cycle,
        COMPLETE_ARTIFACT,
        {
            "version": PROTOCOL_VERSION,
            "paper_only": True,
            "cycle": cycle,
            "cadence": cadence,
            "commit_id": commit_id,
            "account_event_id": artifacts["account_event"]["event_id"],
            "previous_account_event_id": artifacts["account_event"]["previous_event_id"],
            "completed_at": str(equity["ts"]),
            "manifest": {
                "version": manifest_version,
                "artifact_sha256": {
                    name: _canonical_sha256(value)
                    for name, value in sorted(durable_artifacts.items())
                },
                "ledger_sha256": _canonical_sha256(ledger),
                "equity_sha256": _canonical_sha256(equity_row),
                "account_state_sha256": _canonical_sha256(transaction["account"]),
                "runtime_provenance_sha256": _canonical_sha256(
                    durable_artifacts["runtime_provenance"]
                ),
                **(
                    {"generation_root_sha256": transaction["generation_root_sha256"]}
                    if manifest_version == MANIFEST_VERSION
                    else {}
                ),
            },
        },
        cadence=cadence,
    )
    if not cycle_is_complete(
        state_dir,
        cycle,
        cadence=cadence,
        require_manifest=True,
    ):
        raise RuntimeError("published reconcile completion failed verification")
    if _canonical_sha256(_read_json(Path(state_dir) / "account.json")) != _canonical_sha256(
        transaction["account"]
    ):
        raise RuntimeError("published reconcile account does not match durable intent")
    durable_unlink(path)
    return {"recovered": True, "cycle": cycle, "already_complete": False}
