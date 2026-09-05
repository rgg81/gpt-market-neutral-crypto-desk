"""Durable, idempotent equity-history log (reuse template, spec §5/§17).

The desk's total equity at each cycle end is the SOURCE of the return series every downstream
KPI/circuit-breaker reads (daily Sharpe ×365, no-losing-month, max drawdown). Storage is a single
append-only `equity-history.jsonl` under `state/`, written with file-fsync, atomic replace, and
directory-fsync, and
idempotent per cycle so a DUE RETRY re-running the same cycle REPLACES its point rather than
injecting a spurious ~0% return.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from futures_fund.durable_io import durable_write_text


def _path(state_dir) -> Path:
    return Path(state_dir) / "equity-history.jsonl"


def record_equity(state_dir, ts: datetime, equity: float, cycle: int) -> None:
    """Append an immutable, idempotent cycle close to the desk's equity return series.

    Durable reconcile recovery may replay the exact same row. A different same-cycle value or any
    malformed historical row is an audit-integrity failure and must never be rewritten away.
    """
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    recs = []
    if p.exists():
        seen: set[int] = set()
        for line_number, line in enumerate(p.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"malformed equity row {p}:{line_number}") from exc
            if not isinstance(r, dict):
                raise ValueError(f"non-object equity row {p}:{line_number}")
            try:
                row_cycle = int(r["cycle"])
                row_ts = datetime.fromisoformat(str(r["ts"]))
                row_equity = float(r["equity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid equity row {p}:{line_number}") from exc
            if row_cycle in seen:
                raise ValueError(f"duplicate equity cycle {row_cycle}")
            seen.add(row_cycle)
            if row_cycle == cycle:
                if row_ts != ts or row_equity != float(equity):
                    raise ValueError(f"conflicting equity replay for cycle {cycle}")
                continue
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
                    "disorder the equity series"
                )
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid last equity-history row") from exc
    recs.append({"ts": ts.isoformat(), "equity": float(equity), "cycle": cycle})
    durable_write_text(p, "".join(json.dumps(r, default=str) + "\n" for r in recs))


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
