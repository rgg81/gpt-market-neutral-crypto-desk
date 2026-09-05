"""File-orchestration glue for the self-learning loop.

Pure file IO — NO network, NO git — so it is fully testable offline. The thin CLI scripts
(`scripts/desk_score.py`, `scripts/reflector_apply.py`) add settings, snapshots, journaling, and
optional Git audit around these."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.desk_contracts import Book, BookLeg, ReflectionProposal, SpecialistRead
from futures_fund.heartbeat import verify_heartbeat_completion
from futures_fund.prompt_guard import (
    PromptGuardError,
    assert_only_region_changed,
    assert_valid_region,
    splice_managed,
    split_managed,
)
from futures_fund.reconcile_commit import (
    completed_artifact_is_bound,
    completed_artifact_sha256,
    completed_cycle_numbers,
)
from futures_fund.scorecard import (
    Recurrence,
    ScoreRecord,
    classify_objections,
    detect_recurrences,
    forward_returns,
    realized_edge_frac,
    score_book,
    score_candidate_opportunities,
    score_specialist,
)

SPECIALIST_ROLES = ("sentiment", "technical", "futures")
RECURRENCE_COOLDOWN_CYCLES = 3
DAILY_LEARNING_HORIZON_HOURS = 24.0
# A full cycle is scheduled for the same UTC slot every day, but process/network jitter can put a
# committed mark a few seconds before the exact decision timestamp anniversary. Treat that mark
# as the scheduled observation without pretending the actual elapsed time was exactly 24h.
SCHEDULED_MARK_TOLERANCE = timedelta(minutes=5)
FORECAST_SCORE_SCHEMA_VERSION = 4
CANDIDATE_SCORE_SCHEMA_VERSION = 1
FORECAST_EDGE_CHANGE_ABS_FRAC = 0.0025
FORECAST_EDGE_CHANGE_REL_FRAC = 0.25
ADVERSARY_RECOVERY_WINDOW = 6


def _marks_sha256(marks: dict[str, float]) -> str:
    encoded = json.dumps(marks, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _as_utc(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def horizon_is_on_schedule(
    elapsed_hours: float, *, target_hours: float = DAILY_LEARNING_HORIZON_HOURS
) -> bool:
    """Whether an actual elapsed label is within the explicit scheduled-slot tolerance."""
    tolerance_hours = SCHEDULED_MARK_TOLERANCE.total_seconds() / 3600.0
    return abs(float(elapsed_hours) - float(target_hours)) <= tolerance_hours + 1e-12


def daily_score_is_learning_eligible(record: ScoreRecord) -> bool:
    """Only comparable, approximately 24h committed labels may tune daily desk behavior."""
    return record.outcome_provenance == "manifest_bound" and horizon_is_on_schedule(
        record.evaluation_horizon_hours
    )


def _adversary_recovery_recurrences(records: list[ScoreRecord]) -> list[Recurrence]:
    """Surface the active Adversary note's exact positive retirement condition.

    Only structurally manifest-bound, scheduled-24h labels participate here.  The production
    caller additionally rebinds those rows to completed artifacts before invoking this helper.
    A full trailing-six window is mandatory; fewer observations cannot prove the note's explicit
    ``accepted_losing_originals <= 1`` condition.
    """
    eligible = [record for record in records if daily_score_is_learning_eligible(record)]
    recent = eligible[-ADVERSARY_RECOVERY_WINDOW:]
    if len(recent) < ADVERSARY_RECOVERY_WINDOW:
        return []
    if any(
        record.adv_accepted and not record.adv_revised and realized_edge_frac(record.book) is None
        for record in recent
    ):
        # An accepted original without a comparable realized label cannot prove it was non-losing.
        return []
    accepted_losing = [
        record
        for record in recent
        if record.adv_accepted
        and not record.adv_revised
        and realized_edge_frac(record.book) is not None
        and realized_edge_frac(record.book) < 0.0
    ]
    if len(accepted_losing) > 1:
        return []

    evidence: list[str] = []
    for record in recent:
        edge = realized_edge_frac(record.book)
        accepted_original = record.adv_accepted and not record.adv_revised
        if accepted_original and edge is not None and edge < 0.0:
            status = "accepted_losing_original"
        elif accepted_original and edge is not None:
            status = "accepted_non_losing_original"
        elif accepted_original:
            status = "accepted_original_outcome_unavailable"
        else:
            status = "revision_demanded"
        edge_text = "unavailable" if edge is None else f"{edge:+.4f}"
        evidence.append(f"c{record.cycle}: {status}, realized_edge_ex_funding_frac={edge_text}")
    return [
        Recurrence(
            kind="adversary_recovered",
            role="adversary",
            count=ADVERSARY_RECOVERY_WINDOW - len(accepted_losing),
            window=ADVERSARY_RECOVERY_WINDOW,
            evidence=evidence,
            suggestion=(
                f"the trailing {ADVERSARY_RECOVERY_WINDOW} manifest-bound scheduled-horizon cycles "
                f"contain {len(accepted_losing)} accepted losing original(s), satisfying the "
                "active note's <=1 retirement condition; inspect that exact note and retire or "
                "narrow only its auto-managed performance calibration."
            ),
        )
    ]


def _read_scorecard(path: Path, *, state_dir=None, cadence: str = "rebal") -> list[ScoreRecord]:
    by_cycle: dict[int, ScoreRecord] = {}
    if not path.exists():
        return []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = ScoreRecord.model_validate_json(line)
            if (
                state_dir is not None
                and record.outcome_provenance == "manifest_bound"
                and not score_record_is_manifest_bound(state_dir, record, cadence=cadence)
            ):
                raise ValueError(f"score cycle {record.cycle} is not bound to committed artifacts")
            prior = by_cycle.get(record.cycle)
            if prior is not None and prior != record:
                raise ValueError(f"conflicting duplicate scorecard cycle {record.cycle}")
            by_cycle.setdefault(record.cycle, record)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid scorecard row {path}:{line_number}") from exc
    return [by_cycle[cycle] for cycle in sorted(by_cycle)]


def _write_scorecard(path: Path, records: list[ScoreRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(record.model_dump_json() + "\n" for record in records))
    os.replace(tmp, path)


def _archive_unverified_score(memory_dir, record: ScoreRecord, *, replaced_at: str) -> None:
    """Preserve a superseded legacy label without letting it steer calibration."""
    path = Path(memory_dir) / "legacy-scorecard-archive.jsonl"
    rows = []
    if path.exists():
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                int(row["record"]["cycle"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid legacy score archive {path}:{line_number}") from exc
            rows.append(row)
    matches = [row for row in rows if int(row["record"]["cycle"]) == record.cycle]
    candidate = {
        "replaced_at": replaced_at,
        "reason": "legacy outcome lacked a manifest-bound scoring observation",
        "record": record.model_dump(mode="json"),
    }
    if matches:
        if matches != [candidate]:
            raise ValueError(f"conflicting legacy score archive cycle {record.cycle}")
        return
    rows.append(candidate)
    rows.sort(key=lambda row: int(row["record"]["cycle"]))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    os.replace(tmp, path)


def _filter_recurrences(
    recurrences,
    *,
    memory_dir,
    scored_cycle: int,
    active_calibration_roles: set[str] | None,
):
    handled_path = Path(memory_dir) / "recurrence-handled.json"
    handled = _read_json(handled_path, {})
    if not isinstance(handled, dict):
        handled = {}
    requires_active_note = {
        "specialist_inactive",
        "specialist_recovered",
        "pm_recovered",
        "pm_gate_inactive",
        "adversary_recovered",
    }
    out = []
    for recurrence in recurrences:
        if (
            active_calibration_roles is not None
            and recurrence.kind in requires_active_note
            and recurrence.role not in active_calibration_roles
        ):
            continue
        prior = handled.get(f"{recurrence.kind}:{recurrence.role}", {})
        try:
            handled_cycle = int(prior["cycle"])
        except (KeyError, TypeError, ValueError):
            handled_cycle = -RECURRENCE_COOLDOWN_CYCLES
        if scored_cycle - handled_cycle < RECURRENCE_COOLDOWN_CYCLES:
            continue
        out.append(recurrence)
    return out


def mark_recurrences_handled(memory_dir, recurrences: list[dict], *, cycle: int) -> None:
    """Record a completed Reflector consideration, including an explicit no-action result."""
    path = Path(memory_dir) / "recurrence-handled.json"
    handled = _read_json(path, {})
    if not isinstance(handled, dict):
        handled = {}
    for recurrence in recurrences:
        kind = str(recurrence.get("kind", ""))
        role = str(recurrence.get("role", ""))
        if kind and role:
            handled[f"{kind}:{role}"] = {"cycle": int(cycle)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(handled, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def persist_decision_snapshot(
    state_dir, cycle: int, *, evidence: list[dict], pending_dir, cadence: str = "rebal"
) -> None:
    """Save this cycle's evidence snapshot (marks + beta fields) and, if a revision happened, the
    pre-revision book, into the cycle dir for future scoring."""
    save_output(state_dir, cycle, "evidence", evidence, cadence=cadence)
    orig = Path(pending_dir) / "pm_book_original.json"
    if orig.exists():
        save_output(
            state_dir, cycle, "book_original", json.loads(orig.read_text()), cadence=cadence
        )


def _read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def read_forecast_scorecard(
    path: Path,
    *,
    state_dir=None,
    cadence: str = "rebal",
    btc_symbol: str = "BTC/USDT:USDT",
) -> list[dict]:
    """Read immutable leg outcomes strictly and reject conflicting learning evidence."""
    if not path.exists():
        return []
    rows: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError("forecast row must be an object")
            key = (int(row["origin_cycle"]), str(row["symbol"]))
            if key[0] < 1 or not key[1]:
                raise ValueError("invalid forecast key")
            schema_version = int(row.get("forecast_score_schema_version", 1))
            if schema_version not in (1, 2, 3, FORECAST_SCORE_SCHEMA_VERSION):
                raise ValueError("unsupported forecast score schema")
            if schema_version == 1:
                if not isinstance(row.get("sign_hit"), bool):
                    raise ValueError("legacy sign_hit must be boolean")
            elif row.get("sign_hit") not in (True, False, None):
                raise ValueError("sign_hit must be boolean or null")
            observation_cycle = int(row["outcome_observation_cycle"])
            observation_sha256 = str(row["outcome_scoring_marks_sha256"])
            if observation_cycle <= key[0] or len(observation_sha256) != 64:
                raise ValueError("invalid committed forecast observation identity")
            for field in (
                "predicted_selected_edge_frac",
                "realized_selected_edge_frac",
                "forecast_error_frac",
            ):
                if not math.isfinite(float(row[field])):
                    raise ValueError(f"non-finite {field}")
            for field in (
                "forecast_horizon_hours",
                "evaluation_horizon_hours",
                "target_notional",
                "origin_standalone_vol_usd",
                "origin_mark",
                "evaluation_mark",
                "beta_clamped",
                "btc_origin_mark",
                "btc_evaluation_mark",
                "realized_beta_adjusted_return_frac",
            ):
                if row.get(field) is not None and not math.isfinite(float(row[field])):
                    raise ValueError(f"non-finite {field}")
            for field in (
                "forecast_horizon_hours",
                "evaluation_horizon_hours",
                "target_notional",
                "origin_standalone_vol_usd",
            ):
                if row.get(field) is not None and float(row[field]) < 0.0:
                    raise ValueError(f"negative {field}")
            predicted = float(row["predicted_selected_edge_frac"])
            realized = float(row["realized_selected_edge_frac"])
            if not math.isclose(
                float(row["forecast_error_frac"]),
                realized - predicted,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("forecast_error_frac is inconsistent")
            if schema_version == 1:
                # Version 1 unfortunately named selected-side profitability ``sign_hit``.
                # Preserve immutable historical rows, but never let this ambiguous field enter
                # the version-2 effective calibration sample.
                if row["sign_hit"] is not (realized > 0.0):
                    raise ValueError("legacy sign_hit is inconsistent")
            else:
                selected_profitable = realized > 0.0
                predicted_direction = 1 if predicted > 0.0 else -1 if predicted < 0.0 else 0
                realized_direction = 1 if realized > 0.0 else -1 if realized < 0.0 else 0
                directional_hit = (
                    None if predicted_direction == 0 else predicted_direction == realized_direction
                )
                if row.get("selected_side_profitable") is not selected_profitable:
                    raise ValueError("selected_side_profitable is inconsistent")
                if row.get("directional_forecast_hit") is not directional_hit:
                    raise ValueError("directional_forecast_hit is inconsistent")
                if row.get("sign_hit") is not directional_hit:
                    raise ValueError("sign_hit is inconsistent with directional accuracy")
                elapsed = float(row["evaluation_horizon_hours"])
                declared = float(row["forecast_horizon_hours"])
                on_horizon = horizon_is_on_schedule(elapsed, target_hours=declared)
                if row.get("horizon_label_eligible") is not on_horizon:
                    raise ValueError("horizon_label_eligible is inconsistent")
                decision_eligible = bool(row.get("decision_learning_eligible"))
                expected_learning_eligible = on_horizon and decision_eligible
                if schema_version == 3:
                    expected_learning_eligible = (
                        expected_learning_eligible and row.get("statistically_independent") is True
                    )
                elif schema_version >= 4:
                    expected_learning_eligible = (
                        expected_learning_eligible and row.get("leg_nonoverlap_eligible") is True
                    )
                if row.get("learning_eligible") is not expected_learning_eligible:
                    raise ValueError("learning_eligible is inconsistent")
                if schema_version <= 3 and not isinstance(
                    row.get("statistically_independent"), bool
                ):
                    raise ValueError("statistically_independent must be boolean")
                if schema_version >= 4:
                    if row.get("statistically_independent") is not None:
                        raise ValueError("leg rows cannot self-declare statistical independence")
                    if not isinstance(row.get("leg_nonoverlap_eligible"), bool):
                        raise ValueError("leg_nonoverlap_eligible must be boolean")
            if state_dir is not None:
                expected = _build_forecast_score_row(
                    state_dir,
                    origin_cycle=key[0],
                    symbol=key[1],
                    btc_symbol=btc_symbol,
                    cadence=cadence,
                    policy_version=schema_version,
                )
                if expected is None or expected != row:
                    raise ValueError(
                        "forecast row does not match the deterministic earliest committed outcome"
                    )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid forecast score row {path}:{line_number}") from exc
        if key in seen:
            raise ValueError(f"duplicate forecast score {key}")
        seen.add(key)
        rows.append(row)
    return rows


def _build_candidate_score_row(
    state_dir,
    *,
    origin_cycle: int,
    symbol: str,
    side: str,
    btc_symbol: str,
    cadence: str,
) -> dict | None:
    """Build one PM-declared candidate's first scheduled-horizon shadow outcome."""
    if not learning_origin_is_bound(state_dir, origin_cycle, cadence=cadence):
        return None
    origin_dir = cycle_dir(state_dir, origin_cycle, cadence=cadence)
    book = Book.model_validate(_read_json(origin_dir / "book.json", {"legs": []}))
    reads_raw = _read_json(origin_dir / "reads.json", {})
    parsed_reads = {
        role: [SpecialistRead.model_validate(row) for row in reads_raw.get(role, [])]
        for role in SPECIALIST_ROLES
    }
    book.validate_candidate_review_coverage(parsed_reads)
    candidate = next(
        (item for item in book.candidate_reviews if item.symbol == symbol and item.side == side),
        None,
    )
    if candidate is None:
        return None
    book_sha256 = completed_artifact_sha256(state_dir, origin_cycle, "book", cadence=cadence)
    reads_sha256 = completed_artifact_sha256(state_dir, origin_cycle, "reads", cadence=cadence)
    policy_sha256 = completed_artifact_sha256(
        state_dir, origin_cycle, "entry_gate_policy", cadence=cadence
    )
    if not book_sha256 or not reads_sha256 or not policy_sha256:
        return None
    evidence = _read_json(origin_dir / "evidence.json", [])
    ev_by_symbol = {str(row["symbol"]): row for row in evidence}
    origin_row = ev_by_symbol.get(symbol)
    origin_mark = float((origin_row or {}).get("mark") or 0.0)
    btc_origin_mark = float(ev_by_symbol.get(btc_symbol, {}).get("mark") or 0.0)
    report = _read_json(origin_dir / "report.json", {})
    origin_raw = report.get("decision_ts") or (evidence[0].get("as_of_ts") if evidence else None)
    try:
        origin_ts = _as_utc(str(origin_raw))
    except (TypeError, ValueError):
        return None
    if origin_mark <= 0.0 or btc_origin_mark <= 0.0:
        return None
    maturity = origin_ts + timedelta(hours=candidate.edge_horizon_hours)
    eligible = next(
        (
            observation
            for observation in committed_scoring_observations(state_dir, cadence=cadence)
            if observation[1] >= maturity - SCHEDULED_MARK_TOLERANCE
            and float(observation[2].get(symbol, 0.0) or 0.0) > 0.0
            and float(observation[2].get(btc_symbol, 0.0) or 0.0) > 0.0
        ),
        None,
    )
    if eligible is None:
        return None
    outcome_cycle, outcome_ts, outcome_marks, outcome_sha256 = eligible
    elapsed = (outcome_ts - origin_ts).total_seconds() / 3600.0
    current_mark = float(outcome_marks[symbol])
    current_btc = float(outcome_marks[btc_symbol])
    beta = float((origin_row or {}).get("beta_clamped", (origin_row or {}).get("beta_btc", 1.0)))
    relative_return = (
        current_mark / origin_mark - 1.0 - beta * (current_btc / btc_origin_mark - 1.0)
    )
    selected_edge = (1.0 if side == "long" else -1.0) * relative_return
    on_horizon = horizon_is_on_schedule(elapsed, target_hours=candidate.edge_horizon_hours)
    candidate_payload = candidate.model_dump(mode="json")
    candidate_sha256 = sha256(
        json.dumps(candidate_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "candidate_score_schema_version": CANDIDATE_SCORE_SCHEMA_VERSION,
        "origin_cycle": origin_cycle,
        "symbol": symbol,
        "side": side,
        "status": candidate.status,
        "exclusion_reason": candidate.exclusion_reason,
        "gate_causal_claim": candidate.exclusion_reason == "entry_gate",
        "origin_ts": origin_ts.isoformat(),
        "scheduled_maturity_ts": maturity.isoformat(),
        "evaluated_at": outcome_ts.isoformat(),
        "forecast_horizon_hours": candidate.edge_horizon_hours,
        "evaluation_horizon_hours": elapsed,
        "horizon_label_eligible": on_horizon,
        "horizon_status": "on_schedule" if on_horizon else "off_horizon_late",
        "expected_price_edge_frac": candidate.expected_price_edge_frac,
        "counterfactual_notional": candidate.counterfactual_notional,
        "realized_beta_adjusted_return_frac": relative_return,
        "realized_selected_edge_frac": selected_edge,
        "counterfactual_price_pnl": selected_edge * candidate.counterfactual_notional,
        "forecast_error_frac": selected_edge - candidate.expected_price_edge_frac,
        "selected_side_profitable": selected_edge > 0.0,
        "origin_mark": origin_mark,
        "evaluation_mark": current_mark,
        "beta_clamped": beta,
        "btc_origin_mark": btc_origin_mark,
        "btc_evaluation_mark": current_btc,
        "outcome_observation_cycle": outcome_cycle,
        "outcome_scoring_marks_sha256": outcome_sha256,
        "outcome_marks_sha256": _marks_sha256(outcome_marks),
        "candidate_sha256": candidate_sha256,
        "book_sha256": book_sha256,
        "reads_sha256": reads_sha256,
        "specialist_reads_sha256": book.specialist_reads_sha256 or "",
        "entry_gate_policy_sha256": policy_sha256,
        "research_use": "shadow_opportunity_audit_not_trade_authority",
        "learning_eligible": False,
        "learning_exclusion_reason": ("candidate_outcomes_are_selection_biased_shadow_evidence"),
    }


def read_candidate_scorecard(
    path: Path,
    *,
    state_dir=None,
    cadence: str = "rebal",
    btc_symbol: str = "BTC/USDT:USDT",
) -> list[dict]:
    """Read candidate shadow outcomes and rebind each row to immutable cycle artifacts."""
    if not path.exists():
        return []
    rows: list[dict] = []
    seen: set[tuple[int, str, str]] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            key = (int(row["origin_cycle"]), str(row["symbol"]), str(row["side"]))
            if key in seen:
                raise ValueError(f"duplicate candidate score {key}")
            if int(row.get("candidate_score_schema_version", 0)) != (
                CANDIDATE_SCORE_SCHEMA_VERSION
            ):
                raise ValueError("unsupported candidate score schema")
            if row.get("learning_eligible") is not False:
                raise ValueError("candidate shadow rows cannot self-authorize learning")
            if row.get("gate_causal_claim") is not (row.get("exclusion_reason") == "entry_gate"):
                raise ValueError("candidate gate causal claim is inconsistent")
            if state_dir is not None:
                expected = _build_candidate_score_row(
                    state_dir,
                    origin_cycle=key[0],
                    symbol=key[1],
                    side=key[2],
                    btc_symbol=btc_symbol,
                    cadence=cadence,
                )
                if expected is None or expected != row:
                    raise ValueError("candidate score does not match committed artifacts")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid candidate score row {path}:{line_number}") from exc
        seen.add(key)
        rows.append(row)
    return rows


def committed_scoring_observations(
    state_dir, *, cadence: str = "rebal"
) -> list[tuple[int, datetime, dict[str, float], str]]:
    """Return mark packets from completed, provenance-verified cycles only.

    Pending evidence is intentionally excluded: publishing an immutable learning label before the
    reconcile commit would let an aborted attempt train the next desk decision.
    """
    observations: list[tuple[int, datetime, dict[str, float], str]] = []
    for observation_cycle in completed_cycle_numbers(state_dir, cadence=cadence):
        artifact_sha256 = completed_artifact_sha256(
            state_dir, observation_cycle, "scoring_marks", cadence=cadence
        )
        if artifact_sha256 is None:
            continue
        path = cycle_dir(state_dir, observation_cycle, cadence=cadence) / "scoring_marks.json"
        if not path.exists():
            continue
        try:
            raw = _read_json(path, {})
            observation_ts = _as_utc(str(raw["as_of_ts"]))
            raw_marks = raw["marks"]
            if not isinstance(raw_marks, dict) or not raw_marks:
                raise ValueError("marks must be a non-empty object")
            marks = {str(symbol): float(mark) for symbol, mark in raw_marks.items()}
            if any(
                not symbol or not math.isfinite(mark) or mark <= 0.0
                for symbol, mark in marks.items()
            ):
                raise ValueError("marks must be finite and positive")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid committed scoring marks: {path}") from exc
        observations.append((observation_cycle, observation_ts, marks, artifact_sha256))
    observations.sort(key=lambda item: (item[1], item[0]))
    return observations


def _explicit_alpha_forecast(
    state_dir,
    cycle: int,
    symbol: str,
    *,
    btc_symbol: str,
    cadence: str,
) -> dict | None:
    """Return the explicit forecast contract for one committed alpha seat, if present."""
    if not learning_origin_is_bound(state_dir, cycle, cadence=cadence):
        return None
    directory = cycle_dir(state_dir, cycle, cadence=cadence)
    book_raw = _read_json(directory / "book.json", {"legs": []})
    raw_leg = next(
        (
            row
            for row in book_raw.get("legs", [])
            if isinstance(row, dict) and str(row.get("symbol")) == symbol
        ),
        None,
    )
    required = {"seat_role", "expected_price_edge_frac", "edge_horizon_hours"}
    if (
        not isinstance(raw_leg, dict)
        or not required.issubset(raw_leg)
        or raw_leg.get("seat_role") != "alpha"
        or symbol == btc_symbol
    ):
        return None
    report = _read_json(directory / "report.json", {})
    evidence = _read_json(directory / "evidence.json", [])
    origin_raw = report.get("decision_ts") or (evidence[0].get("as_of_ts") if evidence else None)
    try:
        origin_ts = _as_utc(str(origin_raw))
        book = Book.model_validate(book_raw)
    except (TypeError, ValueError):
        return None
    leg = next((item for item in book.legs if item.symbol == symbol), None)
    if leg is None:
        return None
    signature = {
        "side": leg.side,
        "expected_price_edge_frac": float(leg.expected_price_edge_frac),
        "edge_horizon_hours": int(leg.edge_horizon_hours),
    }
    return {
        "cycle": cycle,
        "origin_ts": origin_ts,
        "maturity_ts": origin_ts + timedelta(hours=float(leg.edge_horizon_hours)),
        "signature": signature,
        "signature_sha256": sha256(
            json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _forecast_cohort_metadata(
    state_dir,
    *,
    origin_cycle: int,
    symbol: str,
    btc_symbol: str,
    cadence: str,
    policy_version: int = FORECAST_SCORE_SCHEMA_VERSION,
) -> dict:
    """Classify renewals without pretending overlapping unchanged holds are independent.

    A changed explicit forecast or a re-entered seat remains a real decision observation, but its
    market interval is still disclosed as overlapping. The decision anchor may therefore move
    while the calibration anchor remains fixed: only a statistically non-overlapping forecast may
    advance the boundary used to count the next independent observation. This prevents a shorter
    overlapping thesis from prematurely maturing a longer counted cohort.
    """
    decision_anchor: dict | None = None
    calibration_anchor: dict | None = None
    previous_forecast: dict | None = None
    previous_cycle_had_seat = False
    target_metadata: dict | None = None
    for cycle in completed_cycle_numbers(state_dir, cadence=cadence):
        if cycle > origin_cycle:
            break
        if not learning_origin_is_bound(state_dir, cycle, cadence=cadence):
            continue
        directory = cycle_dir(state_dir, cycle, cadence=cadence)
        raw_book = _read_json(directory / "book.json", {"legs": []})
        cycle_has_seat = any(
            isinstance(row, dict)
            and str(row.get("symbol")) == symbol
            and row.get("seat_role", "alpha") == "alpha"
            for row in raw_book.get("legs", [])
        )
        candidate = _explicit_alpha_forecast(
            state_dir,
            cycle,
            symbol,
            btc_symbol=btc_symbol,
            cadence=cadence,
        )
        if candidate is not None:
            reentered = previous_forecast is not None and not previous_cycle_had_seat
            thesis_changed = False
            if previous_forecast is not None:
                prior_signature = previous_forecast["signature"]
                current_signature = candidate["signature"]
                prior_edge = float(prior_signature["expected_price_edge_frac"])
                current_edge = float(current_signature["expected_price_edge_frac"])
                edge_change = abs(current_edge - prior_edge)
                edge_scale = max(abs(prior_edge), abs(current_edge), 1e-12)
                thesis_changed = (
                    current_signature["side"] != prior_signature["side"]
                    or current_signature["edge_horizon_hours"]
                    != prior_signature["edge_horizon_hours"]
                    or (
                        edge_change >= FORECAST_EDGE_CHANGE_ABS_FRAC
                        and edge_change / edge_scale >= FORECAST_EDGE_CHANGE_REL_FRAC
                    )
                )
            overlap_anchor = calibration_anchor if policy_version >= 3 else decision_anchor
            overlaps_calibration_cohort = (
                overlap_anchor is not None
                and candidate["origin_ts"]
                < overlap_anchor["maturity_ts"] - SCHEDULED_MARK_TOLERANCE
            )
            leg_nonoverlap_eligible = overlap_anchor is None or not overlaps_calibration_cohort
            if decision_anchor is None:
                decision_eligible = True
                reason = "first_forecast"
            elif reentered:
                decision_eligible = True
                reason = "seat_reentered"
            elif thesis_changed:
                decision_eligible = True
                reason = "explicit_thesis_changed"
            elif leg_nonoverlap_eligible:
                decision_eligible = True
                reason = "prior_cohort_matured"
            else:
                decision_eligible = False
                reason = "overlapping_unchanged_thesis"
            if decision_eligible:
                decision_anchor = candidate
            if policy_version >= 3 and leg_nonoverlap_eligible:
                calibration_anchor = candidate
            elif policy_version == 2:
                # Schema v2 used the most recent decision-eligible forecast as both anchors.
                # Preserve that reconstruction exactly so immutable historical rows still bind.
                calibration_anchor = decision_anchor
            if decision_anchor is None or calibration_anchor is None:
                raise ValueError("forecast cohort anchors were not initialized")
            target_metadata = {
                "forecast_thesis_sha256": candidate["signature_sha256"],
                "forecast_thesis_change_policy": (
                    "side_or_horizon_change_or_edge_change_at_least_25bp_and_25pct"
                ),
                "prior_forecast_origin_cycle": (
                    int(previous_forecast["cycle"]) if previous_forecast is not None else None
                ),
                "forecast_thesis_changed": thesis_changed,
                "overlaps_prior_cohort": overlaps_calibration_cohort,
                "decision_learning_eligible": decision_eligible,
                "statistically_independent": (
                    leg_nonoverlap_eligible if policy_version <= 3 else None
                ),
                "forecast_independence_reason": reason,
                "forecast_cohort_origin_cycle": int(calibration_anchor["cycle"]),
            }
            if policy_version >= 3:
                target_metadata.update(
                    {
                        "overlaps_calibration_cohort": overlaps_calibration_cohort,
                        "forecast_decision_cohort_origin_cycle": int(decision_anchor["cycle"]),
                        "forecast_calibration_cohort_origin_cycle": int(
                            calibration_anchor["cycle"]
                        ),
                        "forecast_calibration_boundary_ts": calibration_anchor[
                            "maturity_ts"
                        ].isoformat(),
                        "forecast_calibration_boundary_policy": (
                            "advanced_only_by_statistically_nonoverlapping_forecasts"
                            if policy_version == 3
                            else "advanced_only_by_same_symbol_nonoverlapping_forecasts"
                        ),
                    }
                )
            if policy_version >= 4:
                target_metadata.update(
                    {
                        "leg_nonoverlap_eligible": leg_nonoverlap_eligible,
                        "forecast_leg_nonoverlap_reason": reason,
                        "statistical_independence_unit": (
                            "nonoverlapping_time_outcome_cohort_assigned_in_performance"
                        ),
                    }
                )
            previous_forecast = candidate
            if cycle == origin_cycle:
                break
        previous_cycle_had_seat = cycle_has_seat
    if target_metadata is None:
        raise ValueError(f"explicit forecast missing for cycle {origin_cycle} {symbol}")
    return target_metadata


def _build_forecast_score_row(
    state_dir,
    *,
    origin_cycle: int,
    symbol: str,
    btc_symbol: str,
    cadence: str,
    policy_version: int = FORECAST_SCORE_SCHEMA_VERSION,
) -> dict | None:
    """Build one forecast label from its bound origin and first eligible mature mark packet."""
    if not learning_origin_is_bound(state_dir, origin_cycle, cadence=cadence):
        return None
    origin_dir = cycle_dir(state_dir, origin_cycle, cadence=cadence)
    evidence = _read_json(origin_dir / "evidence.json", [])
    book_raw = _read_json(origin_dir / "book.json", {"legs": []})
    raw_leg = next(
        (
            row
            for row in book_raw.get("legs", [])
            if isinstance(row, dict) and str(row.get("symbol")) == symbol
        ),
        None,
    )
    if not isinstance(raw_leg, dict) or any(
        field not in raw_leg
        for field in ("seat_role", "expected_price_edge_frac", "edge_horizon_hours")
    ):
        return None
    book = Book.model_validate(book_raw)
    leg = next((item for item in book.legs if item.symbol == symbol), None)
    if leg is None or leg.symbol == btc_symbol or leg.seat_role != "alpha" or not evidence:
        return None
    ev_by_symbol = {str(row["symbol"]): row for row in evidence}
    origin_row = ev_by_symbol.get(symbol)
    origin_mark = float((origin_row or {}).get("mark") or 0.0)
    origin_btc = float(ev_by_symbol.get(btc_symbol, {}).get("mark") or 0.0)
    report = _read_json(origin_dir / "report.json", {})
    origin_raw = report.get("decision_ts") or evidence[0].get("as_of_ts")
    try:
        origin_ts = _as_utc(str(origin_raw))
    except (TypeError, ValueError):
        return None
    if origin_mark <= 0.0 or origin_btc <= 0.0:
        return None
    maturity = origin_ts + timedelta(hours=float(leg.edge_horizon_hours))
    earliest_eligible_ts = maturity - SCHEDULED_MARK_TOLERANCE if policy_version >= 2 else maturity
    eligible = next(
        (
            observation
            for observation in committed_scoring_observations(state_dir, cadence=cadence)
            if observation[1] >= earliest_eligible_ts
            and float(observation[2].get(symbol, 0.0) or 0.0) > 0.0
            and float(observation[2].get(btc_symbol, 0.0) or 0.0) > 0.0
        ),
        None,
    )
    if eligible is None:
        return None
    outcome_cycle, outcome_ts, outcome_marks, outcome_artifact_sha256 = eligible
    current_mark = float(outcome_marks[symbol])
    current_btc = float(outcome_marks[btc_symbol])
    btc_return = current_btc / origin_btc - 1.0
    elapsed_hours = (outcome_ts - origin_ts).total_seconds() / 3600.0
    beta = float((origin_row or {}).get("beta_clamped", (origin_row or {}).get("beta_btc", 1.0)))
    relative_return = current_mark / origin_mark - 1.0 - beta * btc_return
    side_sign = 1.0 if leg.side == "long" else -1.0
    selected_edge = side_sign * relative_return
    predicted = float(leg.expected_price_edge_frac)
    risk_model = (
        _read_json(origin_dir / "risk_model.json", {})
        if completed_artifact_is_bound(state_dir, origin_cycle, "risk_model", cadence=cadence)
        else {}
    )
    residual_vols = risk_model.get("residual_vol_annualized") or {}
    legacy_row = {
        "origin_cycle": origin_cycle,
        "symbol": leg.symbol,
        "side": leg.side,
        "seat_role": leg.seat_role,
        "origin_ts": origin_ts.isoformat(),
        "evaluated_at": outcome_ts.isoformat(),
        "outcome_observation_cycle": outcome_cycle,
        "outcome_scoring_marks_sha256": outcome_artifact_sha256,
        "forecast_horizon_hours": int(leg.edge_horizon_hours),
        "evaluation_horizon_hours": elapsed_hours,
        "predicted_selected_edge_frac": predicted,
        "target_notional": float(leg.target_notional),
        "origin_standalone_vol_usd": (
            float(leg.target_notional) * float(residual_vols[leg.symbol])
            if residual_vols.get(leg.symbol) is not None
            else None
        ),
        "origin_mark": origin_mark,
        "evaluation_mark": current_mark,
        "beta_clamped": beta,
        "btc_origin_mark": origin_btc,
        "btc_evaluation_mark": current_btc,
        "realized_beta_adjusted_return_frac": relative_return,
        "realized_selected_edge_frac": selected_edge,
        "sign_hit": selected_edge > 0.0,
        "forecast_error_frac": selected_edge - predicted,
        "outcome_marks_sha256": _marks_sha256(outcome_marks),
    }
    if policy_version == 1:
        return legacy_row

    cohort = _forecast_cohort_metadata(
        state_dir,
        origin_cycle=origin_cycle,
        symbol=symbol,
        btc_symbol=btc_symbol,
        cadence=cadence,
        policy_version=policy_version,
    )
    on_horizon = horizon_is_on_schedule(elapsed_hours, target_hours=float(leg.edge_horizon_hours))
    selected_profitable = selected_edge > 0.0
    predicted_direction = 1 if predicted > 0.0 else -1 if predicted < 0.0 else 0
    realized_direction = 1 if selected_edge > 0.0 else -1 if selected_edge < 0.0 else 0
    directional_hit = (
        None if predicted_direction == 0 else predicted_direction == realized_direction
    )
    exclusion_reasons = []
    if not on_horizon:
        exclusion_reasons.append("off_declared_horizon")
    if not cohort["decision_learning_eligible"]:
        exclusion_reasons.append("overlapping_unchanged_thesis")
    leg_nonoverlap_eligible = (
        cohort["leg_nonoverlap_eligible"]
        if policy_version >= 4
        else cohort["statistically_independent"]
    )
    if policy_version >= 3 and not leg_nonoverlap_eligible:
        exclusion_reasons.append("nonindependent_overlapping_calibration_cohort")
    learning_eligible = on_horizon and cohort["decision_learning_eligible"]
    if policy_version >= 3:
        learning_eligible = learning_eligible and leg_nonoverlap_eligible
    return {
        **legacy_row,
        "forecast_score_schema_version": policy_version,
        "label_policy": (
            "scheduled_horizon_with_nonoverlapping_renewal_cohorts"
            if policy_version <= 3
            else "scheduled_horizon_with_leg_nonoverlap_and_time_cohort_calibration"
        ),
        "scheduled_maturity_ts": maturity.isoformat(),
        "scheduled_mark_tolerance_minutes": (SCHEDULED_MARK_TOLERANCE.total_seconds() / 60.0),
        "horizon_drift_hours": elapsed_hours - float(leg.edge_horizon_hours),
        "horizon_label_eligible": on_horizon,
        "horizon_status": "on_schedule" if on_horizon else "off_horizon_late",
        "selected_side_profitable": selected_profitable,
        "directional_forecast_hit": directional_hit,
        # Retained as a compatibility alias, now with its literal directional meaning.
        "sign_hit": directional_hit,
        **cohort,
        "learning_eligible": learning_eligible,
        "learning_exclusion_reasons": exclusion_reasons,
    }


def learning_origin_is_bound(state_dir, cycle: int, *, cadence: str = "rebal") -> bool:
    """Require every input used to construct a normal learning score to be commit-bound."""
    return all(
        completed_artifact_is_bound(state_dir, cycle, name, cadence=cadence)
        for name in ("evidence", "reads", "book", "adversary", "report")
    )


def score_record_is_manifest_bound(
    state_dir, record: ScoreRecord, *, cadence: str = "rebal"
) -> bool:
    """Rebind a normal score row to its exact origin and committed outcome packet."""
    if record.outcome_provenance != "manifest_bound":
        return False
    observation_cycle = record.outcome_observation_cycle
    if observation_cycle is None or not learning_origin_is_bound(
        state_dir, record.cycle, cadence=cadence
    ):
        return False
    artifact_sha256 = completed_artifact_sha256(
        state_dir, observation_cycle, "scoring_marks", cadence=cadence
    )
    if artifact_sha256 != record.outcome_scoring_marks_sha256:
        return False
    packet_path = cycle_dir(state_dir, observation_cycle, cadence=cadence) / "scoring_marks.json"
    try:
        packet = _read_json(packet_path, {})
        raw_marks = packet["marks"]
        if not isinstance(raw_marks, dict) or not raw_marks:
            return False
        marks = {str(symbol): float(mark) for symbol, mark in raw_marks.items()}
        if any(
            not symbol or not math.isfinite(mark) or mark <= 0.0 for symbol, mark in marks.items()
        ):
            return False
        if not (
            _marks_sha256(marks) == record.outcome_marks_sha256
            and _as_utc(str(packet["as_of_ts"])) == _as_utc(record.scored_at)
        ):
            return False
        expected = _build_score_record(
            state_dir,
            scored_cycle=record.cycle,
            cur_marks=marks,
            now=record.scored_at,
            btc_symbol=record.btc_symbol,
            cadence=cadence,
            outcome_observation_cycle=observation_cycle,
            outcome_scoring_marks_sha256=artifact_sha256,
            outcome_provenance="manifest_bound",
        )
        expected_payload = expected.model_dump(mode="json")
        actual_payload = record.model_dump(mode="json")
        # ScoreRecord predates explicit alpha-gross, funding, exit, and candidate fields. Compare
        # immutable legacy rows only over the fields actually serialized at creation; new rows
        # remain fully strict because their model_fields_set contains the complete current schema.
        for field in record.model_fields_set:
            if field == "book":
                for book_field in record.book.model_fields_set:
                    if expected_payload["book"].get(book_field) != actual_payload["book"].get(
                        book_field
                    ):
                        return False
            elif expected_payload.get(field) != actual_payload.get(field):
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _score_mature_leg_forecasts(
    state_dir,
    memory_dir,
    *,
    through_cycle: int,
    btc_symbol: str,
    cadence: str,
) -> dict:
    """Immutably score each alpha leg at its own declared forecast horizon.

    A forecast waits until the first committed desk mark at the scheduled horizon (allowing the
    explicit five-minute scheduler tolerance). If only a materially late mark exists, it is kept
    as audit evidence and marked ineligible for learning. If a closed
    symbol is absent from the current quality universe, it remains pending rather than receiving a
    fabricated mark. Renewing a forecast creates a new origin-cycle key and never erases the old
    outcome, but an overlapping unchanged renewal is not an independent learning observation.
    """
    path = Path(memory_dir) / "forecast-scorecard.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = read_forecast_scorecard(
        path, state_dir=state_dir, cadence=cadence, btc_symbol=btc_symbol
    )
    seen = {(int(row["origin_cycle"]), str(row["symbol"])) for row in rows}

    pending_count = 0
    new_rows: list[dict] = []
    for origin_cycle in range(1, max(0, through_cycle) + 1):
        origin_dir = cycle_dir(state_dir, origin_cycle, cadence=cadence)
        if not learning_origin_is_bound(state_dir, origin_cycle, cadence=cadence):
            continue
        book_raw = _read_json(origin_dir / "book.json", {"legs": []})
        book = Book.model_validate(book_raw)
        raw_legs = {
            str(row.get("symbol")): row
            for row in book_raw.get("legs", [])
            if isinstance(row, dict) and row.get("symbol")
        }
        for leg in book.legs:
            key = (origin_cycle, leg.symbol)
            if leg.symbol == btc_symbol or leg.seat_role != "alpha" or key in seen:
                continue
            raw_leg = raw_legs.get(leg.symbol, {})
            if (
                "seat_role" not in raw_leg
                or "expected_price_edge_frac" not in raw_leg
                or "edge_horizon_hours" not in raw_leg
            ):
                # Historical legs predate the forecast contract. Pydantic defaults make them
                # readable as Books, but those defaults are not forecasts and must not be scored.
                continue
            row = _build_forecast_score_row(
                state_dir,
                origin_cycle=origin_cycle,
                symbol=leg.symbol,
                btc_symbol=btc_symbol,
                cadence=cadence,
            )
            if row is None:
                pending_count += 1
                continue
            new_rows.append(row)
            seen.add(key)
    if new_rows:
        rows.extend(new_rows)
        rows.sort(key=lambda row: (int(row["origin_cycle"]), str(row["symbol"])))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        os.replace(tmp, path)
    candidate_status = _score_mature_candidate_reviews(
        state_dir,
        memory_dir,
        through_cycle=through_cycle,
        btc_symbol=btc_symbol,
        cadence=cadence,
    )
    return {
        "new_forecast_scores": len(new_rows),
        "new_forecast_learning_labels": sum(
            row.get("learning_eligible") is True for row in new_rows
        ),
        "new_overlapping_renewals_excluded": sum(
            row.get("forecast_independence_reason") == "overlapping_unchanged_thesis"
            for row in new_rows
        ),
        "new_leg_overlap_forecasts_audit_only": sum(
            int(row.get("forecast_score_schema_version", 1)) >= FORECAST_SCORE_SCHEMA_VERSION
            and row.get("leg_nonoverlap_eligible") is not True
            for row in new_rows
        ),
        "new_off_horizon_forecasts_excluded": sum(
            row.get("horizon_label_eligible") is False for row in new_rows
        ),
        "pending_forecasts": pending_count,
        **candidate_status,
    }


def _score_mature_candidate_reviews(
    state_dir,
    memory_dir,
    *,
    through_cycle: int,
    btc_symbol: str,
    cadence: str,
) -> dict:
    """Immutably publish scheduled-horizon outcomes for PM-declared candidates."""
    path = Path(memory_dir) / "candidate-scorecard.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = read_candidate_scorecard(
        path, state_dir=state_dir, cadence=cadence, btc_symbol=btc_symbol
    )
    seen = {(int(row["origin_cycle"]), str(row["symbol"]), str(row["side"])) for row in rows}
    new_rows: list[dict] = []
    pending_count = 0
    for origin_cycle in range(1, max(0, through_cycle) + 1):
        if not learning_origin_is_bound(state_dir, origin_cycle, cadence=cadence):
            continue
        origin_dir = cycle_dir(state_dir, origin_cycle, cadence=cadence)
        book = Book.model_validate(_read_json(origin_dir / "book.json", {"legs": []}))
        for candidate in book.candidate_reviews:
            key = (origin_cycle, candidate.symbol, candidate.side)
            if key in seen:
                continue
            row = _build_candidate_score_row(
                state_dir,
                origin_cycle=origin_cycle,
                symbol=candidate.symbol,
                side=candidate.side,
                btc_symbol=btc_symbol,
                cadence=cadence,
            )
            if row is None:
                pending_count += 1
                continue
            new_rows.append(row)
            seen.add(key)
    if new_rows:
        rows.extend(new_rows)
        rows.sort(key=lambda row: (int(row["origin_cycle"]), str(row["symbol"]), str(row["side"])))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        os.replace(tmp, path)
    return {
        "new_candidate_shadow_scores": len(new_rows),
        "new_gate_declared_shadow_scores": sum(
            row.get("gate_causal_claim") is True for row in new_rows
        ),
        "new_candidate_off_horizon_scores": sum(
            row.get("horizon_label_eligible") is False for row in new_rows
        ),
        "pending_candidate_shadow_scores": pending_count,
    }


def score_mature_leg_forecasts(
    state_dir,
    memory_dir,
    *,
    through_cycle: int,
    btc_symbol: str,
    cadence: str = "rebal",
) -> dict:
    """Publish mature PM forecast outcomes using committed scoring packets only."""
    return _score_mature_leg_forecasts(
        state_dir,
        memory_dir,
        through_cycle=through_cycle,
        btc_symbol=btc_symbol,
        cadence=cadence,
    )


def _resolve_pending_dir(memory_dir) -> Path:
    """The CURRENT cycle's pending dir via pending_io when the pointer exists; the flat root
    otherwise (legacy layout / offline tests). Import is local to keep this module IO-pure."""
    from futures_fund.pending_io import resolve_pending

    try:
        pending, _meta = resolve_pending(memory_dir)
        return pending
    except (FileNotFoundError, ValueError, KeyError):
        return Path(memory_dir) / "pending"


def _decision_prior_inventory_book(
    state_dir,
    origin_cycle: int,
    *,
    btc_symbol: str,
    cadence: str,
) -> Book | None:
    """Reconstruct the exact pre-decision quantities when the execution audit permits it.

    This is used only for descriptive no-change opportunity cost. It never recreates missing
    inventory from stale target notionals: absent or unbound execution evidence returns ``None``.
    """
    if not completed_artifact_is_bound(state_dir, origin_cycle, "execution", cadence=cadence):
        return None
    prior_cycles = [
        cycle
        for cycle in completed_cycle_numbers(state_dir, cadence=cadence)
        if cycle < origin_cycle
        and completed_artifact_is_bound(state_dir, cycle, "book", cadence=cadence)
    ]
    if not prior_cycles:
        return None
    prior_book = Book.model_validate(
        _read_json(
            cycle_dir(state_dir, prior_cycles[-1], cadence=cadence) / "book.json",
            {"legs": []},
        )
    )
    roles = {leg.symbol: leg.seat_role for leg in prior_book.legs}
    execution = _read_json(
        cycle_dir(state_dir, origin_cycle, cadence=cadence) / "execution.json", {}
    )
    if not isinstance(execution, dict):
        return None
    legs: list[BookLeg] = []
    for symbol, row in execution.items():
        if not isinstance(row, dict):
            return None
        quantity = float(row.get("current_qty_signed") or 0.0)
        if abs(quantity) <= 1e-15:
            continue
        mark = float(row.get("decision_mark") or 0.0)
        if mark <= 0.0 or symbol not in roles:
            return None
        legs.append(
            BookLeg(
                symbol=str(symbol),
                side="long" if quantity > 0.0 else "short",
                target_notional=abs(quantity) * mark,
                seat_role=roles[symbol],
            )
        )
    # A cycle's execution artifact covers touched symbols only. Reconstruction is exact only when
    # every prior seat appears; this is naturally true for a complete exit-to-cash decision.
    if {leg.symbol for leg in legs} != {leg.symbol for leg in prior_book.legs}:
        return None
    if any(leg.seat_role == "hedge" and leg.symbol != btc_symbol for leg in legs):
        return None
    return Book(legs=legs)


def _actual_funding_for_score_window(
    state_dir,
    *,
    origin_cycle: int,
    outcome_observation_cycle: int | None,
    origin_ts: datetime,
    outcome_ts: datetime,
    cadence: str,
) -> tuple[float | None, str]:
    """Attribute exact funding only across one unchanged-book decision interval."""
    if outcome_observation_cycle is None:
        return None, "unavailable_unbound_outcome"
    later = [
        cycle
        for cycle in completed_cycle_numbers(state_dir, cadence=cadence)
        if cycle > origin_cycle
    ]
    if not later or later[0] != outcome_observation_cycle:
        return None, "unavailable_intervening_rebalance_or_missing_outcome"
    if not completed_artifact_is_bound(
        state_dir, outcome_observation_cycle, "report", cadence=cadence
    ):
        return None, "unavailable_outcome_report_not_manifest_bound"
    try:
        report = _read_json(
            cycle_dir(state_dir, outcome_observation_cycle, cadence=cadence) / "report.json",
            {},
        )
        cycle_funding = float(report["funding_settled_cycle"])
        if not math.isfinite(cycle_funding):
            raise ValueError("non-finite cycle funding")
        heartbeat_funding = 0.0
        heartbeat_path = Path(state_dir) / "portfolio-heartbeats.jsonl"
        if heartbeat_path.exists():
            for line in heartbeat_path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                ts = _as_utc(str(row["ts"]))
                if origin_ts < ts <= outcome_ts:
                    if not verify_heartbeat_completion(state_dir, row):
                        return None, "unavailable_unbound_heartbeat_funding_audit"
                    value = float(row["funding_settled"])
                    if not math.isfinite(value):
                        raise ValueError("non-finite heartbeat funding")
                    heartbeat_funding += value
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, "unavailable_invalid_funding_audit"
    return (
        cycle_funding + heartbeat_funding,
        "exact_next_rebalance_report_plus_intervening_heartbeats",
    )


def _legacy_pm_gate_declaration(book: Book) -> dict | None:
    """Bind explicit pre-candidate-schema PM prose without inventing candidate rows.

    Only artifacts that truly omitted ``candidate_reviews`` qualify. New production output cannot
    bypass the structured contract by emitting an empty list plus prose. The small marker set is a
    versioned adapter for the desk's already-committed c51-c53 wording; it recognizes only an
    explicit PM claim that the active managed entry policy caused the no-entry result.
    """
    if "candidate_reviews" in book.model_fields_set:
        return None
    fields = {
        "turnover_justification": book.turnover_justification,
        "notes": book.notes,
    }
    markers = (
        "the managed entry calibration admits no new long",
        "no fresh non-btc long or short satisfies the active managed entry calibration",
        "no discretionary non-btc entry satisfies the active managed entry gate",
    )
    matches = [
        {"field": field, "marker": marker}
        for field, value in fields.items()
        for marker in markers
        if marker in value.lower()
    ]
    if not matches:
        return None
    declaration_sha256 = sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "kind": "legacy_manifest_bound_explicit_pm_gate_declaration",
        "adapter_version": 1,
        "declaration_sha256": declaration_sha256,
        "matched_markers": matches,
    }


