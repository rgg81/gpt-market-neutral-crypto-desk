"""Cycle step 2 (deterministic, FAIL-SOFT): score every eligible unscored cycle against the
earliest later COMMITTED scoring marks, then write recurrences the Reflector may act on.

    uv run python scripts/desk_score.py --state-dir live_state --memory-dir live_memory

Runs right after desk_evidence, but the current pending attempt is never a learning label. Its
marks become eligible only after reconcile publishes the cycle's hash-bound completion marker.
Any error is logged and `<memory>/pending/recurrences.json` becomes `[]` so the cycle proceeds on
the current prompts. PAPER ONLY."""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.cycle_io import cycle_dir
from futures_fund.pending_io import resolve_pending
from futures_fund.performance import canonical_sha256
from futures_fund.prompt_guard import split_managed
from futures_fund.reconcile_commit import completed_cycle_numbers
from futures_fund.reflection import (
    DAILY_LEARNING_HORIZON_HOURS,
    SCHEDULED_MARK_TOLERANCE,
    committed_scoring_observations,
    horizon_is_on_schedule,
    learning_origin_is_bound,
    reflection_authority_recovery_status,
    score_mature_leg_forecasts,
    score_previous_cycle,
    scored_cycles,
    write_reflection_authority,
)
from futures_fund.scorecard import Recurrence


