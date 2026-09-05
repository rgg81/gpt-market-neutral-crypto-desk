#!/usr/bin/env python3
"""Attest that one Codex invocation completed a new cycle or correctly stood down EARLY."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.durable_io import durable_write_json, file_sha256
from futures_fund.reconcile_commit import completed_cycle_numbers
from scripts.desk_watchdog import classify, last_cycle_ts


def latest_completed_cycle(state_dir: str | Path) -> int:
    cycles = completed_cycle_numbers(state_dir)
    return max(cycles) if cycles else 0


def attest_cycle_outcome(
    state_dir: str | Path,
    *,
    before_cycle: int,
    now: datetime,
) -> tuple[dict, bool]:
    """Return a durable-launch receipt and whether the invocation met its contract."""
    state = Path(state_dir)
    after_cycle = latest_completed_cycle(state)
    base = {
        "schema_version": 1,
        "paper_only": True,
        "observed_at": now.astimezone(UTC).isoformat(),
        "before_cycle": before_cycle,
        "after_cycle": after_cycle,
    }
    if after_cycle == before_cycle + 1:
        marker = state / "rebal" / "cycle" / str(after_cycle) / "complete.json"
        return (
            {
                **base,
                "outcome": "COMPLETED",
                "complete_marker": str(marker),
                "complete_marker_sha256": file_sha256(marker),
            },
            True,
        )
    if after_cycle > before_cycle + 1:
        return (
            {
                **base,
                "outcome": "UNATTESTED_CYCLE_JUMP",
                "error": (
                    "invocation advanced more than one manifest-complete cycle; "
                    "one firing may complete exactly one cycle"
                ),
            },
            False,
        )

    last_cycle, last_ts = last_cycle_ts(str(state))
    schedule_status = "FIRST"
    gap_hours = None
    if last_cycle is not None and last_ts is not None:
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)
        gap_hours = (now.astimezone(UTC) - last_ts.astimezone(UTC)).total_seconds() / 3600.0
        schedule_status = "FUTURE_CLOCK" if gap_hours < 0.0 else classify(gap_hours)
    if schedule_status == "EARLY":
        return (
            {
                **base,
                "outcome": "EARLY_STAND_DOWN",
                "schedule_status": schedule_status,
                "gap_hours": gap_hours,
            },
            True,
        )
    return (
        {
            **base,
            "outcome": "UNATTESTED_EXIT_ZERO",
            "schedule_status": schedule_status,
            "gap_hours": gap_hours,
            "error": "no new manifest-complete cycle and the watchdog is not EARLY",
        },
        False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--now", help="aware ISO-8601 forensic/test timestamp")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--snapshot", action="store_true")
    action.add_argument("--attest", action="store_true")
    parser.add_argument("--before-cycle", type=int)
    args = parser.parse_args(argv)
    if args.snapshot:
        print(latest_completed_cycle(args.state_dir))
        return 0
    if args.before_cycle is None or args.before_cycle < 0:
        parser.error("--attest requires a non-negative --before-cycle")
    now = datetime.now(UTC)
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            parser.error("--now must be timezone-aware")
    receipt, valid = attest_cycle_outcome(
        args.state_dir,
        before_cycle=args.before_cycle,
        now=now,
    )
    durable_write_json(Path(args.log_dir) / "desk-last-launch-outcome.json", receipt)
    print(json.dumps(receipt, separators=(",", ":")))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