def _pm_gate_inactive_recurrences(
    state_dir,
    *,
    through_cycle: int,
    cadence: str,
    k: int = 3,
) -> list[Recurrence]:
    """Surface a PM-declared entry gate that repeatedly excludes every alpha seat.

    Causality comes only from each PM's manifest-bound ``candidate_reviews`` declaration. Code
    never infers that a rejected candidate should have traded and never turns this recurrence into
    a trade, veto, or deterministic eligibility rule.
    """
    qualifying: list[dict] = []
    completed = [
        cycle
        for cycle in completed_cycle_numbers(state_dir, cadence=cadence)
        if cycle <= through_cycle
    ]
    for cycle in completed[-max(k, 6) :]:
        required = ("evidence", "reads", "book", "entry_gate_policy")
        hashes = {
            name: completed_artifact_sha256(state_dir, cycle, name, cadence=cadence)
            for name in required
        }
        if any(value is None for value in hashes.values()):
            continue
        directory = cycle_dir(state_dir, cycle, cadence=cadence)
        try:
            evidence = _read_json(directory / "evidence.json", [])
            raw_reads = _read_json(directory / "reads.json", {})
            book = Book.model_validate(_read_json(directory / "book.json", {"legs": []}))
            policy = _read_json(directory / "entry_gate_policy.json", {})
            symbols = [str(row["symbol"]) for row in evidence]
            if not symbols or len(symbols) != len(set(symbols)):
                continue
            expected = set(symbols)
            complete_roles = True
            nonflat_technical = 0
            parsed_reads: dict[str, list[SpecialistRead]] = {}
            for role in SPECIALIST_ROLES:
                role_rows = raw_reads.get(role)
                if not isinstance(role_rows, list):
                    complete_roles = False
                    break
                parsed = [SpecialistRead.model_validate(row) for row in role_rows]
                parsed_reads[role] = parsed
                role_symbols = [row.symbol for row in parsed]
                if len(role_symbols) != len(set(role_symbols)) or set(role_symbols) != expected:
                    complete_roles = False
                    break
                if role == "technical":
                    nonflat_technical = sum(row.lean != "flat" for row in parsed)
            alpha_legs = [leg for leg in book.legs if leg.seat_role == "alpha"]
            managed_region = str(policy.get("managed_region") or "")
            managed_sha256 = sha256(
                json.dumps(
                    managed_region,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()
            if (
                not complete_roles
                or alpha_legs
                or not managed_region.strip()
                or policy.get("sha256") != managed_sha256
            ):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if book.candidate_reviews:
            try:
                book.validate_candidate_review_coverage(parsed_reads)
            except ValueError:
                continue
            gate_exclusions = [
                item for item in book.candidate_reviews if item.exclusion_reason == "entry_gate"
            ]
            if not gate_exclusions:
                continue
            causal_evidence = {
                "kind": "structured_pm_candidate_reviews",
                "gate_candidate_bindings": [
                    {
                        "symbol": item.symbol,
                        "side": item.side,
                        "candidate_sha256": sha256(
                            json.dumps(
                                item.model_dump(mode="json"),
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest(),
                    }
                    for item in gate_exclusions
                ],
            }
        else:
            causal_evidence = _legacy_pm_gate_declaration(book)
            if causal_evidence is None:
                continue
        qualifying.append(
            {
                "cycle": cycle,
                "hashes": hashes,
                "nonflat_technical": nonflat_technical,
                "candidate_reviews": len(book.candidate_reviews),
                "causal_evidence": causal_evidence,
            }
        )
    trailing = qualifying[-k:]
    if (
        len(trailing) < k
        or [row["cycle"] for row in trailing] != completed[-k:]
        or len({row["hashes"]["entry_gate_policy"] for row in trailing}) != 1
    ):
        return []
    evidence_rows = []
    for row in trailing:
        hashes = row["hashes"]
        evidence_rows.append(
            f"c{row['cycle']}: alpha_legs=0, complete_specialists=3, "
            f"nonflat_technical={row['nonflat_technical']}, "
            f"candidate_reviews={row['candidate_reviews']}, "
            f"pm_gate_causal_evidence={row['causal_evidence']}, "
            f"book_sha256={hashes['book']}, reads_sha256={hashes['reads']}, "
            f"entry_gate_policy_sha256={hashes['entry_gate_policy']}"
        )
    return [
        Recurrence(
            kind="pm_gate_inactive",
            role="pm",
            count=k,
            window=k,
            evidence=evidence_rows,
            suggestion=(
                f"the last {k} completed manifest-bound cycles had complete specialist coverage, "
                "zero alpha seats, and an explicit PM gate-causal declaration under the same "
                "active managed policy. Narrow or retire only that supported performance "
                "calibration. Never select a candidate, force deployment, or weaken price, risk, "
                "liquidity, neutrality, evidence-integrity, or PAPER-only controls."
            ),
        )
    ]


def _build_score_record(
    state_dir,
    *,
    scored_cycle: int,
    cur_marks: dict[str, float],
    now: str,
    btc_symbol: str,
    cadence: str,
    outcome_observation_cycle: int | None,
    outcome_scoring_marks_sha256: str,
    outcome_provenance: str,
    funding_horizon_events: float | None = None,
) -> ScoreRecord:
    """Deterministically derive a normal learning row from bound decision and mark inputs."""
    directory = cycle_dir(state_dir, scored_cycle, cadence=cadence)
    prev_ev = _read_json(directory / "evidence.json", [])
    marks_prev = {e["symbol"]: float(e["mark"]) for e in prev_ev}
    betas = {e["symbol"]: float(e.get("beta_clamped", e.get("beta_btc", 1.0))) for e in prev_ev}
    rets = forward_returns(marks_prev, cur_marks)
    btc_ret = rets.get(btc_symbol, 0.0)
    specialist_rets = {
        symbol: ret - betas.get(symbol, 1.0) * btc_ret
        for symbol, ret in rets.items()
        if symbol != btc_symbol
    }

    reads_raw = _read_json(directory / "reads.json", {})
    specialists = {}
    for role in SPECIALIST_ROLES:
        reads = [SpecialistRead.model_validate(x) for x in reads_raw.get(role, [])]
        specialists[role] = score_specialist(role, reads, specialist_rets)

    book = Book.model_validate(_read_json(directory / "book.json", {"legs": []}))
    parsed_reads = {
        role: [SpecialistRead.model_validate(x) for x in reads_raw.get(role, [])]
        for role in SPECIALIST_ROLES
    }
    if book.candidate_reviews:
        book.validate_candidate_review_coverage(parsed_reads)
    ev_by_symbol = {e["symbol"]: e for e in prev_ev}
    report = _read_json(directory / "report.json", {})
    evaluation_horizon_hours = 0.0
    try:
        start_raw = report.get("decision_ts") or prev_ev[0].get("as_of_ts")
        evaluation_horizon_hours = max(
            0.0,
            (_as_utc(now) - _as_utc(str(start_raw))).total_seconds() / 3600.0,
        )
    except (IndexError, TypeError, ValueError):
        if funding_horizon_events is not None:
            evaluation_horizon_hours = max(0.0, funding_horizon_events * 8.0)
    horizon_events = (
        funding_horizon_events
        if funding_horizon_events is not None
        else (evaluation_horizon_hours / 8.0 if evaluation_horizon_hours > 0.0 else 1.0)
    )

    projected_funding = 0.0
    for leg in book.legs:
        funding_bps = float(ev_by_symbol.get(leg.symbol, {}).get("expected_funding_8h_bps") or 0.0)
        seat_sign = 1.0 if leg.side == "short" else -1.0
        projected_funding += seat_sign * funding_bps / 1e4 * leg.target_notional * horizon_events
    entry_friction = float(report.get("fees_paid_cycle", 0.0)) + float(
        report.get("slippage_paid_cycle", 0.0)
    )
    previous_book = _decision_prior_inventory_book(
        state_dir, scored_cycle, btc_symbol=btc_symbol, cadence=cadence
    )
    try:
        funding_origin_ts = _as_utc(str(report.get("decision_ts") or prev_ev[0].get("as_of_ts")))
        funding_outcome_ts = _as_utc(now)
    except (IndexError, TypeError, ValueError):
        actual_funding, actual_funding_status = None, "unavailable_invalid_score_timestamps"
    else:
        actual_funding, actual_funding_status = _actual_funding_for_score_window(
            state_dir,
            origin_cycle=scored_cycle,
            outcome_observation_cycle=outcome_observation_cycle,
            origin_ts=funding_origin_ts,
            outcome_ts=funding_outcome_ts,
            cadence=cadence,
        )
    book_score = score_book(
        book,
        rets,
        betas,
        btc_ret,
        projected_funding_pnl=projected_funding,
        entry_friction=entry_friction,
        actual_realized_funding_pnl=actual_funding,
        actual_funding_attribution_status=actual_funding_status,
        previous_book=previous_book,
    )

    book_sha256 = completed_artifact_sha256(state_dir, scored_cycle, "book", cadence=cadence) or ""
    reads_sha256 = (
        completed_artifact_sha256(state_dir, scored_cycle, "reads", cadence=cadence) or ""
    )
    entry_gate_policy_sha256 = (
        completed_artifact_sha256(state_dir, scored_cycle, "entry_gate_policy", cadence=cadence)
        or ""
    )
    candidate_opportunities = score_candidate_opportunities(
        book.candidate_reviews,
        rets,
        betas,
        btc_ret,
        evaluation_horizon_hours=evaluation_horizon_hours,
        book_sha256=book_sha256,
        specialist_reads_sha256=reads_sha256,
        entry_gate_policy_sha256=entry_gate_policy_sha256,
    )

    adv_raw = _read_json(directory / "adversary.json", {"accept": True})
    adv_accept = bool(adv_raw.get("accept", True))
    adv_objections = list(adv_raw.get("objections", []))
    return ScoreRecord(
        cycle=scored_cycle,
        btc_symbol=btc_symbol,
        scored_at=now,
        evaluation_horizon_hours=evaluation_horizon_hours,
        outcome_marks_sha256=_marks_sha256(cur_marks),
        outcome_observation_cycle=outcome_observation_cycle,
        outcome_scoring_marks_sha256=outcome_scoring_marks_sha256,
        outcome_provenance=outcome_provenance,
        n_symbols=len(specialist_rets),
        specialist_return_label="btc_beta_adjusted",
        specialists=specialists,
        book=book_score,
        decision_book_sha256=book_sha256,
        decision_reads_sha256=reads_sha256,
        entry_gate_policy_sha256=entry_gate_policy_sha256,
        candidate_opportunities=candidate_opportunities,
        adv_accepted=adv_accept,
        adv_revised=not adv_accept,
        adv_reason_tags=(classify_objections(adv_objections) if not adv_accept else []),
    )


def score_previous_cycle(
    state_dir,
    memory_dir,
    *,
    scored_cycle: int,
    cur_marks: dict[str, float],
    now: str,
    btc_symbol: str,
    cadence: str = "rebal",
    k: int = 3,
    window: int = 6,
    funding_horizon_events: float | None = None,
    active_calibration_roles: set[str] | None = None,
    outcome_observation_cycle: int | None = None,
    outcome_scoring_marks_sha256: str = "",
) -> dict:
    pending = _resolve_pending_dir(memory_dir)
    pending.mkdir(parents=True, exist_ok=True)
    rec_path = pending / "recurrences.json"
    # consume-once: a stale proposal from a prior cycle must never be applied by reflector_apply
    (pending / "reflection.json").unlink(missing_ok=True)
    forecast_status = _score_mature_leg_forecasts(
        state_dir,
        memory_dir,
        through_cycle=scored_cycle,
        btc_symbol=btc_symbol,
        cadence=cadence,
    )
    d = cycle_dir(state_dir, scored_cycle, cadence=cadence)
    if scored_cycle < 1 or not (d / "evidence.json").exists():
        rec_path.write_text("[]")
        return {"scored_cycle": None, "recurrences": [], **forecast_status}

    outcome_provenance = "legacy_unverified"
    if outcome_observation_cycle is not None or outcome_scoring_marks_sha256:
        eligible = next(
            (
                (observation_ts, marks)
                for observation_cycle, observation_ts, marks, artifact_sha256 in (
                    committed_scoring_observations(state_dir, cadence=cadence)
                )
                if observation_cycle == outcome_observation_cycle
                and artifact_sha256 == outcome_scoring_marks_sha256
            ),
            None,
        )
        if eligible is None:
            raise ValueError("score outcome is not a manifest-bound scoring observation")
        observation_ts, observation_marks = eligible
        if observation_ts != _as_utc(now) or _marks_sha256(observation_marks) != _marks_sha256(
            cur_marks
        ):
            raise ValueError("score outcome does not match the committed scoring observation")
        if not learning_origin_is_bound(state_dir, scored_cycle, cadence=cadence):
            raise ValueError("score origin decision artifacts are not manifest-bound")
        outcome_provenance = "manifest_bound"

    candidate = _build_score_record(
        state_dir,
        scored_cycle=scored_cycle,
        cur_marks=cur_marks,
        now=now,
        btc_symbol=btc_symbol,
        cadence=cadence,
        outcome_observation_cycle=outcome_observation_cycle,
        outcome_scoring_marks_sha256=outcome_scoring_marks_sha256,
        outcome_provenance=outcome_provenance,
        funding_horizon_events=funding_horizon_events,
    )
    sc_path = Path(memory_dir) / "scorecard.jsonl"
    existing = _read_scorecard(sc_path, state_dir=state_dir, cadence=cadence)
    # The first valid forward observation is an immutable label. A later retry may happen at a
    # different mark/horizon; replacing the old label would silently rewrite evidence after the
    # Reflector had already acted on it.
    by_cycle = {record.cycle: record for record in existing}
    attribution_path = d / "attribution.json"
    prior_record = by_cycle.get(scored_cycle)
    if (
        prior_record is not None
        and prior_record.outcome_provenance == "legacy_unverified"
        and candidate.outcome_provenance == "manifest_bound"
    ):
        _archive_unverified_score(memory_dir, prior_record, replaced_at=now)
    immutable_reused = prior_record is not None and (
        prior_record.outcome_provenance == "manifest_bound"
        or candidate.outcome_provenance == "legacy_unverified"
    )
    if immutable_reused:
        record = prior_record
        if attribution_path.exists():
            attribution = ScoreRecord.model_validate_json(attribution_path.read_text())
            if attribution.model_dump(mode="json") != record.model_dump(mode="json"):
                raise ValueError(
                    f"cycle {scored_cycle} attribution conflicts with immutable scorecard"
                )
        else:
            save_output(
                state_dir,
                scored_cycle,
                "attribution",
                record.model_dump(mode="json"),
                cadence=cadence,
            )
    elif attribution_path.exists():
        # Crash boundary: attribution is written before scorecard. Treat that valid first record
        # as the immutable intent rather than relabeling the decision at this retry's later marks.
        attribution = ScoreRecord.model_validate_json(attribution_path.read_text())
        if attribution.cycle != scored_cycle:
            raise ValueError("attribution cycle does not match the score request")
        if attribution.outcome_provenance == "manifest_bound":
            if attribution.model_dump(mode="json") != candidate.model_dump(mode="json"):
                raise ValueError("manifest-bound attribution conflicts with score candidate")
            record = attribution
            immutable_reused = True
        elif candidate.outcome_provenance == "legacy_unverified":
            record = attribution
            immutable_reused = True
        else:
            # An old/orphan attribution was never bound to a committed observation. It cannot
            # suppress a recoverable manifest-bound label.
            record = candidate
            save_output(
                state_dir,
                scored_cycle,
                "attribution",
                record.model_dump(mode="json"),
                cadence=cadence,
            )
        by_cycle[record.cycle] = record
    else:
        record = candidate
        by_cycle[record.cycle] = record
        save_output(
            state_dir, scored_cycle, "attribution", record.model_dump(mode="json"), cadence=cadence
        )
    records = [by_cycle[c] for c in sorted(by_cycle)]
    _write_scorecard(sc_path, records)
    calibration_records = [
        item
        for item in records
        if score_record_is_manifest_bound(state_dir, item, cadence=cadence)
        and daily_score_is_learning_eligible(item)
    ]
    detected = detect_recurrences(calibration_records, k=k, window=window)
    detected.extend(_adversary_recovery_recurrences(calibration_records))
    detected.extend(
        _pm_gate_inactive_recurrences(
            state_dir,
            through_cycle=scored_cycle,
            cadence=cadence,
            k=k,
        )
    )
    recs = _filter_recurrences(
        detected,
        memory_dir=memory_dir,
        scored_cycle=scored_cycle,
        active_calibration_roles=active_calibration_roles,
    )
    payload = [r.model_dump(mode="json") for r in recs]
    rec_path.write_text(json.dumps(payload, indent=2))
    return {
        "scored_cycle": scored_cycle,
        "immutable_score_reused": immutable_reused,
        **forecast_status,
        "recurrences": payload,
    }


_ALL_ROLES = (*SPECIALIST_ROLES, "pm", "adversary")
REFLECTOR_HEADS_SCHEMA_VERSION = 1
REFLECTOR_HEADS_NAME = "reflector-heads-v1.json"
REFLECTOR_HEAD_ANCHOR_NAME = "reflector-head-anchor-v1.json"
REFLECTION_AUTHORITY_DIR = "reflector-authority-v1"
_HEAD_SOURCE_BOOTSTRAP = "audited_legacy_bootstrap"
_HEAD_SOURCE_REFLECTION = "reflection"
_HEX_CHARS = frozenset("0123456789abcdef")


def reflector_heads_path(journal_path) -> Path:
    """Return the versioned latest-head artifact beside the local reflector journal."""
    return Path(journal_path).with_name(REFLECTOR_HEADS_NAME)


def reflector_head_anchor_path(state_dir) -> Path:
    """Return the monotonic head anchor kept outside prompt/live-memory rollback sets."""
    return Path(state_dir) / REFLECTOR_HEAD_ANCHOR_NAME


def reflection_authority_path(state_dir, source_cycle: int) -> Path:
    return Path(state_dir) / REFLECTION_AUTHORITY_DIR / f"cycle-{int(source_cycle)}.json"


def reflection_authority_consumption_path(state_dir, source_cycle: int) -> Path:
    return Path(state_dir) / REFLECTION_AUTHORITY_DIR / f"cycle-{int(source_cycle)}-consumed.json"


def _sha256_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_CHARS for character in value)
    )


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Durably replace one file without exposing a partial JSON/journal/prompt to readers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), mode)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _exclusive_write_bytes(path: Path, content: bytes, *, mode: int = 0o400) -> bool:
    """Durably publish immutable bytes once, without exposing a partial final file.

    Returns ``True`` when this call publishes the file and ``False`` when an identical
    file already exists.  A conflicting existing file always fails closed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), mode)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            # The temporary file lives in the same directory, so link publication is atomic and
            # cannot replace an independently-created receipt racing this writer.
            os.link(temporary_name, path)
            published = True
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f"conflicting immutable receipt: {path}") from None
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except Exception:
        # If durability confirmation fails after publication, do not leave a receipt whose caller
        # observed failure. The outer multi-file transaction also restores its own snapshot.
        if published:
            path.unlink(missing_ok=True)
        raise
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _optional_file_snapshot(path: Path) -> tuple[bool, bytes, int | None]:
    if not path.exists():
        return False, b"", None
    return True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_file_snapshot(path: Path, snapshot: tuple[bool, bytes, int | None]) -> None:
    existed, content, mode = snapshot
    if not existed:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mode is not None:
        path.chmod(mode)


def write_reflection_authority(
    state_dir,
    memory_dir,
    *,
    source_cycle: int,
    recurrences_sha256: str,
    recurrences: list[dict],
    journal_path=None,
    heads_path=None,
) -> dict:
    """Exclusive, read-only deterministic receipt authorizing one cycle's recurrence packet."""
    if source_cycle < 1 or not _is_sha256(recurrences_sha256):
        raise ValueError("invalid reflection authority source")
    memory_dir = Path(memory_dir)
    heads_path = Path(heads_path) if heads_path is not None else memory_dir / REFLECTOR_HEADS_NAME
    journal_path = (
        Path(journal_path) if journal_path is not None else memory_dir / "reflector-journal.md"
    )
    if not heads_path.exists():
        raise ValueError("reflector heads missing while sealing recurrence authority")
    canonical_recurrences = [
        Recurrence.model_validate(item).model_dump(mode="json") for item in recurrences
    ]
    if _sha256_bytes(_canonical_json_bytes(canonical_recurrences)) != recurrences_sha256:
        raise ValueError("reflection authority recurrences do not match their digest")
    payload = {
        "heads_file_sha256": _sha256_bytes(heads_path.read_bytes()),
        "journal_sha256": _sha256_bytes(
            journal_path.read_bytes() if journal_path.exists() else b""
        ),
        "recurrences": canonical_recurrences,
        "recurrences_sha256": recurrences_sha256,
        "schema_version": REFLECTOR_HEADS_SCHEMA_VERSION,
        "source_cycle": source_cycle,
    }
    receipt = {
        **payload,
        "receipt_sha256": _sha256_bytes(_canonical_json_bytes(payload)),
    }
    path = reflection_authority_path(state_dir, source_cycle)
    encoded = json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n"
    try:
        _exclusive_write_bytes(path, encoded)
    except ValueError as exc:
        raise ValueError(f"conflicting reflection authority receipt: {path}") from exc
    return receipt


