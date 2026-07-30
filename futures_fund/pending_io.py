"""Per-cycle pending-directory resolution + freshness validation (2026-07 review, Incident B).

The flat `pending/` layout let a slow cycle-N-1 specialist finish AFTER cycle N's validation had
already passed on its leftover file — the PM then decided on stale reads. Isolation is per-cycle
subdirectories (`pending/<cycle>/`) with a `pending/current.json` pointer; this module is the one
place consumers resolve and sanity-check that pointer. Deterministic plumbing only — no decision.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_META_AGE = timedelta(minutes=90)     # a pending bundle older than this is a dead cycle
MAX_META_FUTURE = timedelta(minutes=5)   # a meta stamped in the future is manufactured


def resolve_pending(memory_dir) -> tuple[Path, dict]:
    """Resolve the CURRENT per-cycle pending dir and its validated meta.

    Returns (pending_dir, meta). Raises with a named reason on: missing pointer, pointer/dir
    mismatch, cycle mismatch between pointer and meta, or a meta `now` outside the sane window
    (future > 5min = manufactured stamp; older than 90min = stale/dead cycle — HALT, prior book
    stands, per the runbook's failure handling)."""
    root = (Path(memory_dir) / "pending").resolve()
    pointer_p = root / "current.json"
    if not pointer_p.exists():
        raise FileNotFoundError(
            f"{pointer_p} missing — run scripts/desk_evidence.py first (no current cycle)")
    pointer = json.loads(pointer_p.read_text())
    cycle = int(pointer["cycle"])
    if cycle < 1:
        raise ValueError(f"pending cycle must be positive, got {cycle}")
    raw_pending = Path(pointer["dir"])
    pending = raw_pending.resolve() if raw_pending.is_absolute() else (
        Path.cwd() / raw_pending
    ).resolve()
    expected = root / str(cycle)
    if pending != expected or pending.parent != root:
        raise ValueError(
            f"pending pointer/dir mismatch: cycle {cycle} must resolve to {expected}, "
            f"not {pending}"
        )
    if not pending.is_dir():
        raise FileNotFoundError(f"pending dir {pending} from current.json does not exist")
    meta = json.loads((pending / "meta.json").read_text())
    if int(meta["cycle"]) != cycle:
        raise ValueError(
            f"pending cycle mismatch: current.json says {cycle} but "
            f"{pending}/meta.json says {meta['cycle']} — refusing stale bundle")
    now_dt = datetime.fromisoformat(meta["now"])
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    wall = datetime.now(UTC)
    if now_dt > wall + MAX_META_FUTURE:
        raise ValueError(f"meta.now {meta['now']} is in the future — manufactured stamp, HALT")
    if wall - now_dt > MAX_META_AGE:
        raise ValueError(
            f"meta.now {meta['now']} is {wall - now_dt} old (> {MAX_META_AGE}) — dead cycle, "
            "HALT (prior book stands); re-run desk_evidence.py for a fresh cycle")
    return pending, meta
