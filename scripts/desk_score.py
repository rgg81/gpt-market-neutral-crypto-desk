"""Cycle step 2 (deterministic, FAIL-SOFT): score the PREVIOUS cycle against this cycle's fresh
marks, append the scorecard, and write the confirmed recurrences the Reflector will act on.

    uv run python scripts/desk_score.py --state-dir live_state --memory-dir live_memory

Runs right after desk_evidence (that is when the previous cycle's forward marks exist). Any error is
logged and the file `<memory>/pending/recurrences.json` is written as `[]` so the cycle proceeds
normally on the current prompts. PAPER ONLY."""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from futures_fund.pending_io import resolve_pending
from futures_fund.reflection import score_previous_cycle


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score the previous desk cycle (fail-soft).")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    args = ap.parse_args(argv)
    pending = Path(args.memory_dir) / "pending"
    try:
        pending, meta = resolve_pending(args.memory_dir)
        evidence = json.loads((pending / "evidence.json").read_text())
        cur_marks = {e["symbol"]: float(e["mark"]) for e in evidence}
        res = score_previous_cycle(
            args.state_dir, args.memory_dir, scored_cycle=int(meta["cycle"]) - 1,
            cur_marks=cur_marks, now=meta["now"], btc_symbol=meta["btc_symbol"])
        print(json.dumps(res, indent=2))
    except Exception:  # noqa: BLE001 — learning is fail-soft; never block the cycle
        pending.mkdir(parents=True, exist_ok=True)
        (pending / "recurrences.json").write_text("[]")
        print("desk_score failed (fail-soft); wrote empty recurrences.json", file=sys.stderr)
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
