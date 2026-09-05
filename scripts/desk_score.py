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
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.cycle_io import cycle_dir
from futures_fund.durable_io import durable_unlink, durable_write_text
from futures_fund.pending_io import resolve_pending
from futures_fund.performance import canonical_sha256
from futures_fund.prompt_guard import split_managed
from futures_fund.reconcile_commit import completed_cycle_numbers
from futures_fund.reflection import (
    CANONICAL_BTC_SYMBOL,
    DAILY_LEARNING_HORIZON_HOURS,
    SCHEDULED_MARK_TOLERANCE,
    canonical_daily_score_observation,
    horizon_is_on_schedule,
    learning_origin_is_bound,
    reflection_authority_recovery_status,
    score_mature_leg_forecasts,
    score_previous_cycle,
    scored_cycles,
    write_reflection_authority,
)
from futures_fund.scorecard import Recurrence


def _canonical_recurrences(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError("recurrences.json must be a list")
    return [Recurrence.model_validate(item).model_dump(mode="json") for item in raw]


def _write_recurrences(path: Path, recurrences: list[dict]) -> None:
    normalized = json.dumps(recurrences, indent=2, sort_keys=True) + "\n"
    durable_write_text(path, normalized)


def _load_sealed_recurrences(pending: Path) -> tuple[list[dict], str] | None:
    """Validate a prepared recurrence packet without changing either of its files."""
    path = pending / "recurrences.json"
    seal_path = pending / "recurrences.sha256"
    if not seal_path.exists():
        return None
    if not path.exists():
        raise ValueError("recurrences.sha256 exists without recurrences.json")
    digest = seal_path.read_text().strip()
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("recurrences.sha256 is not a lowercase SHA-256 digest")
    canonical = _canonical_recurrences(path)
    if canonical_sha256(canonical) != digest:
        raise ValueError("sealed recurrence packet does not match recurrences.sha256")
    return canonical, digest


def _seal_recurrences(pending: Path) -> str:
    """Schema-normalize and seal the complete recurrence authorization packet."""
    path = pending / "recurrences.json"
    canonical = _canonical_recurrences(path)
    digest = canonical_sha256(canonical)
    seal_path = pending / "recurrences.sha256"
    if seal_path.exists() and seal_path.read_text().strip() != digest:
        raise ValueError("existing recurrences.sha256 conflicts with the current packet")
    _write_recurrences(path, canonical)
    if seal_path.exists():
        return digest
    # The host cycle lock provides exclusivity; the shared durable writer prevents SIGKILL or a
    # power loss from exposing a partially-created immutable seal.
    durable_write_text(seal_path, digest + "\n", mode=0o400)
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
    receipt_recurrences = recovery["receipt"]["recurrences"]
    receipt_sha256 = recovery["receipt"]["recurrences_sha256"]
    sealed = _load_sealed_recurrences(pending)
    if recovery["status"] == "unconsumed":
        if sealed is not None and sealed != (receipt_recurrences, receipt_sha256):
            raise ValueError("pending recurrence seal conflicts with unconsumed authority")
        if sealed is None:
            _write_recurrences(pending / "recurrences.json", receipt_recurrences)
            recurrences_sha256 = _seal_recurrences(pending)
        else:
            recurrences_sha256 = sealed[1]
        recurrences = receipt_recurrences
    else:
        # A consumed authority is the sole permission to rotate its ephemeral pending packet to an
        # empty stand-down. Validate any old prepared packet before removing its immutable seal;
        # the authority receipt retains the original packet permanently.
        empty: list[dict] = []
        empty_sha256 = canonical_sha256(empty)
        if (
            sealed is not None
            and sealed != (receipt_recurrences, receipt_sha256)
            and sealed != (empty, empty_sha256)
        ):
            raise ValueError("pending recurrence seal conflicts with consumed authority")
        if sealed != (empty, empty_sha256):
            durable_unlink(pending / "recurrences.sha256")
            _write_recurrences(pending / "recurrences.json", empty)
            recurrences_sha256 = _seal_recurrences(pending)
        else:
            recurrences_sha256 = empty_sha256
        durable_unlink(pending / "reflection.json")
        recurrences = empty
    return {
        "reflection_authority_recovery": recovery["status"],
        "recurrences": recurrences,
        "recurrences_sha256": recurrences_sha256,
    }


def _recover_sealed_packet_before_authority(
    state_dir: str,
    memory_dir: str,
    pending: Path,
    source_cycle: int,
) -> dict | None:
    """Finish the crash boundary where the packet seal exists but its authority does not."""
    sealed = _load_sealed_recurrences(pending)
    if sealed is None:
        return None
    recurrences, recurrences_sha256 = sealed
    write_reflection_authority(
        state_dir,
        memory_dir,
        source_cycle=source_cycle,
        recurrences_sha256=recurrences_sha256,
        recurrences=recurrences,
    )
    return {
        "reflection_authority_recovery": "sealed_packet_before_authority",
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
    if btc_symbol != CANONICAL_BTC_SYMBOL:
        raise ValueError(f"daily score benchmark must be {CANONICAL_BTC_SYMBOL}")
    completed = completed_cycle_numbers(state_dir, cadence=cadence)
    seen = scored_cycles(memory_dir, state_dir=state_dir, cadence=cadence)
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
        eligible = canonical_daily_score_observation(
            state_dir, origin_cycle, cadence=cadence
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
    recurrence_refresh: dict | None = None
    if not results and seen:
        # The score row is the durable side of score_previous_cycle's write-ahead boundary. A host
        # crash after that replace but before recurrences.json must not make the now-seen cycle skip
        # its feedback forever. Replay the newest strictly state-verified row against its one
        # canonical observation; immutable attribution keeps the label unchanged while recurrence
        # detection is recomputed over the complete verified scorecard.
        refresh_cycle = max(seen)
        observation = canonical_daily_score_observation(
            state_dir, refresh_cycle, cadence=cadence
        )
        if observation is None:
            raise ValueError(
                f"verified score cycle {refresh_cycle} lost its canonical outcome during refresh"
            )
        observation_cycle, observation_ts, marks, artifact_sha256 = observation
        recurrence_refresh = score_previous_cycle(
            state_dir,
            memory_dir,
            scored_cycle=refresh_cycle,
            cur_marks=marks,
            now=observation_ts.isoformat(),
            btc_symbol=btc_symbol,
            cadence=cadence,
            active_calibration_roles=active_calibration_roles,
            outcome_observation_cycle=observation_cycle,
            outcome_scoring_marks_sha256=artifact_sha256,
        )
    elif not results:
        pending, _meta = resolve_pending(memory_dir)
        _write_recurrences(pending / "recurrences.json", [])
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
        "recurrence_refresh_cycle": (
            int(recurrence_refresh["scored_cycle"])
            if recurrence_refresh is not None
            else None
        ),
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
    scoring_attempted = False
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
        sealed_recovery = _recover_sealed_packet_before_authority(
            args.state_dir, args.memory_dir, pending, int(meta["cycle"])
        )
        if sealed_recovery is not None:
            print(json.dumps(sealed_recovery, indent=2))
            return 0
        scoring_attempted = True
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
    except Exception as exc:  # noqa: BLE001 — learning is fail-soft unless recovery is untrusted
        pending.mkdir(parents=True, exist_ok=True)
        try:
            sealed = _load_sealed_recurrences(pending)
            if sealed is None and scoring_attempted and meta is not None:
                # Retry a partially-published score exactly once. If its scorecard row made it to
                # disk, score_eligible_completed_cycles now refreshes recurrence state from that
                # verified row even though no new origin remains to score.
                try:
                    score_eligible_completed_cycles(
                        args.state_dir,
                        args.memory_dir,
                        btc_symbol=meta["btc_symbol"],
                        active_calibration_roles=_active_calibration_roles(
                            Path(args.agents_dir)
                        ),
                    )
                    outcome = "replayed score state and sealed its recurrence packet"
                except Exception:  # noqa: BLE001 — ordinary learning failure remains fail-soft
                    _write_recurrences(pending / "recurrences.json", [])
                    outcome = "wrote sealed empty recurrences after replay also failed"
                recurrences_sha256 = _seal_recurrences(pending)
                recurrences = _canonical_recurrences(pending / "recurrences.json")
            elif sealed is None:
                _write_recurrences(pending / "recurrences.json", [])
                recurrences_sha256 = _seal_recurrences(pending)
                recurrences = []
                outcome = "wrote sealed empty recurrences.json"
            else:
                recurrences, recurrences_sha256 = sealed
                outcome = "validated and retained the prior sealed recurrence packet"
            if meta is not None:
                write_reflection_authority(
                    args.state_dir,
                    args.memory_dir,
                    source_cycle=int(meta["cycle"]),
                    recurrences_sha256=recurrences_sha256,
                    recurrences=recurrences,
                )
        except Exception:  # noqa: BLE001 — never bless or overwrite a corrupt prepared packet
            print(
                "desk_score recovery failed; recurrence authority was not changed",
                file=sys.stderr,
            )
            traceback.print_exc()
            return 1
        print(f"desk_score failed (fail-soft); {outcome}", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
