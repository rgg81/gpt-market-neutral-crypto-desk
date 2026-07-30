"""Multi-cadence due-gate for the serialized poll loop: decide whether THIS candle (of a given
timeframe, for a given loop) still needs a cycle.

TEMPEST-WEEKLY runs TWO loops on one paper account — a fast 15m scalp loop and a strategic 1h
trend/swing loop — driven by a single serialized poll. Each loop wants exactly one cycle per its own
candle. This module is the cadence primitive for BOTH: `cycle_due(..., tf_minutes=15, loop="fast")`
and `cycle_due(..., tf_minutes=60, loop="strategic")`. Backward-compatible default
(`tf_minutes=240, loop=None`) reproduces the original single-loop 4h behaviour on `state/cycle/*`.

    run iff no completed cycle has yet SERVED the candle that contains `now`.

Design notes (vetted by the design red-team, see tests/test_scheduling.py):
  * The cadence primitive is the SERVED CANDLE — report['candle'] = floor_tf(gate-start instant) —
    NOT completion time. A catch-up that finishes after the next boundary still only serves the
    candle it started in, so it cannot "steal" the next candle.
  * "Last completed cycle" = the highest cycle number whose report.json EXISTS and PARSES, found
    by scanning dirs in DESCENDING order. Never max(dir): a phantom empty dir or a crashed
    pre-gate dir must not wedge the loop into permanent SKIP.
  * All datetimes are tz-aware UTC end to end. mtime fallback uses fromtimestamp(ts, tz=UTC);
    ran_at/candle parsing normalizes 'Z' and coerces any naive value to UTC. floor_tf asserts aware.
  * Fail-safe: any unhandled error returns DUE (an extra run is low-harm — the gate reconciles
    against on-disk positions and cannot double-open — whereas a swallowed candle is worse).

Returns (mode, n, reason):
  mode == 'FRESH'  -> run a brand-new cycle, create <cycle_root>/<n>/ (n = highest_dir + 1)
  mode == 'RETRY'  -> re-run/overwrite the crashed dir <cycle_root>/<n>/ (n = highest_dir)
  mode == 'SKIP'   -> this candle is already served; do nothing (n = the serving cycle)
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Tolerate a served candle up to ONE step ahead of now's boundary, then distrust as corrupt.
# WHY: under a correct monotonic clock a served candle is always <= now's boundary
# (candle = floor_tf(start) <= floor_tf(now)), so this tolerance is dormant in normal operation. It
# only engages on a clock anomaly. A sub-candle backward NTP step across a boundary makes the
# JUST-served candle look one step ahead; trusting it yields a bounded SKIP (correct — don't
# re-serve it) instead of a needless re-run. COST: a LARGER backward step or a forward write-skew
# that survives correction can false-SKIP and swallow up to two real candles before it self-clears.
# That is an accepted, bounded, self-healing tradeoff for a paper desk.


def tf_to_minutes(tf: str) -> int:
    """Parse a ccxt timeframe string ('15m','1h','4h','1d') to minutes. Raises on unknown unit."""
    tf = tf.strip().lower()
    unit, qty = tf[-1], tf[:-1]
    mult = {"m": 1, "h": 60, "d": 1440}
    if unit not in mult or not qty.isdigit():
        raise ValueError(f"unrecognized timeframe {tf!r}")
    return int(qty) * mult[unit]


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)  # grid anchor (a Thursday) — see floor_tf


def floor_tf(dt: datetime, tf_minutes: int) -> datetime:
    """Floor a tz-aware UTC datetime to the timeframe grid anchored at the UTC epoch.

    The grid is contiguous fixed-width steps measured from 1970-01-01T00:00Z, so it is correct for
    ANY tf_minutes — sub-daily, daily, AND multi-day (weekly = 10080) alike. For every tf that
    divides a day evenly (15, 60, 240, 1440, ...) this is identical to anchoring at UTC 00:00 (the
    epoch is itself UTC midnight), so floor4 and the daily grid are unchanged. For tf_minutes that
    do NOT divide the day (notably WEEKLY=10080) it floors to the true week boundary instead of
    degenerating to the same-day midnight: e.g. Mon and Tue of one week both floor to that week's
    boundary, so weekly cadence gating actually gates per WEEK (Phase 3 Task 3.1). Week boundaries
    fall on Thursday 00:00Z because the epoch is a Thursday."""
    assert dt.tzinfo is not None, "floor_tf requires a tz-aware datetime"
    step = timedelta(minutes=tf_minutes)
    n = (dt - _EPOCH) // step
    return _EPOCH + n * step


def floor4(dt: datetime) -> datetime:
    """Floor to the 4h candle grid (00/04/08/12/16/20). Back-compat wrapper over floor_tf."""
    return floor_tf(dt, 240)


def _parse_utc(raw) -> datetime | None:
    """Parse an ISO timestamp to an aware-UTC datetime, or None. Never raises."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Deliver UTC: normalize any foreign offset (e.g. +05:30) to UTC, and treat a naive stamp as
    # already-UTC. Either way floor_tf then sees a true-UTC instant.
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _served_candle(report_path: Path, now_utc: datetime, tf_minutes: int) -> datetime | None:
    """Resolve which candle a completed cycle served, from its report.json. Priority:
    report['candle'] -> floor_tf(report['ran_at']) -> floor_tf(file mtime). All tz-aware UTC.
    A ran_at in the future (clock skew) is discarded so it cannot drive the candle. Returns None
    if the report cannot be read/parsed (caller treats that dir as not-completed)."""
    try:
        rep = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(rep, dict):
        return None  # valid JSON but not an object (null/list/scalar) == not a completed cycle
    ran_at = _parse_utc(rep.get("ran_at"))
    if ran_at is not None and ran_at > now_utc:
        ran_at = None  # future-stamp guard: never let a skewed ran_at wedge the loop
    cand = _parse_utc(rep.get("candle"))
    if cand is None and ran_at is not None:
        cand = floor_tf(ran_at, tf_minutes)
    if cand is None:
        try:
            cand = floor_tf(datetime.fromtimestamp(report_path.stat().st_mtime, tz=UTC), tf_minutes)
        except OSError:
            return None
    return cand