def validate_reflection_authority(
    state_dir,
    *,
    source_cycle: int,
    recurrences_sha256: str,
    expected_heads_file_sha256: str | None = None,
    expected_journal_sha256: str | None = None,
) -> dict:
    path = reflection_authority_path(state_dir, source_cycle)
    try:
        receipt = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"missing or invalid reflection authority receipt: {path}") from exc
    if not isinstance(receipt, dict) or set(receipt) != {
        "heads_file_sha256",
        "journal_sha256",
        "recurrences",
        "receipt_sha256",
        "recurrences_sha256",
        "schema_version",
        "source_cycle",
    }:
        raise ValueError(f"invalid reflection authority receipt structure: {path}")
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    try:
        canonical_recurrences = [
            Recurrence.model_validate(item).model_dump(mode="json")
            for item in receipt["recurrences"]
        ]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid retained authority recurrences: {path}") from exc
    if (
        receipt["schema_version"] != REFLECTOR_HEADS_SCHEMA_VERSION
        or receipt["source_cycle"] != source_cycle
        or receipt["recurrences_sha256"] != recurrences_sha256
        or receipt["recurrences"] != canonical_recurrences
        or _sha256_bytes(_canonical_json_bytes(canonical_recurrences)) != recurrences_sha256
        or not _is_sha256(receipt["receipt_sha256"])
        or receipt["receipt_sha256"] != _sha256_bytes(_canonical_json_bytes(payload))
    ):
        raise ValueError(f"reflection authority receipt mismatch: {path}")
    if (
        expected_heads_file_sha256 is not None
        and receipt["heads_file_sha256"] != expected_heads_file_sha256
    ):
        raise ValueError("reflection authority was sealed against a different head")
    if expected_journal_sha256 is not None and receipt["journal_sha256"] != expected_journal_sha256:
        raise ValueError("reflection authority was sealed against a different journal")
    return receipt


