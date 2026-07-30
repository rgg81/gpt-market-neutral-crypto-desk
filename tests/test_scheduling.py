"""Tests for the multi-cadence due-gate (`futures_fund.scheduling.cycle_due`) and its helpers.

This module is the P0 cadence primitive that `control_loop.cadence_due` wraps with the weekly/daily
timeframes. It is the source of the SKIP/RETRY/FRESH decision and MUST be covered directly (the
module docstring points here as its red-team verification). The predicate gates on the SERVED CANDLE
(`report['candle']` = `floor_tf(gate-start)`) of the highest cycle with a PARSEABLE `report.json` —
never on completion time, never on `max(dir)`. All datetimes are tz-aware UTC.

The market-neutral desk drives this gate per-cadence via `loop=` and `tf_minutes=`:
`cycle_due(..., tf_minutes=1440, loop="daily")` reads `state/daily/cycle/*` and
`cycle_due(..., tf_minutes=10080, loop="weekly")` reads `state/weekly/cycle/*` — the SAME root the
artifact writer uses (CADENCE-ROOT INVARIANT). The legacy default (`tf_minutes=240, loop=None`)
reproduces the single-loop 4h gate on `state/cycle/*`.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import pytest

from futures_fund.scheduling import (
    _parse_utc as parse_utc,
)
from futures_fund.scheduling import (
    _served_candle as served_candle,
)
from futures_fund.scheduling import (
    cycle_due,
    floor4,
    floor_tf,
    tf_to_minutes,
)

DAILY = 1440
WEEKLY = 7 * 1440  # 10080


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _root(state_dir, loop: str | None) -> object:
    """Cycle root the gate scans for `loop`: state/<loop>/cycle (state/cycle when loop is None)."""
    base = state_dir / loop if loop else state_dir
    return base / "cycle"


def _write_report(state_dir, n: int, *, loop: str | None = None, candle: str | None = None,
                  ran_at: str | None = None, mtime: str | None = None, raw: str | None = None):
    """Create <root>/<n>/report.json under the cadence root for `loop`.

    `raw` overrides with literal bytes (for corrupt/non-dict JSON). `mtime` (ISO UTC) sets the file
    mtime via os.utime so the mtime-fallback branch can be exercised deterministically."""
    d = _root(state_dir, loop) / str(n)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "report.json"
    if raw is not None:
        p.write_text(raw)
    else:
        rep: dict = {"cycle": n}
        if candle is not None:
            rep["candle"] = candle
        if ran_at is not None:
            rep["ran_at"] = ran_at
        p.write_text(json.dumps(rep))
    if mtime is not None:
        ts = _dt(mtime).timestamp()
        os.utime(p, (ts, ts))
    return p


def _bare_dir(state_dir, n: int, *, loop: str | None = None):
    """A cycle dir that crashed before the gate: exists, no report.json."""
    d = _root(state_dir, loop) / str(n)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_due(result) -> bool:
    return result[0] in ("FRESH", "RETRY")


# --------------------------------------------------------------------------- tf_to_minutes

def test_tf_to_minutes_units():
    assert tf_to_minutes("15m") == 15
    assert tf_to_minutes("1h") == 60
    assert tf_to_minutes("4h") == 240
    assert tf_to_minutes("1d") == 1440
    assert tf_to_minutes("7d") == 10080  # weekly cadence width


def test_tf_to_minutes_strips_and_lowercases():
    assert tf_to_minutes("  1H ") == 60


@pytest.mark.parametrize("bad", ["1w", "h1", "abc", "10", "1.5h"])
def test_tf_to_minutes_rejects_unknown(bad):
    with pytest.raises(ValueError):
        tf_to_minutes(bad)


def test_tf_to_minutes_empty_raises():
    # Degenerate empty input still raises (IndexError on the unit lookup) — never silently parses.
    with pytest.raises((ValueError, IndexError)):
        tf_to_minutes("")


# --------------------------------------------------------------------------- floor_tf / floor4

def test_floor4_grid():
    assert floor4(_dt("2026-05-31T12:07:00+00:00")) == _dt("2026-05-31T12:00:00+00:00")
    assert floor4(_dt("2026-05-31T15:59:59+00:00")) == _dt("2026-05-31T12:00:00+00:00")
    assert floor4(_dt("2026-05-31T23:30:00+00:00")) == _dt("2026-05-31T20:00:00+00:00")
    assert floor4(_dt("2026-05-31T00:00:00+00:00")) == _dt("2026-05-31T00:00:00+00:00")
    assert floor4(_dt("2026-05-31T03:59:00+00:00")) == _dt("2026-05-31T00:00:00+00:00")


def test_floor_tf_is_floor4_at_240():
    dt = _dt("2026-05-31T13:37:00+00:00")
    assert floor_tf(dt, 240) == floor4(dt)


def test_floor_tf_15m_and_60m_grids():
    assert floor_tf(_dt("2026-05-31T12:07:00+00:00"), 15) == _dt("2026-05-31T12:00:00+00:00")
    assert floor_tf(_dt("2026-05-31T12:59:00+00:00"), 15) == _dt("2026-05-31T12:45:00+00:00")
    assert floor_tf(_dt("2026-05-31T12:59:00+00:00"), 60) == _dt("2026-05-31T12:00:00+00:00")


def test_floor_tf_daily_floors_to_utc_midnight():
    assert floor_tf(_dt("2026-06-11T17:42:00+00:00"), DAILY) == _dt("2026-06-11T00:00:00+00:00")


def test_floor_tf_weekly_groups_whole_week_not_per_day():
    # WEEKLY (10080) must floor to a true week boundary, NOT degenerate to same-day UTC midnight.
    # Mon and Tue of one week share a weekly candle; the next week is a different candle. The grid
    # is epoch-anchored, so week boundaries fall on Thursday 00:00Z (the epoch is a Thursday).
    mon = floor_tf(_dt("2026-06-08T02:00:00+00:00"), WEEKLY)  # Monday
    tue = floor_tf(_dt("2026-06-09T10:00:00+00:00"), WEEKLY)  # Tuesday, same week
    assert mon == tue                                          # same week -> same candle
    assert mon == _dt("2026-06-04T00:00:00+00:00")            # week boundary (a Thursday)
    next_week = floor_tf(_dt("2026-06-11T12:00:00+00:00"), WEEKLY)
    assert next_week == _dt("2026-06-11T00:00:00+00:00")      # following week boundary
    assert next_week != mon                                    # distinct weekly candle
    # exactly 7 days apart -> exactly one weekly step
    assert (next_week - mon).days == 7


def test_floor_tf_rejects_naive():
    with pytest.raises(AssertionError):
        floor_tf(datetime(2026, 5, 31, 12, 0, 0), 240)


def test_floor4_rejects_naive():
    with pytest.raises(AssertionError):
        floor4(datetime(2026, 5, 31, 12, 0, 0))


# --------------------------------------------------------------------------- _parse_utc

def test_parse_utc_z_suffix_normalized():
    assert parse_utc("2026-05-31T08:27:00Z") == _dt("2026-05-31T08:27:00+00:00")


def test_parse_utc_naive_coerced_to_utc():
    out = parse_utc("2026-05-31T08:27:00")
    assert out == _dt("2026-05-31T08:27:00+00:00")
    assert out.tzinfo is not None


def test_parse_utc_foreign_offset_normalized_to_utc():
    # 13:57+05:30 == 08:27 UTC
    assert parse_utc("2026-05-31T13:57:00+05:30") == _dt("2026-05-31T08:27:00+00:00")


@pytest.mark.parametrize("bad", [None, "", "   ", "not-a-date", 123, 4.5, {"x": 1}, []])
def test_parse_utc_returns_none_never_raises(bad):
    assert parse_utc(bad) is None


# ----------------------------------------------------- _served_candle priority

def test_served_candle_prefers_candle_field(tmp_path):
    p = _write_report(tmp_path, 1, loop="daily", candle="2026-06-10T00:00:00+00:00",
                      ran_at="2026-06-11T09:00:00+00:00")
    now = _dt("2026-06-11T12:00:00+00:00")
    # candle field wins even though ran_at would floor to a DIFFERENT (later) candle
    assert served_candle(p, now, DAILY) == _dt("2026-06-10T00:00:00+00:00")


def test_served_candle_falls_back_to_ran_at(tmp_path):
    p = _write_report(tmp_path, 1, loop="daily", ran_at="2026-06-11T09:30:00+00:00")
    now = _dt("2026-06-11T12:00:00+00:00")
    assert served_candle(p, now, DAILY) == _dt("2026-06-11T00:00:00+00:00")


def test_served_candle_future_ran_at_discarded_falls_to_mtime(tmp_path):
    # ran_at in the future must NOT drive the candle; mtime fallback is used instead.
    p = _write_report(tmp_path, 1, loop="daily", ran_at="2026-06-20T00:00:00+00:00",
                      mtime="2026-06-11T09:00:00+00:00")
    now = _dt("2026-06-11T12:00:00+00:00")
    assert served_candle(p, now, DAILY) == _dt("2026-06-11T00:00:00+00:00")


def test_served_candle_mtime_fallback_when_no_fields(tmp_path):
    p = _write_report(tmp_path, 1, loop="daily", mtime="2026-06-11T09:00:00+00:00")
    now = _dt("2026-06-11T12:00:00+00:00")
    assert served_candle(p, now, DAILY) == _dt("2026-06-11T00:00:00+00:00")


def test_served_candle_unparseable_returns_none(tmp_path):
    p = _write_report(tmp_path, 1, loop="daily", raw="{ not json ")
    assert served_candle(p, _dt("2026-06-11T12:00:00+00:00"), DAILY) is None


@pytest.mark.parametrize("raw", ["null", "[1, 2, 3]", "42", '"a string"'])
def test_served_candle_non_dict_returns_none(tmp_path, raw):
    p = _write_report(tmp_path, 1, loop="daily", raw=raw)
    assert served_candle(p, _dt("2026-06-11T12:00:00+00:00"), DAILY) is None


# ----------------------------------------------------- cycle_due: FRESH cold-start

def test_cycle_due_fresh_cold_start_no_root(tmp_path):
    mode, n, _ = cycle_due(tmp_path / "s", _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("FRESH", 1)


def test_cycle_due_fresh_cold_start_empty_root(tmp_path):
    _root(tmp_path, "daily").mkdir(parents=True)  # root exists but holds no numeric dirs
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("FRESH", 1)


# ----------------------------------------------------- cycle_due: FRESH new-candle

def test_cycle_due_fresh_on_new_candle_next_n(tmp_path):
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-10T00:00:00+00:00",
                  ran_at="2026-06-10T00:30:00+00:00")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("FRESH", 8)


def test_cycle_due_weekly_new_candle(tmp_path):
    # week-of-2026-06-01 served; now is the following week -> FRESH next n on the weekly root.
    _write_report(tmp_path, 3, loop="weekly", candle="2026-06-01T00:00:00+00:00",
                  ran_at="2026-06-01T01:00:00+00:00")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=WEEKLY, loop="weekly")
    assert (mode, n) == ("FRESH", 4)


def test_cycle_due_weekly_same_week_skips_does_not_refire_daily(tmp_path):
    # REGRESSION GUARD for the weekly-degenerates-to-daily floor bug. A weekly cycle that ran on
    # Monday (2026-06-08) must SKIP when now is the SAME week (Tuesday 2026-06-09) — the weekly
    # candle still covers it. Under a (broken) per-day floor Mon and Tue land in DIFFERENT candles
    # so the gate would re-fire FRESH every day; only a true week-width floor groups them together.
    # The seeded candle is computed by the gate's own floor_tf so the writer/reader share one grid.
    mon = _dt("2026-06-08T02:00:00+00:00")
    _write_report(tmp_path, 1, loop="weekly", candle=floor_tf(mon, WEEKLY).isoformat(),
                  ran_at="2026-06-08T02:05:00+00:00")
    tue = _dt("2026-06-09T10:00:00+00:00")  # same week as Monday
    # Sanity: both instants must floor to the SAME weekly candle (else the test is vacuous).
    assert floor_tf(mon, WEEKLY) == floor_tf(tue, WEEKLY)
    mode, n, _ = cycle_due(tmp_path, tue, tf_minutes=WEEKLY, loop="weekly")
    assert (mode, n) == ("SKIP", 1)


# ----------------------------------------------------- cycle_due: RETRY crashed-dir

def test_cycle_due_retry_no_completed_cycle(tmp_path):
    _bare_dir(tmp_path, 1, loop="daily")  # crashed before any report written
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("RETRY", 1)


def test_cycle_due_retry_crashed_higher_dir_after_completed(tmp_path):
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-10T00:00:00+00:00",
                  ran_at="2026-06-10T00:30:00+00:00")
    _bare_dir(tmp_path, 8, loop="daily")  # current-candle attempt crashed before gate
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("RETRY", 8)


def test_cycle_due_phantom_high_dir_does_not_stall(tmp_path):
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-10T00:00:00+00:00",
                  ran_at="2026-06-10T00:30:00+00:00")
    _bare_dir(tmp_path, 99, loop="daily")  # phantom empty high dir
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("RETRY", 99)


def test_cycle_due_unparseable_higher_falls_to_prior_then_retry(tmp_path):
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-10T00:00:00+00:00",
                  ran_at="2026-06-10T00:30:00+00:00")
    _write_report(tmp_path, 8, loop="daily", raw="{ this is : not valid json ")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("RETRY", 8)


# ----------------------------------------------------- cycle_due: SKIP served-candle

def test_cycle_due_skip_same_candle(tmp_path):
    now = _dt("2026-06-11T12:00:00+00:00")
    _write_report(tmp_path, 1, loop="daily", candle="2026-06-11T00:00:00+00:00",
                  ran_at="2026-06-11T00:05:00+00:00")
    mode, n, _ = cycle_due(tmp_path, now, tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("SKIP", 1)


def test_cycle_due_skip_via_ran_at_only(tmp_path):
    _write_report(tmp_path, 4, loop="daily", ran_at="2026-06-11T00:25:00+00:00")  # no candle field
    mode, _, _ = cycle_due(tmp_path, _dt("2026-06-11T18:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert mode == "SKIP"


def test_cycle_due_skip_via_mtime_only(tmp_path):
    _write_report(tmp_path, 4, loop="daily", mtime="2026-06-11T00:25:00+00:00")
    mode, _, _ = cycle_due(tmp_path, _dt("2026-06-11T18:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert mode == "SKIP"


def test_cycle_due_late_finish_does_not_steal_next_candle(tmp_path):
    # candle = floor of START even though ran_at crossed into the next day -> next candle still due.
    _write_report(tmp_path, 8, loop="daily", candle="2026-06-10T00:00:00+00:00",
                  ran_at="2026-06-11T00:02:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-06-11T00:07:00+00:00"),
                             tf_minutes=DAILY, loop="daily"))


def test_cycle_due_boundary_exact_instant_already_served_skips(tmp_path):
    # served_candle == boundary, ran_at slightly ahead of now (future-ran_at guard must not flip it)
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-11T00:00:00+00:00",
                  ran_at="2026-06-11T00:00:05+00:00")
    assert cycle_due(tmp_path, _dt("2026-06-11T00:00:00+00:00"),
                     tf_minutes=DAILY, loop="daily")[0] == "SKIP"


def test_cycle_due_future_candle_field_distrusted(tmp_path):
    # cycle 8 carries an egregiously-future candle (>1 step ahead) -> distrust, fall to cycle 7.
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-10T00:00:00+00:00",
                  ran_at="2026-06-10T00:30:00+00:00")
    _write_report(tmp_path, 8, loop="daily", candle="2026-07-01T00:00:00+00:00",
                  ran_at="2026-07-01T00:05:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                             tf_minutes=DAILY, loop="daily"))


def test_cycle_due_clock_moved_backward_bounded_skip(tmp_path):
    # Host clock jumped back inside the same day; served candle is one step ahead of boundary ->
    # trusted -> bounded SKIP (the future_tol window covers exactly one candle).
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-11T00:00:00+00:00",
                  ran_at="2026-06-11T06:00:00+00:00")
    assert cycle_due(tmp_path, _dt("2026-06-10T23:50:00+00:00"),
                     tf_minutes=DAILY, loop="daily")[0] == "SKIP"


# ----------------------------------------------------- cycle_due: loop-root isolation

def test_cycle_due_loop_roots_are_isolated(tmp_path):
    # A served daily candle must NOT mark the weekly loop's candle as served.
    _write_report(tmp_path, 1, loop="daily", candle="2026-06-11T00:00:00+00:00",
                  ran_at="2026-06-11T00:05:00+00:00")
    now = _dt("2026-06-11T12:00:00+00:00")
    assert cycle_due(tmp_path, now, tf_minutes=DAILY, loop="daily")[0] == "SKIP"
    # weekly root state/weekly/cycle/* is empty -> cold start FRESH 1
    assert cycle_due(tmp_path, now, tf_minutes=WEEKLY, loop="weekly") == (
        "FRESH", 1, "cold-start: no cycle dirs")


def test_cycle_due_legacy_default_uses_state_cycle_root(tmp_path):
    # loop=None default reproduces the legacy single-loop 4h gate on state/cycle/*.
    _write_report(tmp_path, 1, loop=None, candle="2026-05-31T12:00:00+00:00",
                  ran_at="2026-05-31T12:10:00+00:00")
    assert cycle_due(tmp_path, _dt("2026-05-31T13:07:00+00:00"))[0] == "SKIP"


# ----------------------------------------------------- cycle_due: fail-safe exception path

def test_cycle_due_naive_now_fails_safe_to_fresh(tmp_path):
    # A naive now_utc trips the tz-aware assertion -> the broad except returns fail-safe FRESH 1.
    mode, n, reason = cycle_due(tmp_path, datetime(2026, 6, 11, 12, 0, 0),
                                tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("FRESH", 1)
    assert "fail-safe" in reason


def test_cycle_due_internal_error_fails_safe(tmp_path, monkeypatch):
    # Force an unexpected error deep inside the gate; it must be caught and return fail-safe DUE,
    # never propagate (a swallowed candle is worse than one extra reconciled run).
    import futures_fund.scheduling as sched

    def boom(*a, **k):
        raise RuntimeError("synthetic floor failure")

    monkeypatch.setattr(sched, "floor_tf", boom)
    mode, n, reason = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                                tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("FRESH", 1)
    assert "fail-safe" in reason and "synthetic floor failure" in reason


def test_cycle_due_never_raises_on_garbage_state(tmp_path):
    root = _root(tmp_path, "daily")
    root.mkdir(parents=True)
    (root / "notanumber").mkdir()                       # non-numeric dir ignored
    _write_report(tmp_path, 5, loop="daily", raw="\x00\x01 garbage")  # binary garbage
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert mode in ("FRESH", "RETRY", "SKIP")


def test_cycle_due_non_numeric_dirs_excluded_from_n(tmp_path):
    _write_report(tmp_path, 7, loop="daily", candle="2026-06-10T00:00:00+00:00",
                  ran_at="2026-06-10T00:30:00+00:00")
    (_root(tmp_path, "daily") / "scratch").mkdir()
    mode, n, _ = cycle_due(tmp_path, _dt("2026-06-11T12:00:00+00:00"),
                           tf_minutes=DAILY, loop="daily")
    assert (mode, n) == ("FRESH", 8)


# ----------------------------------------------------- timezone-host robustness

def test_cycle_due_tz_aware_host_no_mtime_skew(tmp_path):
    """mtime fallback uses fromtimestamp(..., tz=UTC) so a CEST host does not skew the candle."""
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Zurich"
        time.tzset()
        _write_report(tmp_path, 7, loop="daily", mtime="2026-06-10T08:27:00+00:00")
        assert _is_due(cycle_due(tmp_path, _dt("2026-06-11T12:07:00+00:00"),
                                 tf_minutes=DAILY, loop="daily"))
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
