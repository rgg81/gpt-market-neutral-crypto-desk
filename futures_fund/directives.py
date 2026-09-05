"""Crash-safe lifecycle for one-shot, typed desk directives.

The only production inbox is ``ops/next-cycle-directive.md`` beside ``live_state``.  Evidence
atomically moves one concrete inbox entry into state-owned storage before reading it.  A completed
cycle later consumes only that UUID-bound claim; cleanup never unlinks the inbox pathname.  This
keeps a concurrently queued (even byte-identical) instruction distinct and removes path/receipt
authority from deletion decisions.

The lifecycle is a crash/retry boundary for one trusted local desk account, not a sandbox against
a hostile process that can concurrently rewrite repository or state-directory ancestors. Such a
process already has authority to alter desk code, prompts, and paper-account artifacts.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from uuid import uuid4

from futures_fund.durable_io import (
    canonical_json_sha256,
    durable_unlink,
    durable_write_json,
    fsync_directory,
)

CONTROLLED_RESTART_GRADUATION = "controlled_restart_graduation"
KNOWN_DIRECTIVE_CAPABILITIES = frozenset({CONTROLLED_RESTART_GRADUATION})
_HEADER = re.compile(r"^<!-- desk-directive-capabilities: (\[.*\]) -->$")

DIRECTIVE_SOURCE_RELPATH = PurePosixPath("ops/next-cycle-directive.md")
DIRECTIVE_CLAIM_ROOT = "directive-claims-v1"
DIRECTIVE_CLAIM_SCHEMA_VERSION = 1
DIRECTIVE_RECEIPT_ARTIFACT = "binding_user_directive"
DIRECTIVE_RECEIPT_SCHEMA_VERSION = 2
DIRECTIVE_CONSUMPTION_SCHEMA_VERSION = 1
DIRECTIVE_COMMIT_EXPECTATION_SCHEMA_VERSION = 1
_CLAIM_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_DUPLICATE_SUFFIX = ".source-duplicate"
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


@dataclass(frozen=True)
class DirectiveContext:
    """All lifecycle paths derived from a state directory, never from a receipt."""

    repo_root: Path
    state_dir: Path
    source_path: Path
    claim_root: Path
    active_path: Path

    @classmethod
    def from_state_dir(cls, state_dir: str | Path) -> DirectiveContext:
        state = Path(os.path.abspath(os.fspath(state_dir)))
        repo_root = state.parent
        source = repo_root.joinpath(*DIRECTIVE_SOURCE_RELPATH.parts)
        claim_root = state / DIRECTIVE_CLAIM_ROOT
        return cls(
            repo_root=repo_root,
            state_dir=state,
            source_path=source,
            claim_root=claim_root,
            active_path=claim_root / "active.json",
        )

    def validate_source_argument(self, path: str | Path) -> None:
        supplied = Path(os.path.abspath(os.fspath(path)))
        if supplied != self.source_path:
            raise ValueError(
                "directive source must be the canonical repository inbox: "
                f"{self.source_path}"
            )


def parse_directive_capabilities(text: str) -> tuple[str, ...]:
    """Parse the exact optional first-line capability header, failing closed if reserved."""

    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    reserved_prefix = "<!-- desk-directive-capabilities:"
    if not first_line.startswith(reserved_prefix):
        return ()
    match = _HEADER.fullmatch(first_line)
    if match is None:
        raise ValueError("malformed desk-directive-capabilities header")
    try:
        raw = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("desk directive capabilities must be a JSON string list") from exc
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, str) or not item for item in raw)
        or len(raw) != len(set(raw))
    ):
        raise ValueError("desk directive capabilities must be unique non-empty strings")
    unknown = sorted(set(raw) - KNOWN_DIRECTIVE_CAPABILITIES)
    if unknown:
        raise ValueError("unknown desk directive capabilities: " + ", ".join(unknown))
    if raw != sorted(raw):
        raise ValueError("desk directive capabilities must use canonical sorted order")
    return tuple(raw)


def _reject_symlink_components(path: Path, *, allow_missing_leaf: bool = True) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return
            # Missing descendants cannot themselves be symlinks. Their parent was checked.
            return
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"directive lifecycle path contains a symlink: {current}")


def _require_directory_if_present(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"directive lifecycle requires a real directory: {path}")


def _validate_context_paths(context: DirectiveContext) -> None:
    """Reject redirected lifecycle roots before any lock, creation, or cleanup.

    The inbox payload itself is allowed to be absent, but every existing ancestor and every
    state-owned lifecycle directory must be a real (non-symlink) directory.  Final files are read
    with ``O_NOFOLLOW`` below.  Mutation entry points repeat this check after taking the state
    transaction lock so ordinary cooperating writers cannot exchange a checked path underneath
    the transaction.
    """

    for path in (
        context.repo_root,
        context.state_dir,
        context.source_path.parent,
        context.claim_root,
        context.claim_root / "consumptions",
    ):
        _reject_symlink_components(path)
        _require_directory_if_present(path)


def _source_present(context: DirectiveContext) -> bool:
    try:
        info = os.lstat(context.source_path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(
            f"directive lifecycle requires a regular non-symlink inbox: {context.source_path}"
        )
    return True


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_aware_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_capability_list(value: object) -> bool:
    return bool(
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and value == sorted(value)
        and len(value) == len(set(value))
        and set(value).issubset(KNOWN_DIRECTIVE_CAPABILITIES)
    )


def _require_cycle(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("directive lifecycle cycle must be a positive integer")
    return value


def _claim_payload_names(context: DirectiveContext) -> set[str]:
    if not context.claim_root.exists():
        return set()
    names: set[str] = set()
    with os.scandir(context.claim_root) as entries:
        for entry in entries:
            recognized = any(
                entry.name.endswith(suffix)
                and _CLAIM_ID.fullmatch(entry.name.removesuffix(suffix)) is not None
                for suffix in (".md", ".consuming", _SOURCE_DUPLICATE_SUFFIX)
            )
            if recognized:
                names.add(entry.name)
    return names


def _validate_claim_inventory(context: DirectiveContext, intent: dict | None) -> None:
    present = _claim_payload_names(context)
    allowed: set[str] = set()
    if intent is not None:
        claim_id = intent["claim_id"]
        allowed = {
            f"{claim_id}.md",
            f"{claim_id}.consuming",
            f"{claim_id}{_SOURCE_DUPLICATE_SUFFIX}",
        }
    unexpected = sorted(present - allowed)
    if unexpected:
        raise RuntimeError(
            "directive lifecycle found orphaned state-owned payloads: " + ", ".join(unexpected)
        )


def _fingerprint(info: os.stat_result) -> dict[str, int]:
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
    }


def _safe_read_regular(path: Path, *, sync: bool = False) -> tuple[bytes, dict[str, int]]:
    """Read one stable regular-file instance without following its final symlink."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"directive lifecycle refuses a symlink: {path}") from exc
        raise
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"directive lifecycle requires a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if sync:
            # Persist the producer's bytes before any directory fsync can make their removal from
            # the inbox durable. Reading alone may have populated only the page cache.
            os.fsync(descriptor)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_fp = _fingerprint(before)
    after_fp = _fingerprint(after)
    if before_fp != after_fp:
        raise RuntimeError(f"directive file changed while it was being read: {path}")
    return b"".join(chunks), before_fp


