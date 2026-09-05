"""Prepare and seal the auditable single-PM-revision receipt for one pending cycle."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from futures_fund.desk_contracts import AdversaryVerdict, Book
from futures_fund.pending_io import resolve_pending
from futures_fund.performance import canonical_sha256
from futures_fund.precheck import PrecheckMetrics
from scripts.desk_reconcile import _revision_dispatch_sha256


def _exclusive_json(path: Path, value: dict) -> None:
    """Create one immutable attempt receipt; never replace an existing path."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = (json.dumps(value, indent=2) + "\n").encode()
        os.write(fd, payload)
        os.fsync(fd)
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)


def _inputs(pending: Path):
    verdict = AdversaryVerdict.model_validate_json(
        (pending / "adversary.json").read_text()
    )
    if verdict.accept:
        raise ValueError("revision receipt is invalid for an accepted verdict")
    original_book = Book.model_validate_json(
        (pending / "pm_book_original.json").read_text()
    )
    original_precheck = PrecheckMetrics.model_validate_json(
        (pending / "precheck_original.json").read_text()
    )
    return verdict, original_book, original_precheck


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "seal"))
    parser.add_argument("--memory-dir", default="live_memory")
    args = parser.parse_args(argv)

    pending, meta = resolve_pending(args.memory_dir)
    verdict, original_book, original_precheck = _inputs(pending)
    if verdict.cycle != int(meta["cycle"]):
        raise ValueError("revision inputs do not match the pending cycle")
    dispatch_path = pending / "revision_dispatch_receipt.json"
    output_path = pending / "revision_output_receipt.json"
    dispatch_sha256 = _revision_dispatch_sha256(
        verdict, original_book, original_precheck
    )
    if args.action == "prepare":
        if output_path.exists():
            raise ValueError("revision output is already sealed; a second attempt is forbidden")
        current_book = Book.model_validate_json((pending / "pm_book.json").read_text())
        current_precheck = PrecheckMetrics.model_validate_json(
            (pending / "precheck.json").read_text()
        )
        if (
            canonical_sha256(current_book.model_dump(mode="json"))
            != canonical_sha256(original_book.model_dump(mode="json"))
            or canonical_sha256(current_precheck.model_dump(mode="json"))
            != canonical_sha256(original_precheck.model_dump(mode="json"))
        ):
            raise ValueError(
                "revision prepare must run before pm_book/precheck changes from the originals"
            )
        _exclusive_json(dispatch_path, {
            "schema_version": 1,
            "cycle": verdict.cycle,
            "attempt": 1,
            "dispatch_sha256": dispatch_sha256,
        })
    else:
        raw = json.loads(dispatch_path.read_text())
        if (
            set(raw) != {"schema_version", "cycle", "attempt", "dispatch_sha256"}
            or output_path.exists()
            or raw.get("schema_version") != 1
            or raw.get("cycle") != verdict.cycle
            or raw.get("attempt") != 1
            or raw.get("dispatch_sha256") != dispatch_sha256
        ):
            raise ValueError("revision receipt is missing, changed, or already sealed")
        final_book = Book.model_validate_json((pending / "pm_book.json").read_text())
        _exclusive_json(output_path, {
            "schema_version": 1,
            "cycle": verdict.cycle,
            "attempt": 1,
            "dispatch_sha256": dispatch_sha256,
            "output_book_sha256": canonical_sha256(final_book.model_dump(mode="json")),
        })
    print(json.dumps({
        "cycle": verdict.cycle,
        "revision_attempt": 1,
        "status": "prepared" if args.action == "prepare" else "sealed",
        "receipt": str(dispatch_path if args.action == "prepare" else output_path),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
