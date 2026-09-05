"""Process-safe serialization and lineage checks for PAPER account transactions."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from futures_fund.durable_io import canonical_json_sha256, durable_write_text

ACCOUNT_EVENTS_FILE = "account-events.jsonl"
ACCOUNT_EVENT_SCHEMA_VERSION = 2
SUPPORTED_ACCOUNT_EVENT_SCHEMA_VERSIONS = {1, ACCOUNT_EVENT_SCHEMA_VERSION}
UNIFIED_ACCOUNT_EVENT_GENERATION_VERSION = 3


class StateSnapshotChanged(RuntimeError):
    """The state root appeared while a no-create health snapshot was being read."""

_ACCOUNT_HISTORY_FILES = (
    ACCOUNT_EVENTS_FILE,
    "ledger.jsonl",
    "equity-history.jsonl",
    "portfolio-heartbeats.jsonl",
    "reconcile-protocol.json",
    "seat-role-migration.json",
    "reconcile-transaction.json",
    "heartbeat-transaction.json",
)
_ACCOUNT_HISTORY_DIRS = ("rebal", "heartbeat-generations")


def account_history_established(state_dir: str | Path) -> bool:
    """Return whether any durable artifact proves this is not a fresh PAPER account.

    This predicate intentionally recognizes legacy generations as well as the current unified
    account-event protocol.  Losing ``account.json`` must never turn a mature desk into a seed-cash
    cold start merely because its older manifests predate the v3 event chain.
    """
    root = Path(state_dir)
    if any((root / name).exists() for name in _ACCOUNT_HISTORY_FILES):
        return True
    return any(
        directory.exists() and any(directory.iterdir())
        for directory in (root / name for name in _ACCOUNT_HISTORY_DIRS)
    )


def account_event_chain_established(state_dir: str | Path) -> bool:
    """Return whether any durable generation declares the unified account-event protocol."""
    root = Path(state_dir)
    cycle_root = root / "rebal" / "cycle"
    if cycle_root.exists():
        for marker_path in cycle_root.glob("*/complete.json"):
            try:
                marker = json.loads(marker_path.read_text())
                if int(marker.get("manifest", {}).get("version", 0)) >= 3:
                    return True
            except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
    heartbeat_path = root / "portfolio-heartbeats.jsonl"
    if heartbeat_path.exists():
        for line in heartbeat_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if int(row.get("heartbeat_schema_version", 0)) >= 3:
                    return True
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return False


@contextmanager
def exclusive_state_transaction_lock(state_dir: str | Path) -> Iterator[None]:
    """Serialize all account transaction staging and recovery, even outside launchers."""
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def shared_state_transaction_lock(state_dir: str | Path) -> Iterator[None]:
    """Hold a consistent read snapshot against reconcile/heartbeat publication.

    Multiple health readers may run together, while every writer takes the exclusive form of the
    same directory lock. If the state root does not exist, this path creates nothing; a concurrent
    first writer is detected so the caller can retry against the newly lockable directory.
    """
    root = Path(state_dir)
    try:
        descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        yield
        if root.exists():
            raise StateSnapshotChanged(
                "state root appeared during a read-only snapshot"
            ) from None
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def current_account_sha256(state_dir: str | Path) -> str | None:
    """Return the canonical hash of the current account, or ``None`` before first publish."""
    path = Path(state_dir) / "account.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return canonical_json_sha256(value)


def load_account_with_sha256(state_dir: str | Path, *, default_cash: float):
    """Atomically load the exact PAPER account object and its optimistic-lock hash."""
    from futures_fund.account import load_account

    with exclusive_state_transaction_lock(state_dir):
        digest = current_account_sha256(state_dir)
        if digest is None and account_history_established(state_dir):
            raise RuntimeError(
                "account.json is missing while durable account history exists; "
                "recover an audited account snapshot instead of reseeding cash"
            )
        account = load_account(state_dir, default_cash=default_cash)
        if digest is not None and canonical_json_sha256(account.to_dict()) != digest:
            raise RuntimeError("loaded PAPER account does not match its canonical disk hash")
        return account, digest


def verify_account_lineage(
    state_dir: str | Path,
    *,
    base_sha256: str | None,
    target_sha256: str,
    label: str,
) -> None:
    """Allow only the staged base or an idempotently published target account state."""
    current = current_account_sha256(state_dir)
    allowed = {base_sha256, target_sha256}
    if current not in allowed:
        raise RuntimeError(
            f"{label} account lineage conflict: current state is neither staged base nor target"
        )


def _account_event_sha256(event: dict) -> str:
    body = dict(event)
    body.pop("event_sha256", None)
    return canonical_json_sha256(body)


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _account_event_schema_valid(event: dict) -> bool:
    try:
        version = int(event.get("schema_version", 0))
    except (TypeError, ValueError):
        return False
    return bool(
        version in SUPPORTED_ACCOUNT_EVENT_SCHEMA_VERSIONS
        and (
            version < ACCOUNT_EVENT_SCHEMA_VERSION
            or _is_sha256(event.get("generation_root_sha256"))
        )
    )


def _validate_next_account_event(events: list[dict], event: dict) -> None:
    if (
        not _account_event_schema_valid(event)
        or event.get("kind") not in {"reconcile", "heartbeat"}
        or not isinstance(event.get("event_id"), str)
        or not event["event_id"]
        or event.get("event_sha256") != _account_event_sha256(event)
    ):
        raise ValueError("invalid next account event")
    try:
        event_ts = datetime.fromisoformat(str(event["event_ts"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid next account event timestamp") from exc
    if event_ts.tzinfo is None:
        raise ValueError("next account event timestamp must be timezone-aware")
    prior = events[-1] if events else None
    if prior is None:
        if (
            event.get("lineage_origin") != "root_account_anchor"
            or event.get("previous_event_id") is not None
            or event.get("previous_event_sha256") is not None
        ):
            raise ValueError("first account event must declare a root-account anchor")
        return
    prior_ts = datetime.fromisoformat(str(prior["event_ts"]).replace("Z", "+00:00"))
    if event_ts < prior_ts:
        raise ValueError("account event timestamps are not monotonic")
    if (
        event.get("lineage_origin") != "account_event"
        or event.get("previous_event_id") != prior["event_id"]
        or event.get("previous_event_sha256") != prior["event_sha256"]
        or event.get("base_account_sha256") != prior["account_state_sha256"]
    ):
        raise ValueError(f"broken account event chain at {event.get('event_id')}")


def load_account_events(state_dir: str | Path) -> list[dict]:
    """Load and fully verify the unified reconcile/heartbeat account-state chain."""
    path = Path(state_dir) / ACCOUNT_EVENTS_FILE
    if not path.exists():
        return []
    events: list[dict] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed account event {path}:{line_number}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"non-object account event {path}:{line_number}")
        event_id = event.get("event_id")
        if (
            not _account_event_schema_valid(event)
            or event.get("kind") not in {"reconcile", "heartbeat"}
            or not isinstance(event_id, str)
            or not event_id
            or not isinstance(event.get("account_state_sha256"), str)
            or event.get("event_sha256") != _account_event_sha256(event)
        ):
            raise ValueError(f"invalid account event {path}:{line_number}")
        if event_id in seen:
            raise ValueError(f"duplicate account event id {event_id}")
        seen.add(event_id)
        try:
            event_ts = datetime.fromisoformat(str(event["event_ts"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"invalid account event timestamp {path}:{line_number}") from exc
        if event_ts.tzinfo is None:
            raise ValueError(f"naive account event timestamp {path}:{line_number}")
        if events:
            previous = events[-1]
            previous_ts = datetime.fromisoformat(
                str(previous["event_ts"]).replace("Z", "+00:00")
            )
            if event_ts < previous_ts:
                raise ValueError("account event timestamps are not monotonic")
            if (
                event.get("lineage_origin") != "account_event"
                or event.get("previous_event_id") != previous["event_id"]
                or event.get("previous_event_sha256") != previous["event_sha256"]
                or event.get("base_account_sha256") != previous["account_state_sha256"]
            ):
                raise ValueError(f"broken account event chain at {event_id}")
        elif (
            event.get("lineage_origin") != "root_account_anchor"
            or event.get("previous_event_id") is not None
            or event.get("previous_event_sha256") is not None
        ):
            raise ValueError("first account event must declare a root-account anchor")
        events.append(event)
    return events


def verify_account_event_references(
    state_dir: str | Path,
    *,
    allow_pending_event: dict | None = None,
) -> None:
    """Require every unified account event to have one intact declaring generation.

    During crash recovery the newest event may already have reached the global chain while its
    completion marker has not. The caller may allow exactly that final, byte-identical intent
    event; every predecessor still has to resolve to an intact v3 reconcile or heartbeat.
    Imports are intentionally local so both commit modules can share this check without an import
    cycle at module initialization.
    """
    events = load_account_events(state_dir)
    allowed_id = allow_pending_event.get("event_id") if allow_pending_event else None
    if allow_pending_event is not None and (
        not isinstance(allowed_id, str)
        or allow_pending_event.get("event_sha256") != _account_event_sha256(allow_pending_event)
    ):
        raise ValueError("invalid allowed pending account event")

    from futures_fund.heartbeat import verify_heartbeat_completion  # local import cycle
    from futures_fund.reconcile_commit import cycle_is_complete  # local import cycle

    root = Path(state_dir)
    reconcile_refs: dict[str, list[tuple[int, dict]]] = {}
    cycle_root = root / "rebal" / "cycle"
    if cycle_root.exists():
        for directory in cycle_root.iterdir():
            if not directory.is_dir() or not directory.name.isdigit():
                continue
            marker_path = directory / "complete.json"
            try:
                marker = json.loads(marker_path.read_text())
                manifest = marker.get("manifest") if isinstance(marker, dict) else None
                event_id = marker.get("account_event_id") if isinstance(marker, dict) else None
                version = int(manifest.get("version", 0)) if isinstance(manifest, dict) else 0
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if version >= UNIFIED_ACCOUNT_EVENT_GENERATION_VERSION and isinstance(event_id, str):
                reconcile_refs.setdefault(event_id, []).append((int(directory.name), marker))

    heartbeat_refs: dict[str, list[dict]] = {}
    heartbeat_path = root / "portfolio-heartbeats.jsonl"
    if heartbeat_path.exists():
        for line_number, line in enumerate(heartbeat_path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"malformed heartbeat ledger {heartbeat_path}:{line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"non-object heartbeat ledger row {heartbeat_path}:{line_number}"
                )
            try:
                version = int(row.get("heartbeat_schema_version", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid heartbeat schema version {heartbeat_path}:{line_number}"
                ) from exc
            # Legacy portfolio observations were intentionally less structured and may omit
            # ``kind`` entirely. They predate the unified account-event protocol and therefore
            # cannot declare a reference. Only v3+ rows are subject to the strict binding below.
            if version >= UNIFIED_ACCOUNT_EVENT_GENERATION_VERSION:
                event_id = row.get("commit_id")
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError(
                        f"v3 heartbeat lacks commit_id {heartbeat_path}:{line_number}"
                    )
                heartbeat_refs.setdefault(event_id, []).append(row)

    events_by_id = {event["event_id"]: event for event in events}
    allowed_is_published = allowed_id in events_by_id
    if allow_pending_event is not None:
        if allowed_is_published:
            if events[-1] != allow_pending_event:
                raise ValueError("allowed pending account event is not the exact chain head")
        else:
            _validate_next_account_event(events, allow_pending_event)

    # The relationship is deliberately bidirectional. A generation whose event disappeared is
    # just as corrupt as an event whose generation disappeared; accepting either would permit a
    # valid prefix to fork around a deleted account transition.
    for event_id in set(reconcile_refs) | set(heartbeat_refs):
        declared = events_by_id.get(event_id)
        if declared is None and event_id == allowed_id:
            declared = allow_pending_event
        if declared is None:
            raise ValueError(f"orphan v3 generation declares absent account event {event_id}")
        reconcile_count = len(reconcile_refs.get(event_id, []))
        heartbeat_count = len(heartbeat_refs.get(event_id, []))
        if reconcile_count + heartbeat_count != 1:
            raise ValueError(
                f"account event {event_id} has {reconcile_count + heartbeat_count} "
                "cross-ledger declarations"
            )
        declared_kind = "reconcile" if reconcile_count else "heartbeat"
        if declared.get("kind") != declared_kind:
            raise ValueError(
                f"account event {event_id} is {declared.get('kind')}, "
                f"but its generation declares {declared_kind}"
            )

    for index, event in enumerate(events):
        event_id = event["event_id"]
        is_pending = event_id == allowed_id
        if is_pending and (event != allow_pending_event or index != len(events) - 1):
            raise ValueError("allowed pending account event is not the exact chain head")
        if event["kind"] == "reconcile":
            references = reconcile_refs.get(event_id, [])
            if is_pending:
                # The durable intent is authoritative for its exact current head. Reconcile
                # publishes the event before complete.json, and a crash/fault may leave either no
                # marker or a same-ID marker that failed final verification. Recovery may replace
                # that derived marker; duplicate/cross-kind declarations were rejected above.
                if len(references) > 1:
                    raise ValueError(
                        f"pending reconcile account event {event_id} has duplicate declarations"
                    )
                continue
            if len(references) != 1:
                raise ValueError(
                    f"reconcile account event {event_id} has "
                    f"{len(references)} declaring generations"
                )
            cycle, marker = references[0]
            if (
                marker.get("commit_id") != event_id
                or marker.get("manifest", {}).get("account_state_sha256")
                != event["account_state_sha256"]
                or not cycle_is_complete(root, cycle, cadence="rebal", require_manifest=True)
            ):
                raise ValueError(
                    f"reconcile account event {event_id} declaring generation is not intact"
                )
        else:
            references = heartbeat_refs.get(event_id, [])
            if is_pending and not references:
                # A pending heartbeat may be checked before its index row is first appended, but
                # once its event is published the row must already exist (publication order).
                if not allowed_is_published:
                    continue
                raise ValueError(
                    f"pending heartbeat account event {event_id} lost its index declaration"
                )
            if len(references) != 1:
                raise ValueError(
                    f"heartbeat account event {event_id} has "
                    f"{len(references)} declaring generations"
                )
            record = references[0]
            binding_valid = bool(
                record.get("account_state_sha256") == event["account_state_sha256"]
                and record.get("account_event_sha256") == event["event_sha256"]
            )
            completion_valid = verify_heartbeat_completion(root, record)
            recoverable_pending = bool(is_pending and binding_valid)
            if not binding_valid or (not completion_valid and not recoverable_pending):
                raise ValueError(
                    f"heartbeat account event {event_id} declaring generation is not intact"
                )


def prepare_account_event(
    state_dir: str | Path,
    *,
    event_id: str,
    kind: str,
    event_ts: str,
    base_account_sha256: str | None,
    account_state_sha256: str,
    generation_root_sha256: str,
) -> dict:
    """Build a hash-bound next state event, anchored to the current verified chain."""
    verify_account_event_references(state_dir)
    previous = load_account_events(state_dir)
    prior = previous[-1] if previous else None
    if prior is None and account_event_chain_established(state_dir):
        raise RuntimeError("established account-event history is missing; refusing a new anchor")
    if prior is not None and prior["account_state_sha256"] != base_account_sha256:
        raise RuntimeError("account event head does not match the transaction base account")
    event = {
        "schema_version": ACCOUNT_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "kind": kind,
        "event_ts": event_ts,
        "lineage_origin": "account_event" if prior is not None else "root_account_anchor",
        "previous_event_id": prior["event_id"] if prior is not None else None,
        "previous_event_sha256": prior["event_sha256"] if prior is not None else None,
        "base_account_sha256": base_account_sha256,
        "account_state_sha256": account_state_sha256,
        "generation_root_sha256": generation_root_sha256,
    }
    event["event_sha256"] = _account_event_sha256(event)
    _validate_next_account_event(previous, event)
    return event


def append_account_event(state_dir: str | Path, event: dict) -> None:
    """Append one immutable account transition, idempotently on transaction replay."""
    verify_account_event_references(state_dir, allow_pending_event=event)
    events = load_account_events(state_dir)
    same_id = [row for row in events if row["event_id"] == event.get("event_id")]
    if same_id:
        if same_id != [event]:
            raise RuntimeError(f"conflicting account event replay: {event.get('event_id')}")
        return
    candidate = [*events, event]
    _validate_next_account_event(events, event)
    path = Path(state_dir) / ACCOUNT_EVENTS_FILE
    durable_write_text(
        path,
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in candidate),
    )
    # Verify the bytes we just published before a completion marker can make them authoritative.
    load_account_events(state_dir)