def _decode_payload(payload: bytes) -> tuple[str, tuple[str, ...]]:
    if not payload:
        raise ValueError("binding directive is empty")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("binding directive must be strict UTF-8") from exc
    if not text.strip():
        raise ValueError("binding directive is empty")
    return text, parse_directive_capabilities(text)


def _claim_path(context: DirectiveContext, claim_id: str) -> Path:
    if _CLAIM_ID.fullmatch(claim_id) is None:
        raise ValueError("invalid directive claim id")
    return context.claim_root / f"{claim_id}.md"


def _tomb_path(context: DirectiveContext, claim_id: str) -> Path:
    return context.claim_root / f"{claim_id}.consuming"


def _source_duplicate_path(context: DirectiveContext, claim_id: str) -> Path:
    return context.claim_root / f"{claim_id}{_SOURCE_DUPLICATE_SUFFIX}"


def _consumption_path(context: DirectiveContext, cycle: int) -> Path:
    return context.claim_root / "consumptions" / f"cycle-{int(cycle)}.json"


def _seal_intent(body: dict) -> dict:
    return {**body, "claim_intent_sha256": canonical_json_sha256(body)}


def _validate_intent(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("directive claim intent must be a JSON object")
    required = {
        "schema_version",
        "paper_only",
        "claim_id",
        "cycle",
        "created_at",
        "source_relpath",
        "source_fingerprint",
        "payload_sha256",
        "directive_sha256",
        "capabilities",
        "capabilities_sha256",
        "claim_intent_sha256",
    }
    if set(value) != required:
        raise ValueError("directive claim intent has an invalid field set")
    body = dict(value)
    digest = body.pop("claim_intent_sha256")
    fingerprint = body.get("source_fingerprint")
    claim_id = body.get("claim_id")
    if (
        type(body.get("schema_version")) is not int
        or body.get("schema_version") != DIRECTIVE_CLAIM_SCHEMA_VERSION
        or body.get("paper_only") is not True
        or not isinstance(claim_id, str)
        or _CLAIM_ID.fullmatch(claim_id) is None
        or type(body.get("cycle")) is not int
        or body["cycle"] < 1
        or body.get("source_relpath") != DIRECTIVE_SOURCE_RELPATH.as_posix()
        or not _is_aware_timestamp(body.get("created_at"))
        or not isinstance(fingerprint, dict)
        or set(fingerprint) != {"device", "inode", "size", "mtime_ns", "ctime_ns"}
        or any(type(item) is not int for item in fingerprint.values())
        or any(item < 0 for item in fingerprint.values())
        or not _is_sha256(body.get("payload_sha256"))
        or not _is_sha256(body.get("directive_sha256"))
        or not _valid_capability_list(body.get("capabilities"))
        or not _is_sha256(body.get("capabilities_sha256"))
        or body.get("capabilities_sha256")
        != canonical_json_sha256(body.get("capabilities"))
        or not _is_sha256(digest)
        or digest != canonical_json_sha256(body)
    ):
        raise ValueError("directive claim intent identity/hash mismatch")
    return dict(value)


def _read_json_regular(path: Path) -> dict:
    payload, _fingerprint_value = _safe_read_regular(path)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid directive lifecycle JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"directive lifecycle JSON is not an object: {path}")
    return value


