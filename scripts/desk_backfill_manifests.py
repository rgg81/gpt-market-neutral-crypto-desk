"""One-time audited migration for reconcile generations created before hash manifests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from futures_fund.reconcile_commit import (
    backfill_completion_manifest,
    finalize_manifest_migration,
    protocol_first_cycle,
    protocol_manifest_required,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--cadence", default="rebal")
    args = parser.parse_args(argv)
    first = protocol_first_cycle(args.state_dir)
    if first is None:
        print(json.dumps({"backfilled": [], "reason": "no reconcile protocol"}))
        return 0
    root = Path(args.state_dir) / args.cadence / "cycle"
    published = sorted(
        int(path.name)
        for path in root.iterdir()
        if path.is_dir()
        and path.name.isdigit()
        and int(path.name) >= first
        and (path / "complete.json").exists()
    ) if root.exists() else []
    backfilled = []
    if not protocol_manifest_required(args.state_dir):
        backfilled = [
            cycle
            for cycle in published
            if backfill_completion_manifest(args.state_dir, cycle, cadence=args.cadence)
        ]
    # Also re-verify every marker on idempotent post-migration invocations; never print a green
    # migration state merely because the protocol flag is already set.
    finalize_manifest_migration(args.state_dir, cadence=args.cadence)
    print(json.dumps({
        "backfilled": backfilled,
        "manifest_required": protocol_manifest_required(args.state_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