def write_reflection_authority_consumption(
    state_dir,
    memory_dir,
    *,
    source_cycle: int,
    recurrences_sha256: str,
    outcome: str,
) -> dict:
    """Mark one authority packet consumed after apply/no-action completes successfully."""
    if outcome not in {"head_applied", "no_head_change"}:
        raise ValueError("invalid reflection authority consumption outcome")
    authority = validate_reflection_authority(
        state_dir,
        source_cycle=source_cycle,
        recurrences_sha256=recurrences_sha256,
    )
    memory_dir = Path(memory_dir)
    heads_path = memory_dir / REFLECTOR_HEADS_NAME
    journal_path = memory_dir / "reflector-journal.md"
    payload = {
        "authority_receipt_sha256": authority["receipt_sha256"],
        "outcome": outcome,
        "post_heads_file_sha256": _sha256_bytes(heads_path.read_bytes()),
        "post_journal_sha256": _sha256_bytes(
            journal_path.read_bytes() if journal_path.exists() else b""
        ),
        "schema_version": REFLECTOR_HEADS_SCHEMA_VERSION,
        "source_cycle": source_cycle,
    }
    consumption = {
        **payload,
        "consumption_sha256": _sha256_bytes(_canonical_json_bytes(payload)),
    }
    path = reflection_authority_consumption_path(state_dir, source_cycle)
    encoded = json.dumps(consumption, indent=2, sort_keys=True).encode() + b"\n"
    try:
        _exclusive_write_bytes(path, encoded)
    except ValueError as exc:
        raise ValueError(f"conflicting reflection authority consumption: {path}") from exc
    return consumption