def _load_intent(context: DirectiveContext) -> dict | None:
    try:
        return _validate_intent(_read_json_regular(context.active_path))
    except FileNotFoundError:
        return None


def _claim_from_payload(context: DirectiveContext, intent: dict, path: Path) -> dict:
    payload, fingerprint = _safe_read_regular(path)
    text, capabilities = _decode_payload(payload)
    source_fp = intent["source_fingerprint"]
    # Rename may update ctime. Device/inode/size/mtime plus both exact hashes bind the instance.
    stable_identity = all(
        fingerprint[field] == source_fp[field]
        for field in ("device", "inode", "size", "mtime_ns")
    )
    if (
        not stable_identity
        or sha256(payload).hexdigest() != intent["payload_sha256"]
        or canonical_json_sha256(text) != intent["directive_sha256"]
        or list(capabilities) != intent["capabilities"]
        or canonical_json_sha256(list(capabilities)) != intent["capabilities_sha256"]
    ):
        raise RuntimeError("claimed directive payload does not match its durable intent")
    return {**intent, "text": text}


def build_directive_commit_expectation(claim: dict | None, *, cycle: int) -> dict:
    """Bind explicit claim absence or one exact active claim into a reconcile intent."""

    cycle = _require_cycle(cycle)
    expectation = {
        "schema_version": DIRECTIVE_COMMIT_EXPECTATION_SCHEMA_VERSION,
        "paper_only": True,
        "present": claim is not None,
        "cycle": cycle,
    }
    if claim is None:
        return expectation
    intent = _validate_intent({key: value for key, value in claim.items() if key != "text"})
    if intent["cycle"] != cycle:
        raise ValueError("directive commit expectation cycle does not match its claim")
    return {
        **expectation,
        "claim_id": intent["claim_id"],
        "claim_intent_sha256": intent["claim_intent_sha256"],
        "source_relpath": intent["source_relpath"],
        "payload_sha256": intent["payload_sha256"],
        "directive_sha256": intent["directive_sha256"],
        "capabilities": list(intent["capabilities"]),
        "capabilities_sha256": intent["capabilities_sha256"],
    }


