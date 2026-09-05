"""Seal the exact post-reflection build immediately before specialist dispatch.

This is the authoritative decision-start boundary. Evidence and performance are built before the
optional Reflector, but reflection may legitimately change managed agent prompts. This command
therefore runs only after the post-reflection managed-region check, captures the resulting complete
source inventory, binds it into cycle meta, and rebuilds the performance packet against that final
meta. It refuses to seal after any specialist or downstream decision artifact exists.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from futures_fund.config import load_settings
from futures_fund.durable_io import durable_unlink, durable_write_json, durable_write_text
from futures_fund.pending_io import resolve_pending
from futures_fund.performance import build_performance_snapshot, canonical_sha256
from futures_fund.reflection import (
    audit_managed_region_provenance,
    reflector_head_anchor_path,
    reflector_heads_path,
)
from futures_fund.runtime_provenance import (
    DECISION_START_PROVENANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT,
    PROVENANCE_SCHEMA_VERSION,
    default_runtime_provenance,
    load_bound_decision_start_provenance,
    load_bound_pre_reflection_performance,
    verify_runtime_provenance,
)

DECISION_START_TRANSACTION_ARTIFACT = "decision-start-transaction.json"
_TRANSACTION_SCHEMA_VERSION = 1

_DOWNSTREAM_ARTIFACTS = (
    "sentiment_reads.json",
    "technical_reads.json",
    "futures_reads.json",
    "specialist_reads.sha256",
    "pm_book.json",
    "pm_book_original.json",
    "precheck.json",
    "precheck_original.json",
    "adversary.json",
    "revision_dispatch_receipt.json",
    "revision_output_receipt.json",
)
_BINDING_FIELDS = (
    "decision_start_runtime_provenance_artifact",
    "decision_start_runtime_provenance_sha256",
    "decision_start_runtime_provenance_captured_at",
)
_PRE_REFLECTION_BINDING_FIELDS = (
    "pre_reflection_performance_snapshot_artifact",
    "pre_reflection_performance_snapshot_sha256",
)
_SEAL_META_FIELDS = (*_PRE_REFLECTION_BINDING_FIELDS, *_BINDING_FIELDS)
_TRANSACTION_FIELDS = {
    "schema_version",
    "kind",
    "paper_only",
    "cycle",
    "base_meta",
    "base_meta_sha256",
    "sealed_meta",
    "sealed_meta_sha256",
    "pre_reflection_performance_snapshot",
    "pre_reflection_performance_snapshot_sha256",
    "runtime_provenance",
    "runtime_provenance_sha256",
    "performance_snapshot",
    "performance_snapshot_sha256",
    "intent_sha256",
}


def _validate_reflector_heads(*, agents_dir: str, memory_dir: str, state_dir: str) -> None:
    journal = Path(memory_dir) / "reflector-journal.md"
    issues = audit_managed_region_provenance(
        agents_dir,
        journal,
        reflector_heads_path(journal),
        reflector_head_anchor_path(state_dir),
    )
    if issues:
        raise ValueError(
            "managed prompt provenance failed before decision seal: " + "; ".join(issues)
        )


def _validate_performance_binding(snapshot: object, digest: str, meta: dict, *, label: str) -> dict:
    bindings = snapshot.get("bindings") if isinstance(snapshot, dict) else None
    try:
        snapshot_cycle = int(snapshot.get("cycle", 0)) if isinstance(snapshot, dict) else 0
        meta_cycle = int(meta["cycle"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not bound to current cycle meta") from exc
    if (
        digest != canonical_sha256(snapshot)
        or not isinstance(bindings, dict)
        or bindings.get("meta_sha256") != canonical_sha256(meta)
        or snapshot_cycle != meta_cycle
        or snapshot.get("as_of_ts") != meta.get("now")
    ):
        raise ValueError(f"{label} is not bound to current cycle meta")
    return snapshot


def _validate_preseal_performance(pending: Path, meta: dict) -> tuple[dict, str]:
    try:
        snapshot = json.loads((pending / "performance_snapshot.json").read_text())
        sidecar = (pending / "performance_snapshot.sha256").read_text().strip()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("pre-seal performance packet is missing or malformed") from exc
    digest = canonical_sha256(snapshot)
    if sidecar != digest:
        raise ValueError("pre-seal performance packet is not bound to current cycle meta")
    _validate_performance_binding(snapshot, digest, meta, label="pre-seal performance packet")
    return snapshot, digest


def _sealed_meta(base_meta: dict, provenance: dict, preseal_digest: str) -> dict:
    return {
        **base_meta,
        "pre_reflection_performance_snapshot_artifact": (PRE_REFLECTION_PERFORMANCE_ARTIFACT),
        "pre_reflection_performance_snapshot_sha256": preseal_digest,
        "decision_start_runtime_provenance_artifact": (DECISION_START_PROVENANCE_ARTIFACT),
        "decision_start_runtime_provenance_sha256": canonical_sha256(provenance),
        "decision_start_runtime_provenance_captured_at": provenance["captured_at"],
    }


def _build_performance_for_meta(
    *,
    state_dir: str,
    memory_dir: str,
    pending: Path,
    meta: dict,
    starting_capital: float,
) -> dict:
    """Build against an unpublished meta without modifying canonical cycle artifacts."""
    with TemporaryDirectory(
        prefix=f".decision-start-{int(meta['cycle'])}-", dir=pending.parent
    ) as temporary_name:
        shadow = Path(temporary_name)
        for name in ("evidence.json", "risk_model.json"):
            shutil.copyfile(pending / name, shadow / name)
        (shadow / "meta.json").write_text(json.dumps(meta))
        return build_performance_snapshot(
            state_dir,
            memory_dir,
            shadow,
            cycle=int(meta["cycle"]),
            as_of_ts=meta["now"],
            starting_capital=starting_capital,
            require_cycle_meta=True,
        )


def _new_transaction(
    *,
    state_dir: str,
    memory_dir: str,
    pending: Path,
    base_meta: dict,
    captured_at: datetime | None,
) -> dict:
    if any(field in base_meta for field in _SEAL_META_FIELDS):
        raise ValueError("partial or preexisting decision-start binding in cycle meta")
    pre_reflection_performance, preseal_digest = _validate_preseal_performance(pending, base_meta)
    sealed_at = captured_at or datetime.now(UTC)
    if sealed_at.tzinfo is None:
        raise ValueError("decision-start seal timestamp must be timezone-aware")
    sealed_at = sealed_at.astimezone(UTC)
    evidence_at = datetime.fromisoformat(str(base_meta["now"]))
    if evidence_at.tzinfo is None:
        raise ValueError("cycle evidence timestamp must be timezone-aware")
    if sealed_at < evidence_at.astimezone(UTC):
        raise ValueError("decision-start seal cannot predate cycle evidence")
    provenance = default_runtime_provenance(captured_at=sealed_at)
    final_meta = _sealed_meta(base_meta, provenance, preseal_digest)
    settings = load_settings()
    snapshot = _build_performance_for_meta(
        state_dir=state_dir,
        memory_dir=memory_dir,
        pending=pending,
        meta=final_meta,
        starting_capital=settings.account_size_usdt,
    )
    snapshot_digest = canonical_sha256(snapshot)
    _validate_performance_binding(
        snapshot, snapshot_digest, final_meta, label="rebuilt performance packet"
    )
    body = {
        "schema_version": _TRANSACTION_SCHEMA_VERSION,
        "kind": "decision_start",
        "paper_only": True,
        "cycle": int(base_meta["cycle"]),
        "base_meta": base_meta,
        "base_meta_sha256": canonical_sha256(base_meta),
        "sealed_meta": final_meta,
        "sealed_meta_sha256": canonical_sha256(final_meta),
        "pre_reflection_performance_snapshot": pre_reflection_performance,
        "pre_reflection_performance_snapshot_sha256": preseal_digest,
        "runtime_provenance": provenance,
        "runtime_provenance_sha256": canonical_sha256(provenance),
        "performance_snapshot": snapshot,
        "performance_snapshot_sha256": snapshot_digest,
    }
    return {**body, "intent_sha256": canonical_sha256(body)}


def _load_transaction(path: Path, *, cycle: int) -> dict:
    try:
        transaction = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("decision-start transaction is missing or malformed") from exc
    if not isinstance(transaction, dict) or set(transaction) != _TRANSACTION_FIELDS:
        raise ValueError("decision-start transaction has an invalid schema")
    body = dict(transaction)
    intent_sha256 = body.pop("intent_sha256", None)
    if not isinstance(intent_sha256, str) or canonical_sha256(body) != intent_sha256:
        raise ValueError("decision-start transaction hash mismatch")
    if (
        type(transaction["schema_version"]) is not int
        or transaction["schema_version"] != _TRANSACTION_SCHEMA_VERSION
        or transaction["kind"] != "decision_start"
        or transaction["paper_only"] is not True
        or type(transaction["cycle"]) is not int
        or transaction["cycle"] != cycle
    ):
        raise ValueError("decision-start transaction identity mismatch")

    base_meta = transaction["base_meta"]
    final_meta = transaction["sealed_meta"]
    provenance = transaction["runtime_provenance"]
    preseal = transaction["pre_reflection_performance_snapshot"]
    snapshot = transaction["performance_snapshot"]
    artifacts = (base_meta, final_meta, provenance, preseal, snapshot)
    if not all(isinstance(value, dict) for value in artifacts):
        raise ValueError("decision-start transaction contains a non-object artifact")
    try:
        base_cycle = int(base_meta.get("cycle", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("decision-start transaction has an invalid base meta") from exc
    if base_cycle != cycle or any(field in base_meta for field in _SEAL_META_FIELDS):
        raise ValueError("decision-start transaction has an invalid base meta")
    if canonical_sha256(base_meta) != transaction["base_meta_sha256"]:
        raise ValueError("decision-start transaction base meta hash mismatch")
    if canonical_sha256(provenance) != transaction["runtime_provenance_sha256"]:
        raise ValueError("decision-start transaction provenance hash mismatch")
    if (
        not verify_runtime_provenance(provenance)
        or provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION
    ):
        raise ValueError("decision-start transaction provenance is invalid")
    preseal_digest = transaction["pre_reflection_performance_snapshot_sha256"]
    if canonical_sha256(preseal) != preseal_digest:
        raise ValueError("decision-start transaction pre-reflection packet hash mismatch")
    _validate_performance_binding(
        preseal, preseal_digest, base_meta, label="transaction pre-reflection packet"
    )
    expected_meta = _sealed_meta(base_meta, provenance, preseal_digest)
    if (
        final_meta != expected_meta
        or canonical_sha256(final_meta) != transaction["sealed_meta_sha256"]
    ):
        raise ValueError("decision-start transaction sealed meta mismatch")
    snapshot_digest = transaction["performance_snapshot_sha256"]
    if canonical_sha256(snapshot) != snapshot_digest:
        raise ValueError("decision-start transaction performance packet hash mismatch")
    _validate_performance_binding(
        snapshot, snapshot_digest, final_meta, label="transaction performance packet"
    )
    try:
        sealed_at = datetime.fromisoformat(str(provenance["captured_at"]))
        evidence_at = datetime.fromisoformat(str(base_meta["now"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("decision-start transaction timestamp is invalid") from exc
    if sealed_at.tzinfo is None or evidence_at.tzinfo is None:
        raise ValueError("decision-start transaction timestamps must be timezone-aware")
    if sealed_at.astimezone(UTC) < evidence_at.astimezone(UTC):
        raise ValueError("decision-start transaction predates cycle evidence")
    return transaction


def _read_json_or_conflict(path: Path, *, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"conflicting {label} artifact") from exc
    if not isinstance(value, dict):
        raise ValueError(f"conflicting {label} artifact")
    return value


def _preflight_replay(pending: Path, current_meta: dict, transaction: dict) -> None:
    base_meta = transaction["base_meta"]
    final_meta = transaction["sealed_meta"]
    if current_meta not in (base_meta, final_meta):
        raise ValueError("cycle meta conflicts with decision-start transaction")
    json_expectations = {
        PRE_REFLECTION_PERFORMANCE_ARTIFACT: (transaction["pre_reflection_performance_snapshot"],),
        DECISION_START_PROVENANCE_ARTIFACT: (transaction["runtime_provenance"],),
        "performance_snapshot.json": (
            transaction["pre_reflection_performance_snapshot"],
            transaction["performance_snapshot"],
        ),
    }
    for name, allowed in json_expectations.items():
        path = pending / name
        if path.exists() and _read_json_or_conflict(path, label=name) not in allowed:
            raise ValueError(f"conflicting {name} artifact")
    text_expectations = {
        PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT: {
            transaction["pre_reflection_performance_snapshot_sha256"]
        },
        "performance_snapshot.sha256": {
            transaction["pre_reflection_performance_snapshot_sha256"],
            transaction["performance_snapshot_sha256"],
        },
    }
    for name, allowed in text_expectations.items():
        path = pending / name
        if path.exists() and path.read_text().strip() not in allowed:
            raise ValueError(f"conflicting {name} artifact")


def _write_json_if_needed(path: Path, expected: dict) -> None:
    if path.exists() and _read_json_or_conflict(path, label=path.name) == expected:
        return
    durable_write_json(path, expected)


def _write_digest_if_needed(path: Path, expected: str) -> None:
    if path.exists() and path.read_text().strip() == expected:
        return
    durable_write_text(path, expected + "\n")


def _current_build_matches(provenance: dict) -> bool:
    try:
        captured_at = datetime.fromisoformat(str(provenance["captured_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    if captured_at.tzinfo is None:
        return False
    current = default_runtime_provenance(captured_at=captured_at.astimezone(UTC))
    return canonical_sha256(current) == canonical_sha256(provenance)


def _result(pending: Path, transaction: dict) -> dict:
    provenance = transaction["runtime_provenance"]
    return {
        "cycle": int(transaction["cycle"]),
        "pending_dir": str(pending),
        "captured_at": provenance["captured_at"],
        "runtime_provenance_sha256": transaction["runtime_provenance_sha256"],
        "source_tree_sha256": provenance["source_tree_sha256"],
        "prompt_bundle_sha256": provenance["prompt_bundle_sha256"],
        "pre_reflection_performance_snapshot_sha256": transaction[
            "pre_reflection_performance_snapshot_sha256"
        ],
        "performance_snapshot_sha256": transaction["performance_snapshot_sha256"],
    }


def _replay_transaction(
    *,
    state_dir: str,
    memory_dir: str,
    pending: Path,
    current_meta: dict,
    transaction_path: Path,
    transaction: dict,
) -> dict:
    _preflight_replay(pending, current_meta, transaction)
    if not _current_build_matches(transaction["runtime_provenance"]):
        raise ValueError("runtime build changed after decision-start transaction was prepared")

    _write_json_if_needed(
        pending / PRE_REFLECTION_PERFORMANCE_ARTIFACT,
        transaction["pre_reflection_performance_snapshot"],
    )
    _write_digest_if_needed(
        pending / PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT,
        transaction["pre_reflection_performance_snapshot_sha256"],
    )
    _write_json_if_needed(
        pending / DECISION_START_PROVENANCE_ARTIFACT,
        transaction["runtime_provenance"],
    )
    _write_json_if_needed(pending / "meta.json", transaction["sealed_meta"])
    _write_json_if_needed(
        pending / "performance_snapshot.json", transaction["performance_snapshot"]
    )
    _write_digest_if_needed(
        pending / "performance_snapshot.sha256",
        transaction["performance_snapshot_sha256"],
    )

    final_meta = _read_json_or_conflict(pending / "meta.json", label="meta.json")
    load_bound_decision_start_provenance(pending, final_meta, require_current_match=True)
    load_bound_pre_reflection_performance(pending, final_meta)
    settings = load_settings()
    rebuilt = build_performance_snapshot(
        state_dir,
        memory_dir,
        pending,
        cycle=int(final_meta["cycle"]),
        as_of_ts=final_meta["now"],
        starting_capital=settings.account_size_usdt,
        require_cycle_meta=True,
    )
    if canonical_sha256(rebuilt) != transaction["performance_snapshot_sha256"]:
        raise ValueError("rebuilt performance packet changed during decision-start transaction")
    _validate_preseal_performance(pending, final_meta)
    if not _current_build_matches(transaction["runtime_provenance"]):
        raise ValueError("runtime build changed during decision-start transaction")
    durable_unlink(transaction_path)
    return _result(pending, transaction)


def _verify_completed_seal(*, state_dir: str, memory_dir: str, pending: Path, meta: dict) -> dict:
    provenance = load_bound_decision_start_provenance(pending, meta, require_current_match=True)
    preseal = load_bound_pre_reflection_performance(pending, meta)
    try:
        snapshot = json.loads((pending / "performance_snapshot.json").read_text())
        sidecar = (pending / "performance_snapshot.sha256").read_text().strip()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("sealed performance packet is missing or malformed") from exc
    snapshot_digest = canonical_sha256(snapshot)
    if sidecar != snapshot_digest:
        raise ValueError("sealed performance packet hash mismatch")
    _validate_performance_binding(
        snapshot, snapshot_digest, meta, label="sealed performance packet"
    )
    settings = load_settings()
    rebuilt = build_performance_snapshot(
        state_dir,
        memory_dir,
        pending,
        cycle=int(meta["cycle"]),
        as_of_ts=meta["now"],
        starting_capital=settings.account_size_usdt,
        require_cycle_meta=True,
    )
    if canonical_sha256(rebuilt) != snapshot_digest:
        raise ValueError("sealed performance packet no longer matches current PAPER state")
    transaction = {
        "cycle": int(meta["cycle"]),
        "runtime_provenance": provenance,
        "runtime_provenance_sha256": canonical_sha256(provenance),
        "pre_reflection_performance_snapshot_sha256": canonical_sha256(preseal),
        "performance_snapshot_sha256": snapshot_digest,
    }
    return _result(pending, transaction)


def seal_decision_start(
    *,
    state_dir: str,
    memory_dir: str,
    agents_dir: str,
    captured_at: datetime | None = None,
) -> dict:
    """Seal or idempotently verify one post-reflection decision-start generation."""
    pending, meta = resolve_pending(memory_dir)
    downstream = sorted(name for name in _DOWNSTREAM_ARTIFACTS if (pending / name).exists())
    if downstream:
        raise ValueError(
            "decision-start provenance must precede every specialist/decision artifact: "
            + ", ".join(downstream)
        )
    _validate_reflector_heads(
        agents_dir=agents_dir,
        memory_dir=memory_dir,
        state_dir=state_dir,
    )

    present = [field for field in _SEAL_META_FIELDS if field in meta]
    provenance_path = pending / DECISION_START_PROVENANCE_ARTIFACT
    transaction_path = pending / DECISION_START_TRANSACTION_ARTIFACT
    if transaction_path.exists():
        transaction = _load_transaction(transaction_path, cycle=int(meta["cycle"]))
        return _replay_transaction(
            state_dir=state_dir,
            memory_dir=memory_dir,
            pending=pending,
            current_meta=meta,
            transaction_path=transaction_path,
            transaction=transaction,
        )
    if present:
        if len(present) != len(_SEAL_META_FIELDS):
            raise ValueError("partial decision-start provenance binding in cycle meta")
        return _verify_completed_seal(
            state_dir=state_dir, memory_dir=memory_dir, pending=pending, meta=meta
        )

    pre_reflection_path = pending / PRE_REFLECTION_PERFORMANCE_ARTIFACT
    pre_reflection_sidecar = pending / PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT
    conflicting = [
        path.name
        for path in (provenance_path, pre_reflection_path, pre_reflection_sidecar)
        if path.exists()
    ]
    if conflicting:
        raise ValueError(
            "unbound decision-start provenance artifact already exists: " + ", ".join(conflicting)
        )
    transaction = _new_transaction(
        state_dir=state_dir,
        memory_dir=memory_dir,
        pending=pending,
        base_meta=meta,
        captured_at=captured_at,
    )
    durable_write_json(transaction_path, transaction)
    return _replay_transaction(
        state_dir=state_dir,
        memory_dir=memory_dir,
        pending=pending,
        current_meta=meta,
        transaction_path=transaction_path,
        transaction=transaction,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--memory-dir", default="live_memory")
    parser.add_argument("--agents-dir", default="agents")
    args = parser.parse_args(argv)
    try:
        result = seal_decision_start(
            state_dir=args.state_dir,
            memory_dir=args.memory_dir,
            agents_dir=args.agents_dir,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"decision_start_seal": "FAILED", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"decision_start_seal": "SEALED", **result}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
