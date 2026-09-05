"""Claim-once UTC schedule gate for cron retry polling.

Cron can skip or delay an exact-minute job when the host clock is corrected or the machine wakes
late.  The managed crontab therefore polls both launchers every ten minutes.  This gate maps each
poll to the most recent intended UTC slot, accepts it for a bounded grace period, and persists one
claim before any desk task starts.

Full-cycle claims are deliberately *attempt* receipts because an automatic GPT retry could consume
tokens or collide with pending work. Heartbeat claims may be released after a failed durable,
idempotent heartbeat transaction, allowing cron to retry the same slot within its grace window.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

ScheduleKind = Literal["full", "heartbeat"]
UTC = timezone.utc  # noqa: UP017 — cron's /usr/bin/python3 is 3.10 on the deployed host

SCHEDULE_HOURS: dict[ScheduleKind, tuple[int, ...]] = {
    "full": (0,),
    "heartbeat": (8, 16),
}
SCHEDULE_MINUTE = 7
DEFAULT_GRACE_HOURS = 6.0
STAND_DOWN = 3


def parse_now(value: str | None) -> datetime:
    """Return an aware UTC timestamp, accepting ISO-8601 as a deterministic test seam."""
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def latest_slot(kind: ScheduleKind, now: datetime) -> datetime:
    """Return the latest nominal slot at or before ``now``."""
    now = now.astimezone(UTC)
    candidates = [
        now.replace(hour=hour, minute=SCHEDULE_MINUTE, second=0, microsecond=0)
        for hour in SCHEDULE_HOURS[kind]
    ]
    eligible = [slot for slot in candidates if slot <= now]
    if eligible:
        return max(eligible)
    previous_day = now - timedelta(days=1)
    return previous_day.replace(
        hour=max(SCHEDULE_HOURS[kind]),
        minute=SCHEDULE_MINUTE,
        second=0,
        microsecond=0,
    )


def read_claims(path: Path) -> dict[str, dict[str, str]]:
    """Read claims fail-closed; malformed scheduler state must never be overwritten."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read valid schedule claims from {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"schedule claims must be a JSON object: {path}")
    claims: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if key not in SCHEDULE_HOURS or not isinstance(value, dict):
            raise RuntimeError(f"invalid schedule claim entry {key!r} in {path}")
        slot = value.get("slot")
        claimed_at = value.get("claimed_at")
        if not isinstance(slot, str) or not isinstance(claimed_at, str):
            raise RuntimeError(f"invalid schedule claim timestamps for {key!r} in {path}")
        parse_now(slot)
        parse_now(claimed_at)
        claims[key] = {"slot": slot, "claimed_at": claimed_at}
    return claims


def due_slot(
    kind: ScheduleKind,
    now: datetime,
    claims: dict[str, dict[str, str]],
    *,
    grace_hours: float = DEFAULT_GRACE_HOURS,
) -> datetime | None:
    """Return an unclaimed slot inside its recovery window, otherwise ``None``."""
    if grace_hours <= 0.0:
        raise ValueError("grace_hours must be positive")
    slot = latest_slot(kind, now)
    age = now.astimezone(UTC) - slot
    if age < timedelta(0) or age > timedelta(hours=grace_hours):
        return None
    previous = claims.get(kind)
    if previous and parse_now(previous["slot"]) >= slot:
        return None
    return slot


def write_claims(path: Path, claims: dict[str, dict[str, str]]) -> None:
    """Atomically persist private runtime scheduler state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            fd = -1
            json.dump(claims, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def claim_due_slot(
    kind: ScheduleKind,
    now: datetime,
    path: Path,
    *,
    grace_hours: float = DEFAULT_GRACE_HOURS,
) -> datetime | None:
    """Persist and return the currently due slot. The caller must hold the desk lock."""
    claims = read_claims(path)
    slot = due_slot(kind, now, claims, grace_hours=grace_hours)
    if slot is None:
        return None
    claims[kind] = {"slot": slot.isoformat(), "claimed_at": now.astimezone(UTC).isoformat()}
    write_claims(path, claims)
    return slot


def release_claim(
    kind: ScheduleKind,
    now: datetime,
    path: Path,
    *,
    grace_hours: float = DEFAULT_GRACE_HOURS,
) -> datetime | None:
    """Release only this slot's exact claim. The caller must hold the shared desk lock."""
    claims = read_claims(path)
    slot = latest_slot(kind, now)
    age = now.astimezone(UTC) - slot
    prior = claims.get(kind)
    if (
        prior is None
        or parse_now(prior["slot"]) != slot
        or age < timedelta(0)
        or age > timedelta(hours=grace_hours)
    ):
        return None
    del claims[kind]
    write_claims(path, claims)
    return slot


def seed_current_slots(now: datetime, path: Path) -> bool:
    """Seed a new install so enabling cron cannot launch an immediate historical catch-up."""
    if path.exists():
        read_claims(path)
        return False
    claims = {
        kind: {"slot": latest_slot(kind, now).isoformat(), "claimed_at": now.isoformat()}
        for kind in SCHEDULE_HOURS
    }
    write_claims(path, claims)
    return True


def event(kind: ScheduleKind, slot: datetime, now: datetime, action: str) -> str:
    return json.dumps(
        {
            "schedule_gate": action,
            "kind": kind,
            "slot": slot.isoformat(),
            "observed_at": now.astimezone(UTC).isoformat(),
        },
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=tuple(SCHEDULE_HOURS))
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--now", help="ISO-8601 test seam; defaults to the current UTC time")
    parser.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--claim", action="store_true")
    action.add_argument("--release", action="store_true")
    action.add_argument("--seed-all", action="store_true")
    args = parser.parse_args(argv)

    now = parse_now(args.now)
    try:
        if args.seed_all:
            seeded = seed_current_slots(now, args.claims)
            print(json.dumps({"schedule_gate": "seeded" if seeded else "already_seeded"}))
            return 0
        if args.kind is None:
            parser.error("--kind is required with --check, --claim, or --release")
        if args.release:
            released = release_claim(
                args.kind,
                now,
                args.claims,
                grace_hours=args.grace_hours,
            )
            if released is None:
                return STAND_DOWN
            print(event(args.kind, released, now, "released"))
            return 0
        claims = read_claims(args.claims)
        slot = due_slot(args.kind, now, claims, grace_hours=args.grace_hours)
        if slot is None:
            return STAND_DOWN
        if args.check:
            print(event(args.kind, slot, now, "due"))
            return 0
        claimed = claim_due_slot(
            args.kind,
            now,
            args.claims,
            grace_hours=args.grace_hours,
        )
        if claimed is None:
            return STAND_DOWN
        print(event(args.kind, claimed, now, "claimed"))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"schedule_gate": "error", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