def _validate_directive_commit_expectation(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("directive commit expectation must be a JSON object")
    present = value.get("present")
    required = {"schema_version", "paper_only", "present", "cycle"}
    if present is True:
        required |= {
            "claim_id",
            "claim_intent_sha256",
            "source_relpath",
            "payload_sha256",
            "directive_sha256",
            "capabilities",
            "capabilities_sha256",
        }
    if (
        set(value) != required
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != DIRECTIVE_COMMIT_EXPECTATION_SCHEMA_VERSION
        or value.get("paper_only") is not True
        or type(present) is not bool
        or type(value.get("cycle")) is not int
        or value["cycle"] < 1
        or (
            present is True
            and (
                not isinstance(value.get("claim_id"), str)
                or _CLAIM_ID.fullmatch(value["claim_id"]) is None
                or not _is_sha256(value.get("claim_intent_sha256"))
                or value.get("source_relpath") != DIRECTIVE_SOURCE_RELPATH.as_posix()
                or not _is_sha256(value.get("payload_sha256"))
                or not _is_sha256(value.get("directive_sha256"))
                or not _valid_capability_list(value.get("capabilities"))
                or not _is_sha256(value.get("capabilities_sha256"))
                or value.get("capabilities_sha256")
                != canonical_json_sha256(value.get("capabilities"))
            )
        )
    ):
        raise ValueError("directive commit expectation is malformed")
    return dict(value)


def validate_directive_commit_expectation(
    state_dir: str | Path,
    expectation: object,
    *,
    allow_consumed: bool = False,
) -> dict:
    """Read-only proof that state still matches an explicit reconcile expectation.

    Reconcile calls this while holding the exclusive state transaction lock. Callers outside that
    lock receive a point-in-time validation only.
    """

    expected = _validate_directive_commit_expectation(expectation)
    context = DirectiveContext.from_state_dir(state_dir)
    _validate_context_paths(context)
    intent = _load_intent(context)
    _validate_completed_consumptions(
        context,
        allow_unacknowledged_cycle=int(intent["cycle"]) if intent is not None else None,
    )
    _validate_claim_inventory(context, intent)
    if expected["present"] is False:
        if intent is not None:
            raise RuntimeError("reconcile expected no directive but an active claim exists")
        return expected
    if intent is None:
        if allow_consumed:
            receipt = load_completed_directive_receipt(
                context.state_dir,
                int(expected["cycle"]),
            )
            if receipt is not None:
                bound_fields = (
                    "claim_id",
                    "claim_intent_sha256",
                    "source_relpath",
                    "payload_sha256",
                    "directive_sha256",
                    "capabilities",
                    "capabilities_sha256",
                )
                if all(receipt[field] == expected[field] for field in bound_fields):
                    return expected
        raise RuntimeError("reconcile expected an active directive claim but none exists")
    bound_fields = (
        "claim_id",
        "claim_intent_sha256",
        "source_relpath",
        "payload_sha256",
        "directive_sha256",
        "capabilities",
        "capabilities_sha256",
    )
    if intent["cycle"] != expected["cycle"] or any(
        intent[field] != expected[field] for field in bound_fields
    ):
        raise RuntimeError("active directive claim changed before reconcile commit")
    claim_path = _claim_path(context, intent["claim_id"])
    tomb_path = _tomb_path(context, intent["claim_id"])
    duplicate_path = _source_duplicate_path(context, intent["claim_id"])
    if (
        not claim_path.exists()
        or claim_path.is_symlink()
        or tomb_path.exists()
        or tomb_path.is_symlink()
        or duplicate_path.exists()
        or duplicate_path.is_symlink()
    ):
        raise RuntimeError("active directive payload is not commit-ready")
    _claim_from_payload(context, intent, claim_path)
    return expected


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without ever replacing a concurrently created destination."""

    if _RENAMEAT2 is None:
        raise RuntimeError("directive lifecycle requires Linux renameat2(RENAME_NOREPLACE)")
    ctypes.set_errno(0)
    result = _RENAMEAT2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            f"directive lifecycle destination already exists: {destination}",
            destination,
        )
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise RuntimeError(
            "filesystem lacks atomic renameat2(RENAME_NOREPLACE); preserving both paths"
        )
    raise OSError(error_number, os.strerror(error_number), source)


def _rename_durable(source: Path, destination: Path) -> None:
    if os.stat(source.parent).st_dev != os.stat(destination.parent).st_dev:
        raise RuntimeError("directive source and claim storage must share one filesystem")
    _rename_noreplace(source, destination)
    # Anchor the surviving name before making removal of the source name durable. A crash between
    # these fsyncs may leave both names, which is recoverable; it must never durably lose both.
    fsync_directory(destination.parent)
    if source.parent != destination.parent:
        fsync_directory(source.parent)


def _restore_mismatched_claim(context: DirectiveContext, claim_path: Path) -> None:
    if context.source_path.exists() or context.source_path.is_symlink():
        raise RuntimeError(
            "directive claim conflicts with a newly queued source; both files were preserved"
        )
    _rename_durable(claim_path, context.source_path)
    durable_unlink(context.active_path)


def _path_is_claim_instance(context: DirectiveContext, path: Path, intent: dict) -> bool:
    """Return whether a regular path is the original claimed inode, validating it if so."""

    _payload, fingerprint = _safe_read_regular(path)
    source_fingerprint = intent["source_fingerprint"]
    same_inode = all(
        fingerprint[field] == source_fingerprint[field] for field in ("device", "inode")
    )
    if same_inode:
        _claim_from_payload(context, intent, path)
    return same_inode


def _recover_duplicate_source(
    context: DirectiveContext,
    intent: dict,
    claim_path: Path,
) -> None:
    """Durably retire only a crash-restored second name for the exact claimed inode.

    Destination-first directory fsync makes data loss impossible but permits the old inbox name to
    reappear after power loss. A different inode is a real queued directive and remains untouched.
    The same inode is first moved to a UUID-bound state quarantine, revalidated, and only then
    unlinked. Thus a concurrent replacement can be restored/preserved rather than deleted.
    """

    _claim_from_payload(context, intent, claim_path)
    duplicate_path = _source_duplicate_path(context, intent["claim_id"])
    if duplicate_path.exists() or duplicate_path.is_symlink():
        if duplicate_path.is_symlink() or not _path_is_claim_instance(
            context, duplicate_path, intent
        ):
            raise RuntimeError("directive source-duplicate quarantine is not the claimed instance")
        if _source_present(context):
            source_is_duplicate = _path_is_claim_instance(
                context, context.source_path, intent
            )
            # Removing the state-owned quarantine is safe. If the old inbox name also survived,
            # the loop below moves it back into a fresh quarantine before deleting anything else.
            durable_unlink(duplicate_path)
            if not source_is_duplicate:
                return
        else:
            durable_unlink(duplicate_path)
            return

    if not _source_present(context):
        return
    if not _path_is_claim_instance(context, context.source_path, intent):
        return
    _rename_durable(context.source_path, duplicate_path)
    try:
        if not _path_is_claim_instance(context, duplicate_path, intent):
            raise RuntimeError("quarantined inbox is not the claimed instance")
    except Exception as mismatch:
        try:
            _rename_durable(duplicate_path, context.source_path)
        except Exception as restore_error:
            raise RuntimeError(
                "directive source changed during duplicate recovery; both paths were preserved"
            ) from restore_error
        raise RuntimeError(
            "directive source changed during duplicate recovery and was restored"
        ) from mismatch
    durable_unlink(duplicate_path)


def _materialize_claim(context: DirectiveContext, intent: dict) -> dict:
    claim_path = _claim_path(context, intent["claim_id"])
    tomb_path = _tomb_path(context, intent["claim_id"])
    duplicate_path = _source_duplicate_path(context, intent["claim_id"])
    if tomb_path.exists() or tomb_path.is_symlink():
        raise RuntimeError("uncommitted directive claim unexpectedly has a consuming tombstone")
    if claim_path.exists() or claim_path.is_symlink():
        claim = _claim_from_payload(context, intent, claim_path)
        _recover_duplicate_source(context, intent, claim_path)
        return claim
    if duplicate_path.exists() or duplicate_path.is_symlink():
        raise RuntimeError("directive source-duplicate quarantine exists without its claim")
    if not context.source_path.exists() and not context.source_path.is_symlink():
        raise RuntimeError("prepared directive claim lost both source and claimed payload")
    _reject_symlink_components(context.source_path, allow_missing_leaf=False)
    _rename_durable(context.source_path, claim_path)
    try:
        return _claim_from_payload(context, intent, claim_path)
    except Exception:
        _restore_mismatched_claim(context, claim_path)
        raise


def _new_claim_intent(
    context: DirectiveContext,
    *,
    cycle: int,
    now: datetime,
) -> dict:
    _reject_symlink_components(context.source_path, allow_missing_leaf=False)
    payload, fingerprint = _safe_read_regular(context.source_path, sync=True)
    text, capabilities = _decode_payload(payload)
    # Persist the producer's directory entry before publishing an intent that depends on it. A
    # durable active intent must never be the sole survivor of a crash while both source and claim
    # names were still only in directory cache. Re-open after the directory barrier so the intent
    # cannot bind a source that changed during that barrier.
    fsync_directory(context.source_path.parent)
    persisted_payload, persisted_fingerprint = _safe_read_regular(context.source_path)
    if persisted_payload != payload or persisted_fingerprint != fingerprint:
        raise RuntimeError("directive inbox changed while its durable claim was being prepared")
    # Anchor the direct-child names for both ops/ and live_state/ on a first-ever desk before an
    # active intent or removal of the inbox can become durable inside those children.
    fsync_directory(context.repo_root)
    body = {
        "schema_version": DIRECTIVE_CLAIM_SCHEMA_VERSION,
        "paper_only": True,
        "claim_id": uuid4().hex,
        "cycle": int(cycle),
        "created_at": now.astimezone(UTC).isoformat(),
        "source_relpath": DIRECTIVE_SOURCE_RELPATH.as_posix(),
        "source_fingerprint": fingerprint,
        "payload_sha256": sha256(payload).hexdigest(),
        "directive_sha256": canonical_json_sha256(text),
        "capabilities": list(capabilities),
        "capabilities_sha256": canonical_json_sha256(list(capabilities)),
    }
    return _seal_intent(body)


def claim_next_directive(
    state_dir: str | Path,
    *,
    cycle: int,
    now: datetime | None = None,
    source_path: str | Path | None = None,
) -> dict | None:
    """Atomically claim the canonical inbox entry, or replay the same failed-cycle claim."""

    from futures_fund.state_transaction import exclusive_state_transaction_lock

    cycle = _require_cycle(cycle)
    if now is not None and now.tzinfo is None:
        raise ValueError("directive claim timestamp must be timezone-aware")
    context = DirectiveContext.from_state_dir(state_dir)
    if source_path is not None:
        context.validate_source_argument(source_path)
    _validate_context_paths(context)
    with exclusive_state_transaction_lock(context.state_dir):
        _validate_context_paths(context)
        from futures_fund.reconcile_commit import cycle_is_complete, transaction_path

        pending_reconcile = transaction_path(context.state_dir)
        if pending_reconcile.exists() or pending_reconcile.is_symlink():
            raise RuntimeError("cannot claim a directive during a pending reconcile transaction")
        _recover_completed_claim_unlocked(context)
        intent = _load_intent(context)
        _validate_claim_inventory(context, intent)
        if intent is not None:
            if intent["cycle"] != cycle:
                raise RuntimeError(
                    "active directive claim belongs to a different cycle and was not consumed"
                )
            return _materialize_claim(context, intent)
        if cycle_is_complete(
            context.state_dir,
            cycle,
            cadence="rebal",
            require_manifest=True,
        ):
            raise RuntimeError("cannot claim a new directive for an already completed cycle")
        if not _source_present(context):
            return None
        context.claim_root.mkdir(parents=True, exist_ok=True)
        fsync_directory(context.state_dir)
        _validate_context_paths(context)
        intent = _new_claim_intent(
            context,
            cycle=cycle,
            now=now or datetime.now(UTC),
        )
        durable_write_json(context.active_path, intent)
        return _materialize_claim(context, intent)


def build_directive_receipt(claim: dict, *, cycle: int) -> dict:
    """Build the completed-generation archive for one validated state-owned claim."""

    cycle = _require_cycle(cycle)
    intent = _validate_intent({key: value for key, value in claim.items() if key != "text"})
    text = claim.get("text")
    if not isinstance(text, str):
        raise ValueError("directive claim lacks exact text")
    payload = text.encode("utf-8")
    capabilities = list(parse_directive_capabilities(text))
    if (
        cycle != intent["cycle"]
        or sha256(payload).hexdigest() != intent["payload_sha256"]
        or canonical_json_sha256(text) != intent["directive_sha256"]
        or capabilities != intent["capabilities"]
    ):
        raise ValueError("directive receipt does not match its claimed instance")
    body = {
        "schema_version": DIRECTIVE_RECEIPT_SCHEMA_VERSION,
        "paper_only": True,
        "cycle": cycle,
        "claim_id": intent["claim_id"],
        "claim_intent_sha256": intent["claim_intent_sha256"],
        "source_relpath": DIRECTIVE_SOURCE_RELPATH.as_posix(),
        "text": text,
        "payload_sha256": intent["payload_sha256"],
        "directive_sha256": intent["directive_sha256"],
        "capabilities": capabilities,
        "capabilities_sha256": intent["capabilities_sha256"],
    }
    return {**body, "receipt_sha256": canonical_json_sha256(body)}


def validate_directive_receipt(receipt: object, *, expected_cycle: int | None = None) -> dict:
    """Validate v2; older absolute-path receipts are audit-only, never cleanup authority."""

    if expected_cycle is not None:
        expected_cycle = _require_cycle(expected_cycle)
    if not isinstance(receipt, dict):
        raise ValueError("directive receipt must be a JSON object")
    required = {
        "schema_version",
        "paper_only",
        "cycle",
        "claim_id",
        "claim_intent_sha256",
        "source_relpath",
        "text",
        "payload_sha256",
        "directive_sha256",
        "capabilities",
        "capabilities_sha256",
        "receipt_sha256",
    }
    if set(receipt) != required:
        raise ValueError("directive receipt has an invalid field set")
    body = dict(receipt)
    receipt_sha = body.pop("receipt_sha256")
    text = body.get("text")
    claim_id = body.get("claim_id")
    capabilities = list(parse_directive_capabilities(text)) if isinstance(text, str) else None
    if (
        type(body.get("schema_version")) is not int
        or body.get("schema_version") != DIRECTIVE_RECEIPT_SCHEMA_VERSION
        or body.get("paper_only") is not True
        or type(body.get("cycle")) is not int
        or body["cycle"] < 1
        or (expected_cycle is not None and body["cycle"] != int(expected_cycle))
        or not isinstance(claim_id, str)
        or _CLAIM_ID.fullmatch(claim_id) is None
        or not _is_sha256(body.get("claim_intent_sha256"))
        or body.get("source_relpath") != DIRECTIVE_SOURCE_RELPATH.as_posix()
        or not isinstance(text, str)
        or not text.strip()
        or sha256(text.encode("utf-8")).hexdigest() != body.get("payload_sha256")
        or canonical_json_sha256(text) != body.get("directive_sha256")
        or capabilities != body.get("capabilities")
        or canonical_json_sha256(capabilities) != body.get("capabilities_sha256")
        or not _is_sha256(body.get("payload_sha256"))
        or not _is_sha256(body.get("directive_sha256"))
        or not _is_sha256(body.get("capabilities_sha256"))
        or not _is_sha256(receipt_sha)
        or receipt_sha != canonical_json_sha256(body)
    ):
        raise ValueError("directive receipt identity/hash/capability mismatch")
    return dict(receipt)


def load_completed_directive_receipt(
    state_dir: str | Path,
    cycle: int,
) -> dict | None:
    """Return the same receipt object whose exact hash the completion manifest authenticated."""

    from futures_fund.cycle_io import cycle_dir
    from futures_fund.reconcile_commit import completed_artifact_sha256

    cycle = _require_cycle(cycle)
    context = DirectiveContext.from_state_dir(state_dir)
    _validate_context_paths(context)
    expected_sha = completed_artifact_sha256(
        state_dir,
        cycle,
        DIRECTIVE_RECEIPT_ARTIFACT,
        cadence="rebal",
    )
    if expected_sha is None:
        return None
    path = cycle_dir(state_dir, cycle, cadence="rebal") / f"{DIRECTIVE_RECEIPT_ARTIFACT}.json"
    _reject_symlink_components(path, allow_missing_leaf=False)
    receipt = _read_json_regular(path)
    if canonical_json_sha256(receipt) != expected_sha:
        raise ValueError("directive receipt changed after manifest verification")
    if type(receipt.get("schema_version")) is int and receipt.get("schema_version") == 1:
        # Historical absolute-path receipts remain immutable audit evidence, but never become
        # cleanup or acknowledgement authority under the claimed-instance protocol.
        return None
    return validate_directive_receipt(receipt, expected_cycle=cycle)


def _validate_receipt_matches_intent(receipt: dict, intent: dict) -> None:
    fields = (
        "claim_id",
        "claim_intent_sha256",
        "source_relpath",
        "payload_sha256",
        "directive_sha256",
        "capabilities",
        "capabilities_sha256",
    )
    if receipt["cycle"] != intent["cycle"] or any(
        receipt[field] != intent[field] for field in fields
    ):
        raise RuntimeError("completed directive receipt does not match the active claim")


def _validate_consumption(value: dict, receipt: dict) -> None:
    required = {
        "schema_version",
        "paper_only",
        "cycle",
        "claim_id",
        "receipt_sha256",
        "consumed_at",
        "consumption_sha256",
    }
    if set(value) != required:
        raise ValueError("directive consumption has an invalid field set")
    body = dict(value)
    digest = body.pop("consumption_sha256", None)
    claim_id = body.get("claim_id")
    if (
        type(body.get("schema_version")) is not int
        or body.get("schema_version") != DIRECTIVE_CONSUMPTION_SCHEMA_VERSION
        or body.get("paper_only") is not True
        or type(body.get("cycle")) is not int
        or body.get("cycle") != receipt["cycle"]
        or not isinstance(claim_id, str)
        or _CLAIM_ID.fullmatch(claim_id) is None
        or claim_id != receipt["claim_id"]
        or body.get("receipt_sha256") != receipt["receipt_sha256"]
        or not _is_aware_timestamp(body.get("consumed_at"))
        or not _is_sha256(digest)
        or digest != canonical_json_sha256(body)
    ):
        raise ValueError("directive consumption receipt mismatch")


def _validate_completed_consumptions(
    context: DirectiveContext,
    *,
    allow_unacknowledged_cycle: int | None = None,
) -> None:
    """Prove every historical v2 receipt reached its state-owned acknowledgement boundary."""

    from futures_fund.reconcile_commit import completed_cycle_numbers

    receipt_cycles: set[int] = set()
    for cycle in completed_cycle_numbers(context.state_dir, cadence="rebal"):
        receipt = load_completed_directive_receipt(context.state_dir, cycle)
        if receipt is None:
            continue
        receipt_cycles.add(cycle)
        if cycle == allow_unacknowledged_cycle:
            continue
        ack_path = _consumption_path(context, cycle)
        if not ack_path.exists() and not ack_path.is_symlink():
            raise RuntimeError(
                f"manifest-bound directive cycle {cycle} lacks its consumption acknowledgement"
            )
        _validate_consumption(_read_json_regular(ack_path), receipt)

    consumption_root = context.claim_root / "consumptions"
    if not consumption_root.exists():
        return
    for path in consumption_root.iterdir():
        match = re.fullmatch(r"cycle-([1-9][0-9]*)\.json", path.name)
        if match is None:
            continue
        cycle = int(match.group(1))
        if cycle not in receipt_cycles:
            raise RuntimeError(
                f"directive consumption cycle {cycle} has no manifest-bound v2 receipt"
            )


def _finalize_unlocked(context: DirectiveContext, cycle: int, receipt: dict) -> dict:
    _validate_context_paths(context)
    ack_path = _consumption_path(context, cycle)
    intent = _load_intent(context)
    if intent is None:
        _validate_claim_inventory(context, None)
        if not ack_path.exists() and not ack_path.is_symlink():
            raise RuntimeError(
                "manifest-bound directive receipt has neither an active claim nor a "
                "consumption acknowledgement"
            )
        _validate_consumption(_read_json_regular(ack_path), receipt)
        return {"consumed": False, "reason": "claim_absent", "cycle": int(cycle)}
    _validate_receipt_matches_intent(receipt, intent)
    _validate_claim_inventory(context, intent)
    claim_path = _claim_path(context, intent["claim_id"])
    tomb_path = _tomb_path(context, intent["claim_id"])
    duplicate_path = _source_duplicate_path(context, intent["claim_id"])
    if claim_path.exists() and tomb_path.exists():
        raise RuntimeError("directive cleanup has both claim and tombstone; preserving both")
    if claim_path.is_symlink() or tomb_path.is_symlink():
        raise RuntimeError("directive cleanup refuses a symlinked claim/tombstone")
    if claim_path.exists():
        _claim_from_payload(context, intent, claim_path)
        _recover_duplicate_source(context, intent, claim_path)
        if duplicate_path.exists() or duplicate_path.is_symlink():
            raise RuntimeError("directive source-duplicate recovery remains unfinished")
        _rename_durable(claim_path, tomb_path)
    elif duplicate_path.exists() or duplicate_path.is_symlink():
        raise RuntimeError("directive source-duplicate quarantine exists without its claim")
    if tomb_path.exists():
        _claim_from_payload(context, intent, tomb_path)
        durable_unlink(tomb_path)
    # No payload after an authenticated receipt also represents a crash after tomb unlink.
    if not ack_path.exists() and not ack_path.is_symlink():
        body = {
            "schema_version": DIRECTIVE_CONSUMPTION_SCHEMA_VERSION,
            "paper_only": True,
            "cycle": int(cycle),
            "claim_id": receipt["claim_id"],
            "receipt_sha256": receipt["receipt_sha256"],
            "consumed_at": datetime.now(UTC).isoformat(),
        }
        durable_write_json(
            ack_path,
            {**body, "consumption_sha256": canonical_json_sha256(body)},
        )
    else:
        _validate_consumption(_read_json_regular(ack_path), receipt)
    durable_unlink(context.active_path)
    return {
        "consumed": True,
        "cycle": int(cycle),
        "claim_id": receipt["claim_id"],
        "receipt_sha256": receipt["receipt_sha256"],
    }


def finalize_cycle_directive(state_dir: str | Path, cycle: int) -> dict:
    """Consume only cycle N's manifest-bound state claim; never touch the canonical inbox."""

    from futures_fund.state_transaction import exclusive_state_transaction_lock

    cycle = _require_cycle(cycle)
    context = DirectiveContext.from_state_dir(state_dir)
    _validate_context_paths(context)
    with exclusive_state_transaction_lock(context.state_dir):
        _validate_context_paths(context)
        from futures_fund.reconcile_commit import transaction_path

        pending_reconcile = transaction_path(context.state_dir)
        if pending_reconcile.exists() or pending_reconcile.is_symlink():
            raise RuntimeError(
                "cannot finalize a directive during a pending reconcile transaction"
            )
        intent = _load_intent(context)
        allow_cycle = (
            cycle if intent is not None and intent["cycle"] == cycle else None
        )
        _validate_completed_consumptions(
            context,
            allow_unacknowledged_cycle=allow_cycle,
        )
        receipt = load_completed_directive_receipt(context.state_dir, cycle)
        if receipt is None:
            if intent is not None and intent["cycle"] == int(cycle):
                raise RuntimeError("completed cycle lacks its active directive receipt")
            return {"consumed": False, "reason": "cycle_has_no_v2_directive", "cycle": int(cycle)}
        result = _finalize_unlocked(context, int(cycle), receipt)
        _validate_completed_consumptions(context)
        return result


def _recover_completed_claim_unlocked(context: DirectiveContext) -> dict:
    from futures_fund.reconcile_commit import cycle_is_complete

    intent = _load_intent(context)
    _validate_completed_consumptions(
        context,
        allow_unacknowledged_cycle=int(intent["cycle"]) if intent is not None else None,
    )
    if intent is None:
        _validate_claim_inventory(context, None)
        return {"status": "idle"}
    _validate_claim_inventory(context, intent)
    receipt = load_completed_directive_receipt(context.state_dir, int(intent["cycle"]))
    if receipt is not None:
        result = _finalize_unlocked(context, int(intent["cycle"]), receipt)
        return {"status": "recovered_cleanup", **result}
    if cycle_is_complete(
        context.state_dir,
        int(intent["cycle"]),
        cadence="rebal",
        require_manifest=True,
    ):
        raise RuntimeError("completed cycle has an active claim but no manifest-bound receipt")
    tomb_path = _tomb_path(context, intent["claim_id"])
    if tomb_path.exists() or tomb_path.is_symlink():
        raise RuntimeError("directive tombstone exists without a manifest-bound receipt")
    claim = _materialize_claim(context, intent)
    return {
        "status": "claimed_pending",
        "cycle": intent["cycle"],
        "claim_id": intent["claim_id"],
        "directive_sha256": claim["directive_sha256"],
    }


def recover_directive_lifecycle(state_dir: str | Path) -> dict:
    """Replay a prepared claim or post-completion cleanup under the state transaction lock."""

    from futures_fund.state_transaction import exclusive_state_transaction_lock

    context = DirectiveContext.from_state_dir(state_dir)
    _validate_context_paths(context)
    with exclusive_state_transaction_lock(context.state_dir):
        _validate_context_paths(context)
        from futures_fund.reconcile_commit import transaction_path

        pending_reconcile = transaction_path(context.state_dir)
        if pending_reconcile.exists() or pending_reconcile.is_symlink():
            raise RuntimeError(
                "cannot recover directives during a pending reconcile transaction"
            )
        return _recover_completed_claim_unlocked(context)


def directive_lifecycle_status(state_dir: str | Path) -> dict:
    """Inspect claim/cleanup state without creating, recovering, renaming, or deleting anything."""

    context = DirectiveContext.from_state_dir(state_dir)
    queued = os.path.lexists(context.source_path)
    try:
        _validate_context_paths(context)
        queued = _source_present(context)
        if not context.active_path.exists() and not context.active_path.is_symlink():
            _validate_completed_consumptions(context)
            _validate_claim_inventory(context, None)
            return {"status": "idle", "queued_source_present": queued}
        _reject_symlink_components(context.active_path, allow_missing_leaf=False)
        intent = _load_intent(context)
        if intent is None:
            raise RuntimeError("active directive claim disappeared during status inspection")
        _validate_completed_consumptions(
            context,
            allow_unacknowledged_cycle=int(intent["cycle"]),
        )
        _validate_claim_inventory(context, intent)
        claim_path = _claim_path(context, intent["claim_id"])
        tomb_path = _tomb_path(context, intent["claim_id"])
        duplicate_path = _source_duplicate_path(context, intent["claim_id"])
        receipt = load_completed_directive_receipt(context.state_dir, int(intent["cycle"]))
        if receipt is None:
            from futures_fund.reconcile_commit import cycle_is_complete

            if cycle_is_complete(
                context.state_dir,
                int(intent["cycle"]),
                cadence="rebal",
                require_manifest=True,
            ):
                raise RuntimeError(
                    "completed cycle has an active claim but no manifest-bound receipt"
                )
        payload_state = (
            "duplicate_source"
            if duplicate_path.exists()
            or duplicate_path.is_symlink()
            or (
                claim_path.exists()
                and queued
                and _path_is_claim_instance(context, context.source_path, intent)
            )
            else "claim"
            if claim_path.exists() and not tomb_path.exists()
            else "tombstone"
            if tomb_path.exists() and not claim_path.exists()
            else "missing"
            if not claim_path.exists() and not tomb_path.exists()
            else "conflict"
        )
        if payload_state in {"claim", "tombstone"}:
            _claim_from_payload(
                context,
                intent,
                claim_path if payload_state == "claim" else tomb_path,
            )
        return {
            "status": "cleanup_pending" if receipt is not None else "claimed_pending",
            "cycle": intent["cycle"],
            "claim_id": intent["claim_id"],
            "payload_state": payload_state,
            "queued_source_present": queued,
        }
    except Exception as exc:  # noqa: BLE001 - health must expose, not repair, malformed state
        return {
            "status": "conflict",
            "queued_source_present": queued,
            "error": str(exc),
        }