def reflection_authority_recovery_status(state_dir, memory_dir, source_cycle: int) -> dict | None:
    """Classify a prior incomplete attempt's immutable Reflector authorization."""
    path = reflection_authority_path(state_dir, source_cycle)
    if not path.exists():
        return None
    receipt_raw = json.loads(path.read_text())
    digest = str(receipt_raw.get("recurrences_sha256", ""))
    receipt = validate_reflection_authority(
        state_dir,
        source_cycle=source_cycle,
        recurrences_sha256=digest,
    )
    memory_dir = Path(memory_dir)
    heads_path = memory_dir / REFLECTOR_HEADS_NAME
    journal_path = memory_dir / "reflector-journal.md"
    current_heads_sha = _sha256_bytes(heads_path.read_bytes())
    current_journal = journal_path.read_bytes() if journal_path.exists() else b""
    current_journal_sha = _sha256_bytes(current_journal)
    consumption_path = reflection_authority_consumption_path(state_dir, source_cycle)
    if consumption_path.exists():
        try:
            consumption = json.loads(consumption_path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError("invalid reflection authority consumption") from exc
        payload = {key: value for key, value in consumption.items() if key != "consumption_sha256"}
        if (
            set(consumption)
            != {
                "authority_receipt_sha256",
                "consumption_sha256",
                "outcome",
                "post_heads_file_sha256",
                "post_journal_sha256",
                "schema_version",
                "source_cycle",
            }
            or consumption.get("authority_receipt_sha256") != receipt["receipt_sha256"]
            or consumption.get("source_cycle") != source_cycle
            or consumption.get("schema_version") != REFLECTOR_HEADS_SCHEMA_VERSION
            or consumption.get("outcome") not in {"head_applied", "no_head_change"}
            or consumption.get("consumption_sha256")
            != _sha256_bytes(_canonical_json_bytes(payload))
            or consumption.get("post_heads_file_sha256") != current_heads_sha
            or consumption.get("post_journal_sha256") != current_journal_sha
        ):
            raise ValueError("reflection authority consumption mismatch")
        return {"status": "consumed", "receipt": receipt}
    applied_events = []
    for line in current_journal.decode().splitlines():
        if not line.startswith("- head_event_v1: "):
            continue
        event = json.loads(line.removeprefix("- head_event_v1: "))
        if int(event.get("source_cycle", -1)) == source_cycle:
            applied_events.append(event)
    if applied_events:
        if any(
            event.get("surfaced_recurrences_sha256") != receipt["recurrences_sha256"]
            for event in applied_events
        ):
            raise ValueError("applied head event conflicts with reflection authority")
        return {"status": "consumed", "receipt": receipt}
    if (
        receipt["heads_file_sha256"] == current_heads_sha
        and receipt["journal_sha256"] == current_journal_sha
    ):
        return {"status": "unconsumed", "receipt": receipt}
    raise ValueError("reflection authority prestate changed without a consumption record")


def _heads_payload(artifact: dict) -> dict:
    return {key: value for key, value in artifact.items() if key != "artifact_sha256"}


def _seal_heads(payload: dict) -> dict:
    return {
        **payload,
        "artifact_sha256": _sha256_bytes(_canonical_json_bytes(payload)),
    }


def _heads_bytes(artifact: dict) -> bytes:
    return json.dumps(artifact, indent=2, sort_keys=True).encode() + b"\n"


def _seal_anchor(payload: dict) -> dict:
    return {
        **payload,
        "anchor_sha256": _sha256_bytes(_canonical_json_bytes(payload)),
    }


def _anchor_bytes(anchor: dict) -> bytes:
    return json.dumps(anchor, indent=2, sort_keys=True).encode() + b"\n"


def _build_anchor(heads: dict, heads_content: bytes, prior: dict | None) -> dict:
    payload = {
        "generation": heads["generation"],
        "heads_artifact_sha256": heads["artifact_sha256"],
        "heads_file_sha256": _sha256_bytes(heads_content),
        "prior_anchor_sha256": prior["anchor_sha256"] if prior is not None else None,
        "schema_version": REFLECTOR_HEADS_SCHEMA_VERSION,
    }
    return _seal_anchor(payload)


def _bootstrap_head(region: str) -> dict:
    return {
        "head_generation": 0,
        "journal_entry_length": None,
        "journal_entry_sha256": None,
        "journal_entry_start": None,
        "proposal_sha256": None,
        "region": region,
        "region_sha256": _sha256_bytes(region.encode()),
        "source": _HEAD_SOURCE_BOOTSTRAP,
        "source_cycle": None,
        "surfaced_recurrences_sha256": None,
    }


def journaled_managed_regions(journal_path) -> dict[str, list[str]]:
    """Return exact managed-region bodies authorized by this desk's local journal."""
    path = Path(journal_path)
    if not path.exists():
        return {}
    sections: dict[str, list[str]] = {}
    current_role: str | None = None
    body: list[str] = []

    def flush() -> None:
        if current_role is None:
            return
        text = "\n".join(body)
        marker = "- region:\n"
        if marker in text:
            region = text.split(marker, 1)[1].strip()
            sections.setdefault(current_role, []).append(region)

    for line in path.read_text().splitlines():
        if line.startswith("## ") and " — " in line:
            flush()
            current_role = line[3:].split(" — ", 1)[0].strip()
            body = []
        else:
            body.append(line)
    flush()
    return sections


def _validate_reflection_head_entry(
    role: str,
    record: dict,
    *,
    journal: bytes,
    generation: int,
) -> None:
    start = record["journal_entry_start"]
    length = record["journal_entry_length"]
    entry_sha256 = record["journal_entry_sha256"]
    proposal_sha256 = record["proposal_sha256"]
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or start < 0
        or not isinstance(length, int)
        or isinstance(length, bool)
        or length <= 0
        or not _is_sha256(entry_sha256)
        or not _is_sha256(proposal_sha256)
        or start + length > len(journal)
    ):
        raise ValueError(f"{role}: invalid reflection journal pointer")
    entry = journal[start : start + length]
    if _sha256_bytes(entry) != entry_sha256:
        raise ValueError(f"{role}: reflection journal entry digest mismatch")
    try:
        entry_text = entry.decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{role}: reflection journal entry is not UTF-8") from exc
    event_lines = [
        line.removeprefix("- head_event_v1: ")
        for line in entry_text.splitlines()
        if line.startswith("- head_event_v1: ")
    ]
    if len(event_lines) != 1:
        raise ValueError(f"{role}: reflection journal entry has no unique head event")
    try:
        event = json.loads(event_lines[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{role}: invalid reflection head event") from exc
    expected_event = {
        "generation": record["head_generation"],
        "proposal_sha256": proposal_sha256,
        "region_sha256": record["region_sha256"],
        "role": role,
        "schema_version": REFLECTOR_HEADS_SCHEMA_VERSION,
        "source_cycle": record["source_cycle"],
        "surfaced_recurrences_sha256": record["surfaced_recurrences_sha256"],
    }
    if event != expected_event:
        raise ValueError(f"{role}: reflection head event does not match the head record")
    proposal_lines = [
        line.removeprefix("- proposal_v1: ")
        for line in entry_text.splitlines()
        if line.startswith("- proposal_v1: ")
    ]
    recurrence_lines = [
        line.removeprefix("- surfaced_recurrences_v1: ")
        for line in entry_text.splitlines()
        if line.startswith("- surfaced_recurrences_v1: ")
    ]
    if len(proposal_lines) != 1 or len(recurrence_lines) != 1:
        raise ValueError(f"{role}: reflection journal entry is missing retained source payloads")
    try:
        retained_proposal = ReflectionProposal.model_validate_json(proposal_lines[0]).model_dump(
            mode="json"
        )
        raw_recurrences = json.loads(recurrence_lines[0])
        if not isinstance(raw_recurrences, list):
            raise ValueError("recurrences must be a list")
        retained_recurrences = [
            Recurrence.model_validate(item).model_dump(mode="json") for item in raw_recurrences
        ]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{role}: invalid retained reflection source payload") from exc
    if _sha256_bytes(_canonical_json_bytes(retained_proposal)) != proposal_sha256:
        raise ValueError(f"{role}: retained proposal digest mismatch")
    if (
        _sha256_bytes(_canonical_json_bytes(retained_recurrences))
        != record["surfaced_recurrences_sha256"]
    ):
        raise ValueError(f"{role}: retained recurrence digest mismatch")
    role_edits = [edit for edit in retained_proposal["edits"] if edit["role"] == role]
    if not role_edits or role_edits[-1]["region_text"].strip() != record["region"]:
        raise ValueError(f"{role}: retained proposal does not authorize the head region")
    if role not in {recurrence["role"] for recurrence in retained_recurrences}:
        raise ValueError(f"{role}: retained recurrences do not authorize this role")
    if record["head_generation"] > generation:
        raise ValueError(f"{role}: head generation exceeds artifact generation")


def _load_reflector_anchor(anchor_path: Path, heads: dict, heads_content: bytes) -> dict:
    if not anchor_path.exists():
        raise ValueError(f"missing {anchor_path.name}; run --bootstrap-heads explicitly")
    try:
        anchor = json.loads(anchor_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {anchor_path.name}") from exc
    if not isinstance(anchor, dict) or set(anchor) != {
        "anchor_sha256",
        "generation",
        "heads_artifact_sha256",
        "heads_file_sha256",
        "prior_anchor_sha256",
        "schema_version",
    }:
        raise ValueError(f"invalid {anchor_path.name} structure")
    payload = {key: value for key, value in anchor.items() if key != "anchor_sha256"}
    if (
        anchor["schema_version"] != REFLECTOR_HEADS_SCHEMA_VERSION
        or not _is_sha256(anchor["anchor_sha256"])
        or anchor["anchor_sha256"] != _sha256_bytes(_canonical_json_bytes(payload))
    ):
        raise ValueError(f"{anchor_path.name} self-digest mismatch")
    if (
        anchor["generation"] != heads["generation"]
        or anchor["heads_artifact_sha256"] != heads["artifact_sha256"]
        or anchor["heads_file_sha256"] != _sha256_bytes(heads_content)
    ):
        raise ValueError(f"{anchor_path.name} does not authorize the current reflector head")
    if anchor["generation"] == 0:
        if anchor["prior_anchor_sha256"] is not None:
            raise ValueError(f"{anchor_path.name} generation zero has a prior anchor")
    elif not _is_sha256(anchor["prior_anchor_sha256"]):
        raise ValueError(f"{anchor_path.name} is missing its prior anchor link")
    return anchor


def _load_reflector_heads(
    agents_dir,
    journal_path,
    heads_path=None,
    anchor_path=None,
    *,
    require_anchor: bool = True,
) -> dict:
    """Load and strictly verify the current reflector-head trust anchor.

    The whole local journal is bound, each active prompt must equal its role's exact current head,
    and reflection-derived heads additionally point at the exact structured journal append that
    installed them. Historical journal bodies are deliberately irrelevant here.
    """
    agents_dir = Path(agents_dir)
    journal_path = Path(journal_path)
    heads_path = Path(heads_path) if heads_path is not None else reflector_heads_path(journal_path)
    anchor_path = (
        Path(anchor_path)
        if anchor_path is not None
        else journal_path.with_name(REFLECTOR_HEAD_ANCHOR_NAME)
    )
    if not heads_path.exists():
        raise ValueError(
            f"missing {heads_path.name}; run reflector_apply.py --bootstrap-heads explicitly"
        )
    try:
        artifact = json.loads(heads_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {heads_path.name}") from exc
    if not isinstance(artifact, dict) or set(artifact) != {
        "artifact_sha256",
        "generation",
        "journal_sha256",
        "roles",
        "schema_version",
    }:
        raise ValueError(f"invalid {heads_path.name} structure")
    if artifact["schema_version"] != REFLECTOR_HEADS_SCHEMA_VERSION:
        raise ValueError(f"unsupported {heads_path.name} schema_version")
    generation = artifact["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError(f"invalid {heads_path.name} generation")
    if not _is_sha256(artifact["artifact_sha256"]) or artifact["artifact_sha256"] != _sha256_bytes(
        _canonical_json_bytes(_heads_payload(artifact))
    ):
        raise ValueError(f"{heads_path.name} self-digest mismatch")
    journal = journal_path.read_bytes() if journal_path.exists() else b""
    if not _is_sha256(artifact["journal_sha256"]) or artifact["journal_sha256"] != _sha256_bytes(
        journal
    ):
        raise ValueError(f"{heads_path.name} journal digest mismatch")
    roles = artifact["roles"]
    if not isinstance(roles, dict) or set(roles) != set(_ALL_ROLES):
        raise ValueError(f"{heads_path.name} must contain exactly the five desk roles")
    journaled = journaled_managed_regions(journal_path)
    record_keys = {
        "head_generation",
        "journal_entry_length",
        "journal_entry_sha256",
        "journal_entry_start",
        "proposal_sha256",
        "region",
        "region_sha256",
        "source",
        "source_cycle",
        "surfaced_recurrences_sha256",
    }
    for role in _ALL_ROLES:
        record = roles[role]
        if not isinstance(record, dict) or set(record) != record_keys:
            raise ValueError(f"{role}: invalid reflector head structure")
        region = record["region"]
        if not isinstance(region, str):
            raise ValueError(f"{role}: reflector head region must be text")
        try:
            assert_valid_region(region)
        except PromptGuardError as exc:
            raise ValueError(f"{role}: invalid reflector head region") from exc
        if not _is_sha256(record["region_sha256"]) or record["region_sha256"] != _sha256_bytes(
            region.encode()
        ):
            raise ValueError(f"{role}: reflector head region digest mismatch")
        head_generation = record["head_generation"]
        if (
            not isinstance(head_generation, int)
            or isinstance(head_generation, bool)
            or head_generation < 0
            or head_generation > generation
        ):
            raise ValueError(f"{role}: invalid head generation")
        source = record["source"]
        if source == _HEAD_SOURCE_BOOTSTRAP:
            if head_generation != 0 or any(
                record[field] is not None
                for field in (
                    "journal_entry_length",
                    "journal_entry_sha256",
                    "journal_entry_start",
                    "proposal_sha256",
                    "source_cycle",
                    "surfaced_recurrences_sha256",
                )
            ):
                raise ValueError(f"{role}: invalid explicit-bootstrap head")
            # A non-empty migration head must have existed in the pre-migration local journal.
            # Blank heads are explicit prompt-state declarations: never infer them from ambiguous
            # legacy correction rows whose `region:` field happened to be empty.
            if region and region not in journaled.get(role, []):
                raise ValueError(f"{role}: bootstrap head is not backed by the local journal")
        elif source == _HEAD_SOURCE_REFLECTION:
            if (
                not isinstance(record["source_cycle"], int)
                or isinstance(record["source_cycle"], bool)
                or record["source_cycle"] < 0
                or not _is_sha256(record["surfaced_recurrences_sha256"])
            ):
                raise ValueError(f"{role}: invalid reflection source provenance")
            validate_reflection_authority(
                anchor_path.parent,
                source_cycle=record["source_cycle"],
                recurrences_sha256=record["surfaced_recurrences_sha256"],
            )
            _validate_reflection_head_entry(role, record, journal=journal, generation=generation)
        else:
            raise ValueError(f"{role}: unknown reflector head source")
        prompt_path = agents_dir / f"{role}.md"
        if not prompt_path.exists():
            raise ValueError(f"{role}: missing prompt")
        try:
            _prefix, prompt_region, _suffix = split_managed(prompt_path.read_text())
        except PromptGuardError as exc:
            raise ValueError(f"{role}: {exc}") from exc
        if prompt_region.strip() != region:
            raise ValueError(f"{role}: active managed region does not match its latest head")
    if require_anchor:
        _load_reflector_anchor(anchor_path, artifact, heads_path.read_bytes())
    return artifact


def bootstrap_reflector_heads(agents_dir, journal_path, heads_path=None, anchor_path=None) -> dict:
    """Explicitly establish the v1 head trust anchor from the reviewed active prompts.

    Existing valid heads make this idempotent. Existing invalid heads are never overwritten. A
    non-empty active region must already occur in the local journal; a blank active region is
    recorded explicitly instead of being guessed from the journal's legacy blank correction rows.
    """
    agents_dir = Path(agents_dir)
    journal_path = Path(journal_path)
    heads_path = Path(heads_path) if heads_path is not None else reflector_heads_path(journal_path)
    anchor_path = (
        Path(anchor_path)
        if anchor_path is not None
        else journal_path.with_name(REFLECTOR_HEAD_ANCHOR_NAME)
    )
    if heads_path.exists():
        if anchor_path.exists():
            artifact = _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
            return {"created": False, "anchor_created": False, "artifact": artifact}
        artifact = _load_reflector_heads(
            agents_dir, journal_path, heads_path, anchor_path, require_anchor=False
        )
        if artifact["generation"] != 0 or any(
            record["source"] != _HEAD_SOURCE_BOOTSTRAP for record in artifact["roles"].values()
        ):
            raise ValueError("a missing anchor may only migrate an audited generation-zero head")
        anchor = _build_anchor(artifact, heads_path.read_bytes(), None)
        anchor_snapshot = _optional_file_snapshot(anchor_path)
        try:
            _atomic_write_bytes(anchor_path, _anchor_bytes(anchor))
            _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
        except BaseException:
            _restore_file_snapshot(anchor_path, anchor_snapshot)
            raise
        return {"created": False, "anchor_created": True, "artifact": artifact}
    if anchor_path.exists():
        raise ValueError(
            f"{heads_path.name} is missing but {anchor_path.name} already exists; "
            "refusing rebootstrap"
        )
    journal = journal_path.read_bytes() if journal_path.exists() else b""
    journaled = journaled_managed_regions(journal_path)
    roles: dict[str, dict] = {}
    for role in _ALL_ROLES:
        prompt_path = agents_dir / f"{role}.md"
        if not prompt_path.exists():
            raise ValueError(f"{role}: missing prompt")
        try:
            _prefix, prompt_region, _suffix = split_managed(prompt_path.read_text())
            region = prompt_region.strip()
            assert_valid_region(region)
        except PromptGuardError as exc:
            raise ValueError(f"{role}: {exc}") from exc
        if region and region not in journaled.get(role, []):
            raise ValueError(f"{role}: active region is not backed by the local journal")
        roles[role] = _bootstrap_head(region)
    payload = {
        "generation": 0,
        "journal_sha256": _sha256_bytes(journal),
        "roles": roles,
        "schema_version": REFLECTOR_HEADS_SCHEMA_VERSION,
    }
    artifact = _seal_heads(payload)
    heads_content = _heads_bytes(artifact)
    anchor = _build_anchor(artifact, heads_content, None)
    heads_snapshot = _optional_file_snapshot(heads_path)
    anchor_snapshot = _optional_file_snapshot(anchor_path)
    try:
        _atomic_write_bytes(heads_path, heads_content)
        _atomic_write_bytes(anchor_path, _anchor_bytes(anchor))
        # Read-after-write verifies durable bytes and prompts before success is claimed.
        verified = _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
    except BaseException:
        _restore_file_snapshot(heads_path, heads_snapshot)
        _restore_file_snapshot(anchor_path, anchor_snapshot)
        raise
    return {"created": True, "anchor_created": True, "artifact": verified}


def audit_managed_region_provenance(
    agents_dir, journal_path, heads_path=None, anchor_path=None
) -> list[str]:
    """List failures binding active regions to their exact, versioned latest heads."""
    try:
        _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
    except ValueError as exc:
        return [str(exc)]
    return []


def scored_cycles(memory_dir, *, state_dir=None, cadence: str = "rebal") -> set[int]:
    """Cycles with verified scores (state-backed when a state directory is supplied)."""
    return {
        record.cycle
        for record in _read_scorecard(
            Path(memory_dir) / "scorecard.jsonl",
            state_dir=state_dir,
            cadence=cadence,
        )
        if record.outcome_provenance == "manifest_bound"
    }


def validate_edit_citations(
    edit: dict, cycles: set[int], current_cycle: int | None = None
) -> str | None:
    """Deterministic evidence-integrity guard (2026-07 review: the Reflector cited per-cycle
    scores for cycles that had NO ScoreRecord — fabricated evidence tuned live decision prompts).

    The fabrication mode is a claim about a PAST cycle's measured score that doesn't exist. A
    reference to the CURRENT cycle (the note's "[cN]" date tag) or a FUTURE cycle (a `retire_if:
    ... by cM` target) legitimately has no scorecard record yet — those are not evidence claims.
    So only a cited cycle STRICTLY BEFORE `current_cycle` must exist in scorecard.jsonl. When
    `current_cycle` is None, every cited cycle must exist (strict legacy behaviour). Returns a
    refusal reason, or None when clean."""
    import re

    text = " ".join(
        [
            str(edit.get("region_text", "")),
            str(edit.get("reason", "")),
            " ".join(str(e) for e in edit.get("evidence", [])),
        ]
    )
    cited = {int(m) for m in re.findall(r"\bc(?:ycle\s*)?(\d{1,4})\b", text, flags=re.IGNORECASE)}
    if current_cycle is not None:
        cited = {c for c in cited if c < current_cycle}  # only PAST-score claims are checkable
    missing = sorted(c for c in cited if c not in cycles)
    if missing:
        return (
            f"cites past cycle(s) {missing} with no ScoreRecord in scorecard.jsonl — "
            "fabricated or unverifiable evidence; edit refused"
        )
    return None


def _journal_entry_bytes(
    role: str,
    edit: dict,
    *,
    generation: int,
    proposal_sha256: str,
    source_cycle: int,
    surfaced_recurrences_sha256: str,
    canonical_proposal: dict,
    canonical_recurrences: list[dict],
    region: str,
) -> bytes:
    event = {
        "generation": generation,
        "proposal_sha256": proposal_sha256,
        "region_sha256": _sha256_bytes(region.encode()),
        "role": role,
        "schema_version": REFLECTOR_HEADS_SCHEMA_VERSION,
        "source_cycle": source_cycle,
        "surfaced_recurrences_sha256": surfaced_recurrences_sha256,
    }
    lines = [
        f"## {role} — {edit.get('reason', '')}",
        f"- retire_if: {edit.get('retire_if', '')}",
        f"- evidence: {'; '.join(edit.get('evidence', []))}",
        f"- head_event_v1: {_canonical_json_bytes(event).decode()}",
        f"- proposal_v1: {_canonical_json_bytes(canonical_proposal).decode()}",
        f"- surfaced_recurrences_v1: {_canonical_json_bytes(canonical_recurrences).decode()}",
        f"- region:\n{region}\n",
    ]
    return ("\n".join(lines) + "\n").encode()


def apply_reflection(
    proposal: dict,
    agents_dir,
    journal_path,
    *,
    allowed_roles: set[str] | None = None,
    known_cycles: set[int] | None = None,
    current_cycle: int | None = None,
    surfaced_recurrences: list[dict] | None = None,
    sealed_recurrences_sha256: str | None = None,
    anchor_path=None,
) -> dict:
    """Apply a reflection proposal to the agent prompts, guarded to the managed region.

    `allowed_roles` (when not None) restricts edits to roles the scorecard surfaced a
    recurrence for — a hallucinated edit to an unmentioned role is skipped. At most one
    edit per role (last wins). `known_cycles` (when not None) enables the evidence-integrity
    guard: an edit citing a PAST cycle with no ScoreRecord is refused (fabricated evidence).
    `current_cycle` scopes that guard to past-score claims only (the note's own [cN] tag and a
    future retire_if target are legitimately unscored). Every applied head is bound to that source
    cycle, the canonical proposal, and the complete canonical surfaced-recurrence packet."""
    agents_dir = Path(agents_dir)
    journal_path = Path(journal_path)
    heads_path = reflector_heads_path(journal_path)
    anchor_path = (
        Path(anchor_path)
        if anchor_path is not None
        else journal_path.with_name(REFLECTOR_HEAD_ANCHOR_NAME)
    )
    heads = _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
    prior_anchor = _load_reflector_anchor(anchor_path, heads, heads_path.read_bytes())
    canonical_proposal = ReflectionProposal.model_validate(proposal).model_dump(mode="json")
    applied: list[str] = []
    skipped: list[tuple[str, str]] = []
    # dedupe per role (last edit wins) so a role is edited at most once per cycle
    edits_by_role: dict = {}
    order: list = []
    for edit in canonical_proposal["edits"]:
        role = edit.get("role")
        if role not in order:
            order.append(role)
        edits_by_role[role] = edit
    prompt_updates: dict[Path, bytes] = {}
    applied_edits: list[tuple[str, dict, str]] = []
    for role in order:
        edit = edits_by_role[role]
        path = agents_dir / f"{role}.md"
        if role not in _ALL_ROLES or not path.exists():
            skipped.append((role, "unknown role or missing file"))
            continue
        if allowed_roles is not None and role not in allowed_roles:
            skipped.append((role, "no surfaced recurrence for this role"))
            continue
        if known_cycles is not None:
            refusal = validate_edit_citations(edit, known_cycles, current_cycle=current_cycle)
            if refusal:
                skipped.append((role, refusal))
                continue
        old = path.read_text()
        try:
            new = splice_managed(old, edit.get("region_text", ""))
            assert_only_region_changed(old, new)
        except PromptGuardError as e:
            skipped.append((role, str(e)))
            continue
        _prefix, region, _suffix = split_managed(new)
        prompt_updates[path] = new.encode()
        applied_edits.append((role, edit, region.strip()))
        applied.append(role)
    if not applied:
        return {"applied": applied, "skipped": skipped}

    if not isinstance(current_cycle, int) or isinstance(current_cycle, bool) or current_cycle < 1:
        raise ValueError("an applied reflection requires a positive source cycle")
    if not isinstance(surfaced_recurrences, list) or any(
        not isinstance(recurrence, dict) for recurrence in surfaced_recurrences
    ):
        raise ValueError("an applied reflection requires the complete surfaced recurrence packet")
    canonical_recurrences = [
        Recurrence.model_validate(recurrence).model_dump(mode="json")
        for recurrence in surfaced_recurrences
    ]
    surfaced_roles = {recurrence["role"] for recurrence in canonical_recurrences}
    if allowed_roles is None or set(allowed_roles) != surfaced_roles:
        raise ValueError("allowed roles do not match the surfaced recurrence packet")
    proposal_sha256 = _sha256_bytes(_canonical_json_bytes(canonical_proposal))
    recurrences_sha256 = _sha256_bytes(_canonical_json_bytes(canonical_recurrences))
    if not _is_sha256(sealed_recurrences_sha256) or sealed_recurrences_sha256 != recurrences_sha256:
        raise ValueError("surfaced recurrence packet does not match its desk_score seal")
    generation = int(heads["generation"]) + 1
    old_journal = journal_path.read_bytes() if journal_path.exists() else b""
    if old_journal and not old_journal.endswith(b"\n"):
        raise ValueError("reflector journal is not newline-terminated")
    prior_source_cycles: list[int] = []
    for line in old_journal.decode().splitlines():
        if not line.startswith("- head_event_v1: "):
            continue
        try:
            event = json.loads(line.removeprefix("- head_event_v1: "))
            prior_source_cycles.append(int(event["source_cycle"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid prior reflector head event") from exc
    if prior_source_cycles and current_cycle <= max(prior_source_cycles):
        raise ValueError(
            "reflection source cycle was already handled or predates the latest head event"
        )
    validate_reflection_authority(
        anchor_path.parent,
        source_cycle=current_cycle,
        recurrences_sha256=recurrences_sha256,
        expected_heads_file_sha256=_sha256_bytes(heads_path.read_bytes()),
        expected_journal_sha256=_sha256_bytes(old_journal),
    )
    new_journal = bytearray(old_journal)
    roles = json.loads(json.dumps(heads["roles"]))
    for role, edit, region in applied_edits:
        entry = _journal_entry_bytes(
            role,
            edit,
            generation=generation,
            proposal_sha256=proposal_sha256,
            source_cycle=current_cycle,
            surfaced_recurrences_sha256=recurrences_sha256,
            canonical_proposal=canonical_proposal,
            canonical_recurrences=canonical_recurrences,
            region=region,
        )
        start = len(new_journal)
        new_journal.extend(entry)
        roles[role] = {
            "head_generation": generation,
            "journal_entry_length": len(entry),
            "journal_entry_sha256": _sha256_bytes(entry),
            "journal_entry_start": start,
            "proposal_sha256": proposal_sha256,
            "region": region,
            "region_sha256": _sha256_bytes(region.encode()),
            "source": _HEAD_SOURCE_REFLECTION,
            "source_cycle": current_cycle,
            "surfaced_recurrences_sha256": recurrences_sha256,
        }
    new_heads = _seal_heads(
        {
            "generation": generation,
            "journal_sha256": _sha256_bytes(bytes(new_journal)),
            "roles": roles,
            "schema_version": REFLECTOR_HEADS_SCHEMA_VERSION,
        }
    )
    new_heads_content = _heads_bytes(new_heads)
    new_anchor = _build_anchor(new_heads, new_heads_content, prior_anchor)
    prompt_snapshots = {path: _optional_file_snapshot(path) for path in prompt_updates}
    journal_snapshot = _optional_file_snapshot(journal_path)
    heads_snapshot = _optional_file_snapshot(heads_path)
    anchor_snapshot = _optional_file_snapshot(anchor_path)
    try:
        for path, content in prompt_updates.items():
            _atomic_write_bytes(path, content)
        _atomic_write_bytes(journal_path, bytes(new_journal))
        _atomic_write_bytes(heads_path, new_heads_content)
        _atomic_write_bytes(anchor_path, _anchor_bytes(new_anchor))
        _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
    except BaseException:
        for path, snapshot in prompt_snapshots.items():
            _restore_file_snapshot(path, snapshot)
        _restore_file_snapshot(journal_path, journal_snapshot)
        _restore_file_snapshot(heads_path, heads_snapshot)
        _restore_file_snapshot(anchor_path, anchor_snapshot)
        raise
    return {"applied": applied, "skipped": skipped}
