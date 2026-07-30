# scripts/runlock_cli.py
"""Single-flight run-lock CLI (SKILL.md W1/W12/D1/D8): the dual-cadence desk is orchestrated across
many separate CLI processes, so each cadence acquires the lock at the start of its
meeting and releases it at the END — exactly one writer at a time over the shared book.

    uv run python scripts/runlock_cli.py acquire --owner weekly   # ACQUIRED | LOCKED: <holder>
    uv run python scripts/runlock_cli.py release --owner weekly    # RELEASED
    uv run python scripts/runlock_cli.py status                    # FREE | HELD: <holder>

Closes I1 (runlock_cli.py named in SKILL.md but absent; runlock.py had no CLI). `acquire` exits 0 on
ACQUIRED, 0 on LOCKED (the caller stands down — not an error), 2 on internal error. A crashed
meeting that never releases is auto-reclaimed after `runlock.DEFAULT_STALE_AFTER_S`. The state
root flag is
`--state-dir` (consistent with the rest of the Phase 8 CLI family)."""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("acquire", "release", "status"):
        print("usage: runlock_cli.py acquire|release|status [--owner NAME] [--state-dir DIR]")
        return 2
    action = argv[0]
    owner = "runner"
    state_dir = "state"
    i = 1
    while i < len(argv):
        if argv[i] == "--owner" and i + 1 < len(argv):
            owner = argv[i + 1]
            i += 2
        elif argv[i] == "--state-dir" and i + 1 < len(argv):
            state_dir = argv[i + 1]
            i += 2
        else:
            i += 1
    try:
        from futures_fund import runlock
        now = datetime.now(UTC)
        if action == "acquire":
            ok, holder = runlock.try_acquire(state_dir, now, owner=owner)
            print("ACQUIRED" if ok else f"LOCKED: {json.dumps(holder)}")
            return 0
        if action == "release":
            runlock.release(state_dir)
            print("RELEASED")
            return 0
        p = Path(state_dir) / runlock.LOCK_NAME
        holder = runlock._read(p) if p.exists() else None
        print(f"HELD: {json.dumps(holder)}" if holder else "FREE")
        return 0
    except Exception as e:  # noqa: BLE001 — surface, never crash the orchestrator silently
        print(f"ERROR: runlock {action} failed: {e!r}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
