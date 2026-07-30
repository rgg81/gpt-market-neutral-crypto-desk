"""Atomic, idempotent equity-history log (reuse template, spec §5/§17 — from the weekly desk).

The desk's total equity at each cycle end is the SOURCE of the return series every downstream
KPI/circuit-breaker reads (daily Sharpe ×365, no-losing-month, max drawdown). Storage is a single
append-only `equity-history.jsonl` under `state/`, written atomically (tmp + `os.replace`) and
idempotent per cycle so a DUE RETRY re-running the same cycle REPLACES its point rather than
injecting a spurious ~0% return.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


def _path(state_dir) -> Path:
    return Path(state_dir) / "equity-history.jsonl"


def record_equity(state_dir, ts: datetime, equity: float, cycle: int) -> None:
    """Append — or REPLACE the existing point for this `cycle` — the desk's total equity at cycle
    end (the return series' source). Idempotent under a DUE RETRY re-running the same cycle: without
    this, a RETRY appended a SECOND point for the cycle, injecting a spurious ~0% return that
    corrupts the Sharpe/Sortino and the daily/weekly/monthly circuit breakers fed off this series.
    Rewrite is atomic (tmp + os.replace) and tolerant of a pre-existing malformed line."""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    recs = []
    if p.exists():
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # skip a corrupt line rather than wedge the whole series
            if isinstance(r, dict) and r.get("cycle") == cycle:
                continue  # drop the prior point for this cycle -> RETRY replaces, never duplicates
            recs.append(r)
    # Monotonicity guard (2026-07 review: c3 carried a manufactured midnight stamp LATER than
    # c4's real one, silently disordering every time-scaled stat). A new point must not predate
    # the last surviving one — refuse loudly instead of recording a lie.
    if recs:
        try:
            from datetime import datetime as _dt
            last_ts = _dt.fromisoformat(str(recs[-1]["ts"]))
            if ts < last_ts:
                raise ValueError(
                    f"record_equity: non-monotonic ts {ts.isoformat()} < last "
                    f"{last_ts.isoformat()} (cycle {recs[-1].get('cycle')}) — refusing to "
                    "disorder the equity series")
        except (KeyError, TypeError):
            pass  # a legacy malformed last line never blocks recording
    recs.append({"ts": ts.isoformat(), "equity": float(equity), "cycle": cycle})
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in recs))
    os.replace(tmp, p)


def equity_series(state_dir) -> list[tuple[str, float]]:
    p = _path(state_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out.append((r["ts"], float(r["equity"])))
    return out


def returns_series(state_dir) -> list[float]:
    eq = [e for _, e in equity_series(state_dir)]
    return [(eq[i] / eq[i - 1] - 1.0) for i in range(1, len(eq)) if eq[i - 1] > 0]
