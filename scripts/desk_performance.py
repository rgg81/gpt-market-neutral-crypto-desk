"""Build the deterministic performance packet consumed by every GPT desk role."""
from __future__ import annotations

import argparse
import json
import sys

from futures_fund.config import load_settings
from futures_fund.pending_io import resolve_pending
from futures_fund.performance import (
    build_performance_snapshot,
    canonical_sha256,
    write_performance_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--memory-dir", default="live_memory")
    args = parser.parse_args(argv)

    pending, meta = resolve_pending(args.memory_dir)
    settings = load_settings()
    snapshot = build_performance_snapshot(
        args.state_dir,
        args.memory_dir,
        pending,
        cycle=int(meta["cycle"]),
        as_of_ts=meta["now"],
        starting_capital=settings.account_size_usdt,
        require_cycle_meta=True,
    )
    path = pending / "performance_snapshot.json"
    write_performance_snapshot(path, snapshot)
    hash_path = pending / "performance_snapshot.sha256"
    hash_path.write_text(canonical_sha256(snapshot) + "\n")
    print(json.dumps({
        "cycle": snapshot["cycle"],
        "equity": snapshot["desk"]["equity"],
        "return_frac": snapshot["desk"]["return_frac"],
        "drawdown_frac": snapshot["desk"]["drawdown_frac"],
        "performance_snapshot": str(path),
        "performance_snapshot_sha256": str(hash_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
