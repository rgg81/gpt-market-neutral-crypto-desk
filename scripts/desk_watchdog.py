"""Schedule watchdog (deterministic DATA FEED): classify how late/early this cycle is firing.

    uv run python scripts/desk_watchdog.py --state-dir live_state

Compares now against the LAST recorded cycle's `ran_at` (report.json; falls back to the
equity-history ts) and the 8h cadence, and prints a JSON `schedule_status` the orchestrator
injects into the PM/Adversary dispatches: agents deserve to know the book floated unmarked for
17 hours (session death) or that this firing is a manual re-run 30 minutes after the last.
Classification only — the orchestrator/agents decide what to do with it (charter: data, no veto).

Classes: FIRST (no history), EARLY (< 6h since last), ON_TIME (6-10h), LATE (10-16h),
MISSED_N (>= 16h: N = full 8h cycles skipped).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

CADENCE_H = 8.0
EARLY_H = 6.0
LATE_H = 10.0
MISSED_H = 16.0


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
    nums = sorted(int(p.name) for p in root.glob("*") if p.is_dir() and p.name.isdigit())
    if not nums:
        return None, None
    last = nums[-1]
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classify this firing vs the 8h cadence.")
    ap.add_argument("--state-dir", default="live_state")
    args = ap.parse_args(argv)

    now = datetime.now(UTC)
    cycle, ts = last_cycle_ts(args.state_dir)
    if cycle is None or ts is None:
        out = {"schedule_status": "FIRST", "last_cycle": cycle, "gap_hours": None,
               "note": "no prior cycle timestamp — first run or legacy state"}
    else:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        gap_h = (now - ts).total_seconds() / 3600.0
        status = classify(gap_h)
        note = {
            "EARLY": "under 6h since the last cycle — likely a manual/duplicate firing; "
                     "consider standing down (turnover costs real money)",
            "ON_TIME": "normal 8h cadence",
            "LATE": "over 10h — the book floated unmarked; funding boundaries may have passed",
        }.get(status, "16h+ — one or more cycles were missed (session death?); "
                      "run ONE catch-up cycle, never backfill")
        out = {"schedule_status": status, "last_cycle": cycle,
               "last_ran_at": ts.isoformat(), "now": now.isoformat(),
               "gap_hours": round(gap_h, 2), "note": note}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
