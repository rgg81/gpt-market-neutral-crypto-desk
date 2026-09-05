"""Write the exact digest of the complete normalized specialist-read packet."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from futures_fund.adversary_binding import specialist_reads_sha256
from futures_fund.pending_io import resolve_pending
from scripts.desk_reconcile import _parse_specialist_reads


def _atomic_json(path: Path, value: list[dict]) -> None:
    temporary = path.with_name(f".{path.name}.normalizing-{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(value, indent=2) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _exclusive_digest(path: Path, digest: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (digest + "\n").encode())
        os.fsync(fd)
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", default="live_memory")
    args = parser.parse_args(argv)

    pending, meta = resolve_pending(args.memory_dir)
    path = pending / "specialist_reads.sha256"
    if path.exists():
        raise ValueError(
            "specialist read digest already exists; post-digest mutation/re-digest is forbidden"
        )
    evidence = json.loads((pending / "evidence.json").read_text())
    expected_symbols = [str(row["symbol"]) for row in evidence]
    reads = {}
    failed = []
    for role in ("sentiment", "technical", "futures"):
        try:
            raw = json.loads((pending / f"{role}_reads.json").read_text())
            reads[role] = _parse_specialist_reads(raw, expected_symbols)
        except Exception:  # noqa: BLE001 — mirror reconcile's documented fail-soft role handling
            reads[role] = []
            failed.append(role)
    if len(failed) == 3:
        raise ValueError("all three specialist outputs failed validation")

    # From this point forward every downstream role reads the same normalized packet that is
    # hashed. A malformed fail-soft role cannot leave partial prose beside a semantic [] digest.
    for role in ("sentiment", "technical", "futures"):
        _atomic_json(
            pending / f"{role}_reads.json",
            [read.model_dump(mode="json") for read in reads[role]],
        )
    digest = specialist_reads_sha256(reads)
    _exclusive_digest(path, digest)
    print(json.dumps({
        "cycle": int(meta["cycle"]),
        "specialist_reads_sha256": digest,
        "specialist_failed": failed,
        "path": str(path),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