def cycle_due(state_dir, now_utc: datetime, *, tf_minutes: int = 240,
              loop: str | None = None) -> tuple[str, int, str]:
    """Decide whether the candle containing `now_utc` still needs a cycle. Never raises.

    `tf_minutes` is the loop's candle width; `loop` selects the per-loop cycle root
    (state/<loop>/cycle/*). Defaults (240, None) reproduce the legacy single-loop 4h gate on
    state/cycle/*."""
    candle = timedelta(minutes=tf_minutes)
    future_tol = candle
    try:
        assert now_utc.tzinfo is not None and now_utc.utcoffset() == timedelta(0), \
            "now_utc must be tz-aware UTC"
        boundary = floor_tf(now_utc, tf_minutes)
        root = Path(state_dir) / loop / "cycle" if loop else Path(state_dir) / "cycle"

        dirs = sorted(
            (int(p.name) for p in root.glob("*") if p.is_dir() and p.name.isdigit()),
            reverse=True,
        ) if root.exists() else []
        if not dirs:
            return ("FRESH", 1, "cold-start: no cycle dirs")
        highest_dir = dirs[0]

        completed_n: int | None = None
        served: datetime | None = None
        for n in dirs:
            rp = root / str(n) / "report.json"
            if not rp.exists():
                continue  # crashed/in-flight: not a completed cycle
            cand = _served_candle(rp, now_utc, tf_minutes)
            if cand is None:
                continue  # unparseable report == not completed
            if cand > boundary + future_tol:
                continue  # egregiously-future candle (corrupt/skew) -> distrust, scan downward
            completed_n, served = n, cand
            break

        if completed_n is None or served is None:
            # No trustworthy completed cycle. The highest dir is a crashed/junk attempt -> RETRY it
            # (overwrite). Safe: the gate reconciles vs on-disk positions and cannot double-open.
            return ("RETRY", highest_dir, f"no completed cycle; retry/overwrite dir {highest_dir}")

        if served >= boundary:
            nxt = (boundary + candle).isoformat()
            return ("SKIP", completed_n,
                    f"cycle {completed_n} already served candle {served.isoformat()} "
                    f"(>= boundary {boundary.isoformat()}); next boundary {nxt}")

        # This candle is unserved -> DUE. If a higher dir exists with no trustworthy report, it is
        # a crashed current-candle attempt -> RETRY/overwrite it; otherwise a FRESH next cycle.
        if highest_dir > completed_n:
            return ("RETRY", highest_dir,
                    f"cycle {highest_dir} crashed before gate; last completed {completed_n} "
                    f"served {served.isoformat()}")
        return ("FRESH", highest_dir + 1,
                f"new candle {boundary.isoformat()}; last completed {completed_n} "
                f"served {served.isoformat()}")
    except Exception as e:  # noqa: BLE001 — fail SAFE: never swallow a candle on an internal error
        return ("FRESH", 1, f"fail-safe DUE after internal error: {e!r}")
