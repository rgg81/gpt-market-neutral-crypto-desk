"""Single-flight run lock for the serialized paper runner (Phase 7, reuse — from the weekly desk).

The weekly Selection and daily Rebalance cadences share ONE paper account/book. There is no
concurrency in the engine; correctness depends on EXACTLY ONE writer at a time. Atomic state writes
prevent torn files but NOT lost updates (load->mutate->save races). This lock closes that gap: the
e2e driver (`scripts/run_paper_cli.py`, `owner="paper"`) tries to acquire `state/.run.lock` before
running any cadence; if it is held by a live run, the fire stands down (the next poll retries). A
stale lock — a crashed run that never released — is reclaimed after `stale_after_s`, so a crash can
never wedge the desk permanently.

Pure stdlib, no third-party deps. `now` is injected (tz-aware UTC) so the stale check is testable
and deterministic. Acquisition is atomic via O_CREAT|O_EXCL.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

LOCK_NAME = ".run.lock"
DEFAULT_STALE_AFTER_S = 1800.0  # 30 min: well beyond a normal cadence cycle, short enough to heal


def _read(path: Path) -> dict | None:
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, dict) else None
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _holder_age_s(holder: dict | None, now: datetime) -> float | None:
    """Seconds since the holder started, or None if its start_ts is missing/unparseable."""
    if not isinstance(holder, dict):
        return None
    raw = holder.get("start_ts")
    if not isinstance(raw, str):
        return None
    try:
        start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return (now - start).total_seconds()


def _is_stale(holder: dict | None, now: datetime, stale_after_s: float) -> bool:
    """A holder is reclaimable when its age is unparseable, NEGATIVE (future-skewed start_ts —
    corrupt; mirrors scheduling.py's future-stamp guard so a forward clock skew can't extend the
    lease forever), or older than the stale window."""
    age = _holder_age_s(holder, now)
    return age is None or age < 0 or age >= stale_after_s


def _create_excl(lock: Path, payload: str) -> bool:
    """Atomically create the lock with O_CREAT|O_EXCL. True iff WE created it (the kernel guarantees
    exactly one creator); False if it already exists."""
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, payload.encode())
    finally:
        os.close(fd)
    return True


def _unlink_if_same(lock: Path, holder: dict | None) -> bool:
    """Best-effort CAS: remove the lock ONLY if it still holds `holder` (same pid+start_ts), so we
    never delete a lock another process already reclaimed. True if removed or already gone; False if
    it now holds a DIFFERENT holder (caller must re-observe)."""
    cur = _read(lock)
    if (cur is not None and isinstance(holder, dict)
            and (cur.get("pid") != holder.get("pid")
                 or cur.get("start_ts") != holder.get("start_ts"))):
        return False
    try:
        os.unlink(lock)
    except FileNotFoundError:
        pass
    return True


def try_acquire(state_dir, now: datetime, *, owner: str = "runner",
                stale_after_s: float = DEFAULT_STALE_AFTER_S) -> tuple[bool, dict | None]:
    """Atomically try to acquire state/.run.lock. Returns (acquired, prior_holder).

    Acquired when the lock was free, OR when an existing lock is STALE and is reclaimed. When held
    by a live run, returns (False, holder).

    Reclaim is ATOMIC: the stale lock is removed (only if still the same stale holder) and then
    re-created via O_CREAT|O_EXCL, so under concurrent reclaimers EXACTLY ONE process wins the
    create and returns True — the losers re-observe the fresh holder and return False. (A blind
    os.replace overwrite would let two processes both 'reclaim' the same stale lock and both proceed
    as the writer — the exact double-writer the lock exists to prevent.)"""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    lock = d / LOCK_NAME
    payload = json.dumps({"pid": os.getpid(), "owner": owner, "start_ts": now.isoformat()})
    evicted: dict | None = None  # the stale holder we reclaimed, returned to the caller on win
    for _ in range(8):  # bounded retries under extreme contention; normal path is 1-2 iterations
        if _create_excl(lock, payload):
            return True, evicted            # win: free acquire (evicted=None) or stale reclaim
        holder = _read(lock)
        if not _is_stale(holder, now, stale_after_s):
            return False, holder            # a live holder -> denied
        if not _unlink_if_same(lock, holder):
            continue                        # someone reclaimed under us -> re-observe
        evicted = holder                    # we removed this stale holder; the next create wins
        # loop: re-attempt the EXCL create; only the creator wins, losers re-observe a fresh holder
    return False, _read(lock)               # gave up under contention; never double-writes


def release(state_dir) -> None:
    """Release the lock (idempotent: a missing lock is fine)."""
    try:
        (Path(state_dir) / LOCK_NAME).unlink()
    except FileNotFoundError:
        pass


@contextmanager
def single_flight(state_dir, now: datetime, *, owner: str = "runner",
                  stale_after_s: float = DEFAULT_STALE_AFTER_S):
    """Context manager: yields True if the lock was acquired (releasing on exit), else False.

        with single_flight(state_dir, now, owner="paper") as ok:
            if not ok:
                return  # another run is in flight; stand down
            ... run the cadences ...
    """
    acquired, _holder = try_acquire(state_dir, now, owner=owner, stale_after_s=stale_after_s)
    try:
        yield acquired
    finally:
        if acquired:
            release(state_dir)