def _seal_recurrences(pending: Path) -> str:
    """Schema-normalize and seal the complete recurrence authorization packet."""
    path = pending / "recurrences.json"
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError("recurrences.json must be a list")
    canonical = [
        Recurrence.model_validate(item).model_dump(mode="json") for item in raw
    ]
    digest = canonical_sha256(canonical)
    normalized = json.dumps(canonical, indent=2, sort_keys=True) + "\n"
    seal_path = pending / "recurrences.sha256"
    if seal_path.exists() and seal_path.read_text().strip() != digest:
        raise ValueError("existing recurrences.sha256 conflicts with the current packet")
    path_tmp = path.with_suffix(path.suffix + ".tmp")
    path_tmp.write_text(normalized)
    os.replace(path_tmp, path)
    if seal_path.exists():
        return digest
    fd = os.open(seal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        os.write(fd, (digest + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return digest


def _recover_reflection_authority_packet(
    state_dir: str, memory_dir: str, pending: Path, source_cycle: int
) -> dict | None:
    """Restore an unconsumed packet, or publish empty after that cycle already consumed it."""
    recovery = reflection_authority_recovery_status(
        state_dir, memory_dir, source_cycle
    )
    if recovery is None:
        return None
    recurrences = (
        recovery["receipt"]["recurrences"]
        if recovery["status"] == "unconsumed"
        else []
    )
    (pending / "recurrences.json").write_text(json.dumps(recurrences, indent=2) + "\n")
    recurrences_sha256 = _seal_recurrences(pending)
    return {
        "reflection_authority_recovery": recovery["status"],
        "recurrences": recurrences,
        "recurrences_sha256": recurrences_sha256,
    }


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _active_calibration_roles(agents_dir: Path) -> set[str]:
    roles: set[str] = set()
    for role in ("sentiment", "technical", "futures", "pm", "adversary"):
        path = agents_dir / f"{role}.md"
        if path.exists() and split_managed(path.read_text())[1].strip():
            roles.add(role)
    return roles


def score_eligible_completed_cycles(
    state_dir: str,
    memory_dir: str,
    *,
    btc_symbol: str,
    active_calibration_roles: set[str],
    cadence: str = "rebal",
) -> dict:
    """Catch up every unscored origin at its earliest complete, all-symbol mark packet."""
    completed = completed_cycle_numbers(state_dir, cadence=cadence)
    seen = scored_cycles(memory_dir, state_dir=state_dir, cadence=cadence)
    observations = committed_scoring_observations(state_dir, cadence=cadence)
    results: list[dict] = []
    waiting: list[int] = []
    off_horizon_scored: list[dict] = []
    for origin_cycle in completed:
        if origin_cycle in seen or not learning_origin_is_bound(
            state_dir, origin_cycle, cadence=cadence
        ):
            continue
        origin_dir = cycle_dir(state_dir, origin_cycle, cadence=cadence)
        evidence_path = origin_dir / "evidence.json"
        if not evidence_path.exists():
            waiting.append(origin_cycle)
            continue
        evidence = json.loads(evidence_path.read_text())
        report_path = origin_dir / "report.json"
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
        origin_raw = report.get("decision_ts") or (
            evidence[0].get("as_of_ts") if evidence else None
        )
        try:
            origin_ts = _as_utc(str(origin_raw))
        except (TypeError, ValueError):
            waiting.append(origin_cycle)
            continue
        scheduled_target = origin_ts + timedelta(hours=DAILY_LEARNING_HORIZON_HOURS)
        required = {
            str(row["symbol"])
            for row in evidence
            if isinstance(row, dict) and row.get("symbol")
        }
        required.add(btc_symbol)
        eligible = next(
            (
                (observation_cycle, observation_ts, marks, artifact_sha256)
                for observation_cycle, observation_ts, marks, artifact_sha256 in observations
                if observation_cycle > origin_cycle
                and observation_ts >= scheduled_target - SCHEDULED_MARK_TOLERANCE
                and required.issubset(marks)
            ),
            None,
        )
        if eligible is None:
            waiting.append(origin_cycle)
            continue
        observation_cycle, observation_ts, marks, artifact_sha256 = eligible
        result = score_previous_cycle(
            state_dir,
            memory_dir,
            scored_cycle=origin_cycle,
            cur_marks=marks,
            now=observation_ts.isoformat(),
            btc_symbol=btc_symbol,
            cadence=cadence,
            active_calibration_roles=active_calibration_roles,
            outcome_observation_cycle=observation_cycle,
            outcome_scoring_marks_sha256=artifact_sha256,
        )
        results.append(result)
        elapsed_hours = (observation_ts - origin_ts).total_seconds() / 3600.0
        if not horizon_is_on_schedule(elapsed_hours):
            off_horizon_scored.append({
                "origin_cycle": origin_cycle,
                "outcome_observation_cycle": observation_cycle,
                "evaluation_horizon_hours": elapsed_hours,
                "scheduled_horizon_hours": DAILY_LEARNING_HORIZON_HOURS,
                "learning_eligible": False,
            })
        seen.add(origin_cycle)
    if not results:
        pending, _meta = resolve_pending(memory_dir)
        (pending / "recurrences.json").write_text("[]")
    forecast_status = score_mature_leg_forecasts(
        state_dir,
        memory_dir,
        through_cycle=max(completed, default=0),
        btc_symbol=btc_symbol,
        cadence=cadence,
    )
    return {
        "scored_cycles": [int(result["scored_cycle"]) for result in results],
        "daily_learning_horizon_hours": DAILY_LEARNING_HORIZON_HOURS,
        "scheduled_mark_tolerance_minutes": (
            SCHEDULED_MARK_TOLERANCE.total_seconds() / 60.0
        ),
        "off_horizon_scores_retained_for_audit": off_horizon_scored,
        "unscored_waiting_for_committed_marks": waiting,
        "latest_result": results[-1] if results else None,
        **forecast_status,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score the previous desk cycle (fail-soft).")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    ap.add_argument("--agents-dir", default="agents")
    args = ap.parse_args(argv)
    pending = Path(args.memory_dir) / "pending"
    meta: dict | None = None
    try:
        pending, meta = resolve_pending(args.memory_dir)
        scoring = json.loads((pending / "scoring_marks.json").read_text())
        if canonical_sha256(scoring) != meta.get("scoring_marks_sha256"):
            raise ValueError("scoring marks do not match cycle meta")
        # Pending marks prove this attempt's data packet is intact, but are never learning labels.
        # Only a later reconcile commit makes scoring_marks eligible for immutable attribution.
        pending_marks = {symbol: float(mark) for symbol, mark in scoring["marks"].items()}
        if not pending_marks:
            raise ValueError("pending scoring marks are empty")
        authority_recovery = _recover_reflection_authority_packet(
            args.state_dir, args.memory_dir, pending, int(meta["cycle"])
        )
        if authority_recovery is not None:
            print(json.dumps(authority_recovery, indent=2))
            return 0
        res = score_eligible_completed_cycles(
            args.state_dir,
            args.memory_dir,
            btc_symbol=meta["btc_symbol"],
            active_calibration_roles=_active_calibration_roles(Path(args.agents_dir)),
        )
        recurrences_sha256 = _seal_recurrences(pending)
        write_reflection_authority(
            args.state_dir,
            args.memory_dir,
            source_cycle=int(meta["cycle"]),
            recurrences_sha256=recurrences_sha256,
            recurrences=json.loads((pending / "recurrences.json").read_text()),
        )
        res["recurrences_sha256"] = recurrences_sha256
        print(json.dumps(res, indent=2))
    except Exception:  # noqa: BLE001 — learning is fail-soft; never block the cycle
        pending.mkdir(parents=True, exist_ok=True)
        if not (pending / "recurrences.sha256").exists():
            (pending / "recurrences.json").write_text("[]")
            recurrences_sha256 = _seal_recurrences(pending)
            outcome = "wrote sealed empty recurrences.json"
        else:
            recurrences_sha256 = (pending / "recurrences.sha256").read_text().strip()
            outcome = "left the prior sealed recurrence packet untouched"
        if meta is not None:
            write_reflection_authority(
                args.state_dir,
                args.memory_dir,
                source_cycle=int(meta["cycle"]),
                recurrences_sha256=recurrences_sha256,
                recurrences=json.loads((pending / "recurrences.json").read_text()),
            )
        print(f"desk_score failed (fail-soft); {outcome}", file=sys.stderr)
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
