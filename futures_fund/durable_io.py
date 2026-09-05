"""Power-loss durable local file primitives.

``os.replace`` prevents readers from observing a partial file, but it does not by itself make
either the new file data or the directory entry durable across sudden power loss.  State commit
paths use these helpers so the order promised by their recovery protocols is also the order the
filesystem is asked to persist.
"""

from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any


def canonical_json_bytes(value: object) -> bytes:
    """Return the stable JSON representation used by manifests and hash chains."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: str | Path) -> None:
    """Persist directory-entry changes made before this call."""
    directory = Path(path)
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_parent(path: Path) -> None:
    """Create the parent and persist the resulting directory hierarchy best-effort."""
    missing: list[Path] = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        fsync_directory(directory)
        if directory.parent.exists():
            fsync_directory(directory.parent)


def durable_write_bytes(path: str | Path, data: bytes, *, mode: int = 0o600) -> Path:
    """Write, file-fsync, replace, and directory-fsync a file in one filesystem."""
    destination = Path(path)
    _ensure_parent(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return destination


def durable_write_text(path: str | Path, text: str, *, mode: int = 0o600) -> Path:
    return durable_write_bytes(path, text.encode("utf-8"), mode=mode)


def durable_write_json(
    path: str | Path,
    value: Any,
    *,
    indent: int | None = 2,
    mode: int = 0o600,
) -> Path:
    text = json.dumps(value, indent=indent, default=str, allow_nan=False)
    if indent is not None:
        text += "\n"
    return durable_write_text(path, text, mode=mode)


def durable_unlink(path: str | Path) -> bool:
    """Remove a file and persist the deletion before returning."""
    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    fsync_directory(target.parent)
    return True
