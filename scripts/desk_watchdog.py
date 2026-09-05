"""Schedule watchdog (deterministic DATA FEED): classify how late/early this cycle is firing.

    uv run python scripts/desk_watchdog.py --state-dir live_state

Compares now against the LAST recorded cycle's `ran_at` (report.json; falls back to the
equity-history ts) and the 24h decision cadence, and prints a JSON `schedule_status` the
orchestrator injects into the PM/Adversary dispatches: agents deserve to know when the book missed
its daily decision or that this firing is a manual re-run 30 minutes after the last.
The classifier remains a deterministic data feed.  The evidence boundary enforces the runbook's
EARLY stand-down and binds this receipt into the cycle packet; no trading decision is made here.

Classes: FIRST (no history), EARLY (< 18h since last), ON_TIME (18-30h), LATE (30-48h),
MISSED_N (>= 48h: N = full 24h periods elapsed).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.reconcile_commit import completed_cycle_numbers

CADENCE_H = 24.0
EARLY_H = 18.0
LATE_H = 30.0
MISSED_H = 48.0


def classify(gap_hours: float) -> str:
    if gap_hours < EARLY_H:
        return "EARLY"
    if gap_hours < LATE_H:
        return "ON_TIME"
    if gap_hours < MISSED_H:
        return "LATE"
    return f"MISSED_{int(gap_hours // CADENCE_H)}"


def last_cycle_ts(state_dir: str, cadence: str = "rebal") -> tuple[int | None, datetime | None]:
    """(last_cycle, its best-known run timestamp) from report.json ran_at, else equity history."""
    root = Path(state_dir) / cadence / "cycle"
    if not root.exists():
        return None, None
    nums = completed_cycle_numbers(state_dir, cadence=cadence)
    if not nums:
        return None, None
    last = max(nums)
    report_p = root / str(last) / "report.json"
    if report_p.exists():
        rep = json.loads(report_p.read_text())
        for key in ("ran_at", "decision_ts"):
            if rep.get(key):
                return last, datetime.fromisoformat(rep[key])
    eq_p = Path(state_dir) / "equity-history.jsonl"
    if eq_p.exists():
        for line in reversed(eq_p.read_text().splitlines()):
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("cycle") == last:
                return last, datetime.fromisoformat(str(r["ts"]))
    return last, None


def build_watchdog_receipt(
    state_dir: str,
    *,
    now: datetime,
    cadence: str = "rebal",
) -> dict:
    """Build the deterministic cadence receipt bound into a cycle's evidence packet."""
    if now.tzinfo is None:
        raise ValueError("watchdog observation timestamp must be timezone-aware")
    observed_at = now.astimezone(UTC)
    cycle, ts = last_cycle_ts(state_dir, cadence=cadence)
    expected_next_cycle = (cycle or 0) + 1
    if cycle is None:
        return {
            "schema_version": 1,
            "paper_only": True,
            "schedule_status": "FIRST",
            "last_completed_cycle": None,
            "last_cycle_ts": None,
            "observed_at": observed_at.isoformat(),
            "gap_hours": None,
            "next_cycle": expected_next_cycle,
        }
    if ts is None:
        return {
            "schema_version": 1,
            "paper_only": True,
            "schedule_status": "UNKNOWN_LAST_TIMESTAMP",
            "last_completed_cycle": cycle,
            "last_cycle_ts": None,
            "observed_at": observed_at.isoformat(),
            "gap_hours": None,
            "next_cycle": expected_next_cycle,
        }
    prior = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    gap_hours = (observed_at - prior).total_seconds() / 3600.0
    status = "FUTURE_CLOCK" if gap_hours < 0.0 else classify(gap_hours)
    return {
        "schema_version": 1,
        "paper_only": True,
        "schedule_status": status,
        "last_completed_cycle": cycle,
        "last_cycle_ts": prior.isoformat(),
        "observed_at": observed_at.isoformat(),
        "gap_hours": gap_hours,
        "next_cycle": expected_next_cycle,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classify this firing vs the 24h GPT cadence.")
    ap.add_argument("--state-dir", default="live_state")
    args = ap.parse_args(argv)

    now = datetime.now(UTC)
    out = build_watchdog_receipt(args.state_dir, now=now)
    status = out["schedule_status"]
    out["note"] = {
        "FIRST": "no completed prior cycle — cycle 1 may proceed",
        "EARLY": "under 18h since the last cycle — mandatory stand-down",
        "ON_TIME": "normal 24h full-GPT cadence",
        "LATE": "over 30h — the book missed its daily decision window",
        "FUTURE_CLOCK": "latest completed cycle is future-dated — HALT",
        "UNKNOWN_LAST_TIMESTAMP": "completed cycle lacks a trustworthy timestamp — HALT",
    }.get(
        status,
        "48h+ — one or more daily decisions were missed; run ONE catch-up cycle, never backfill",
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
