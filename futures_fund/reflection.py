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
from base64 import b64decode, b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.desk_contracts import (
    Book,
    BookLeg,
    ReflectionProposal,
    SpecialistRead,
    validate_unique_reflection_edit_roles,
)
from futures_fund.durable_io import (
    durable_unlink,
    durable_write_bytes,
    durable_write_text,
    fsync_directory,
)
from futures_fund.heartbeat import verify_heartbeat_completion
from futures_fund.precheck import (
    _entry_execution_side,
    _exit_execution_side,
    _one_way_friction_usd,
)
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
    CURRENT_SCORE_SCHEMA_VERSION,
    SCORECARD_MIGRATION_WAL_FILE,
    Recurrence,
    ScoreRecord,
    classify_objections,
    detect_recurrences,
    forward_returns,
    parse_score_record_json,
    realized_edge_frac,
    score_book,
    score_candidate_opportunities,
    score_specialist,
)
from futures_fund.slippage import ExecutionRealism

SPECIALIST_ROLES = ("sentiment", "technical", "futures")
CANONICAL_BTC_SYMBOL = "BTC/USDT:USDT"
RECURRENCE_COOLDOWN_CYCLES = 3
DAILY_LEARNING_HORIZON_HOURS = 24.0
# A full cycle is scheduled for the same UTC slot every day, but process/network jitter can put a
# committed mark a few seconds before the exact decision timestamp anniversary. Treat that mark
# as the scheduled observation without pretending the actual elapsed time was exactly 24h.
SCHEDULED_MARK_TOLERANCE = timedelta(minutes=5)
FORECAST_SCORE_SCHEMA_VERSION = 5
# Schema v4 introduced the correct leg-nonoverlap/time-cohort calibration contract. Preserve its
# gross forecast evidence after v5 adds a separately fail-closed transaction-cost-net view.
FORECAST_CALIBRATION_MIN_SCHEMA_VERSION = 4
FORECAST_COST_NET_MIN_SCHEMA_VERSION = 5
FORECAST_ROUND_TRIP_COST_MODEL = (
    "origin_directional_l2_full_target_round_trip_with_fixed_book_legging_v1"
)
CANDIDATE_SCORE_SCHEMA_VERSION = 1
FORECAST_EDGE_CHANGE_ABS_FRAC = 0.0025
FORECAST_EDGE_CHANGE_REL_FRAC = 0.25
ADVERSARY_RECOVERY_WINDOW = 6


def _marks_sha256(marks: dict[str, float]) -> str:
    encoded = json.dumps(marks, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def forecast_cohort_membership_sha256(
    origin_cycle: int,
    forecast_horizon_hours: int,
    symbols: list[str],
) -> str:
    """Hash one canonical same-origin, same-horizon alpha membership packet."""
    payload = {
        "origin_cycle": int(origin_cycle),
        "forecast_horizon_hours": int(forecast_horizon_hours),
        "symbols": symbols,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _forecast_cohort_membership(
    book: Book,
    *,
    origin_cycle: int,
    forecast_horizon_hours: int,
    btc_symbol: str,
) -> dict:
    """Derive the complete alpha cohort from the manifest-bound origin Book."""
    symbols = sorted(
        leg.symbol
        for leg in book.legs
        if leg.seat_role == "alpha"
        and leg.symbol != btc_symbol
        and int(leg.edge_horizon_hours) == int(forecast_horizon_hours)
    )
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("forecast cohort membership must be non-empty and duplicate-free")
    return {
        "forecast_cohort_expected_symbols": symbols,
        "forecast_cohort_expected_member_count": len(symbols),
        "forecast_cohort_expected_symbols_sha256": forecast_cohort_membership_sha256(
            origin_cycle,
            forecast_horizon_hours,
            symbols,
        ),
    }


def _marks_cover_positive_finite(marks: dict[str, float], required: set[str]) -> bool:
    if not required.issubset(marks):
        return False
    try:
        values = [float(marks[symbol]) for symbol in required]
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(value) and value > 0.0 for value in values)


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
    if (path.parent / SCORECARD_MIGRATION_WAL_FILE).exists():
        raise ValueError("scorecard migration is incomplete; recover it before reading scores")
    if not path.exists():
        return []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = parse_score_record_json(line)
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
    changed = False
    if not isinstance(handled, dict):
        handled = {}
        changed = True
    for recurrence in recurrences:
        kind = str(recurrence.get("kind", ""))
        role = str(recurrence.get("role", ""))
        if kind and role:
            key = f"{kind}:{role}"
            try:
                prior_cycle = int(handled.get(key, {}).get("cycle", -1))
            except (AttributeError, TypeError, ValueError):
                prior_cycle = -1
            # Crash recovery may replay an older consumed authority after a newer event was
            # handled. Never move the cooldown clock backwards.
            next_cycle = max(prior_cycle, int(cycle))
            if prior_cycle != next_cycle:
                handled[key] = {"cycle": next_cycle}
                changed = True
    if changed:
        durable_write_text(path, json.dumps(handled, indent=2, sort_keys=True) + "\n")


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


def _read_strict_json(path: Path):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON object: {path}") from exc


def _read_strict_json_object(path: Path) -> dict:
    value = _read_strict_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


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
            if schema_version not in (1, 2, 3, 4, 5):
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
                if schema_version >= 5:
                    expected_symbols = row.get("forecast_cohort_expected_symbols")
                    expected_count = row.get("forecast_cohort_expected_member_count")
                    expected_sha256 = row.get("forecast_cohort_expected_symbols_sha256")
                    if (
                        not isinstance(expected_symbols, list)
                        or not expected_symbols
                        or not all(
                            isinstance(expected_symbol, str) and expected_symbol
                            for expected_symbol in expected_symbols
                        )
                        or expected_symbols != sorted(set(expected_symbols))
                        or btc_symbol in expected_symbols
                        or key[1] not in expected_symbols
                    ):
                        raise ValueError("invalid forecast cohort expected symbols")
                    if (
                        type(expected_count) is not int
                        or expected_count != len(expected_symbols)
                    ):
                        raise ValueError("invalid forecast cohort expected member count")
                    horizon = float(row["forecast_horizon_hours"])
                    if not horizon.is_integer() or expected_sha256 != (
                        forecast_cohort_membership_sha256(
                            key[0], int(horizon), expected_symbols
                        )
                    ):
                        raise ValueError("invalid forecast cohort expected symbols hash")
                    if row.get("round_trip_cost_model") != FORECAST_ROUND_TRIP_COST_MODEL:
                        raise ValueError("unsupported round-trip forecast cost model")
                    for field in ("origin_precheck_sha256", "origin_risk_model_sha256"):
                        value = row.get(field)
                        if value is not None and not _is_sha256(value):
                            raise ValueError(f"invalid {field}")
                    priced = row.get("round_trip_friction_priced")
                    if not isinstance(priced, bool):
                        raise ValueError("round_trip_friction_priced must be boolean")
                    if not isinstance(
                        row.get("round_trip_cost_exclusion_reasons"), list
                    ) or not all(
                        isinstance(reason, str)
                        for reason in row["round_trip_cost_exclusion_reasons"]
                    ):
                        raise ValueError("invalid round_trip_cost_exclusion_reasons")
                    cost_fields = (
                        "round_trip_entry_friction_usd",
                        "round_trip_exit_friction_usd",
                        "round_trip_friction_usd",
                        "round_trip_friction_frac",
                        "realized_round_trip_cost_net_price_edge_frac",
                    )
                    if priced:
                        if row.get("origin_precheck_sha256") is None:
                            raise ValueError("priced round-trip forecast lacks bound precheck")
                        if float(row["target_notional"]) <= 0.0:
                            raise ValueError("priced round-trip forecast has nonpositive target")
                        values = [row.get(field) for field in cost_fields]
                        if any(
                            value is None
                            or not math.isfinite(float(value))
                            or float(value) < 0.0 and field != cost_fields[-1]
                            for field, value in zip(cost_fields, values, strict=True)
                        ):
                            raise ValueError("invalid priced round-trip forecast costs")
                        entry_cost = float(row["round_trip_entry_friction_usd"])
                        exit_cost = float(row["round_trip_exit_friction_usd"])
                        total_cost = float(row["round_trip_friction_usd"])
                        cost_frac = float(row["round_trip_friction_frac"])
                        if not math.isclose(
                            total_cost, entry_cost + exit_cost, rel_tol=0.0, abs_tol=1e-12
                        ) or not math.isclose(
                            cost_frac,
                            total_cost / float(row["target_notional"]),
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        ):
                            raise ValueError("round-trip forecast costs are inconsistent")
                        if not math.isclose(
                            float(row["realized_round_trip_cost_net_price_edge_frac"]),
                            realized - cost_frac,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        ):
                            raise ValueError("cost-net forecast edge is inconsistent")
                        if row["round_trip_cost_exclusion_reasons"]:
                            raise ValueError("priced round-trip forecast has exclusion reasons")
                    else:
                        if any(row.get(field) is not None for field in cost_fields):
                            raise ValueError("unpriced round-trip forecast exposes cost values")
                        if not row["round_trip_cost_exclusion_reasons"]:
                            raise ValueError("unpriced round-trip forecast lacks an exclusion")
                    reserve = row.get("round_trip_legging_reserve_bps")
                    if reserve is not None and (
                        not math.isfinite(float(reserve)) or float(reserve) < 0.0
                    ):
                        raise ValueError("invalid round_trip_legging_reserve_bps")
                    expected_cost_net_eligible = expected_learning_eligible and priced
                    if row.get("cost_net_learning_eligible") is not expected_cost_net_eligible:
                        raise ValueError("cost_net_learning_eligible is inconsistent")
                    reasons = row.get("cost_net_learning_exclusion_reasons")
                    if not isinstance(reasons, list) or not all(
                        isinstance(reason, str) for reason in reasons
                    ):
                        raise ValueError("invalid cost_net_learning_exclusion_reasons")
                    if bool(reasons) is expected_cost_net_eligible:
                        raise ValueError("cost-net eligibility/reasons are inconsistent")
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
        directory = cycle_dir(state_dir, observation_cycle, cadence=cadence)
        path = directory / "scoring_marks.json"
        artifact_sha256 = completed_artifact_sha256(
            state_dir, observation_cycle, "scoring_marks", cadence=cadence
        )
        if artifact_sha256 is None:
            if path.exists():
                raise ValueError(
                    f"completed cycle {observation_cycle} has unbound scoring marks"
                )
            continue
        try:
            related_hashes = {
                name: completed_artifact_sha256(
                    state_dir, observation_cycle, name, cadence=cadence
                )
                for name in ("evidence", "meta", "report")
            }
            if any(value is None for value in related_hashes.values()):
                raise ValueError("scoring observation lacks bound evidence/meta/report")
            raw = _read_strict_json_object(path)
            if set(raw) != {"as_of_ts", "marks"}:
                raise ValueError("scoring marks have an unsupported shape")
            report = _read_strict_json_object(directory / "report.json")
            meta = _read_strict_json_object(directory / "meta.json")
            evidence = _read_strict_json(directory / "evidence.json")
            if not isinstance(evidence, list) or not evidence:
                raise ValueError("scoring observation evidence must be a non-empty list")
            if (
                type(meta.get("cycle")) is not int
                or meta["cycle"] != observation_cycle
                or type(report.get("cycle")) is not int
                or report["cycle"] != observation_cycle
                or meta.get("btc_symbol") != CANONICAL_BTC_SYMBOL
                or meta.get("scoring_marks_sha256") != artifact_sha256
                or meta.get("evidence_sha256") != related_hashes["evidence"]
            ):
                raise ValueError("scoring observation identity/hash binding is inconsistent")
            observation_ts = _as_utc(str(raw["as_of_ts"]))
            if not (
                observation_ts == _as_utc(str(report["decision_ts"]))
                == _as_utc(str(meta["now"]))
            ):
                raise ValueError("scoring observation timestamps do not identify one cycle")
            raw_marks = raw["marks"]
            if not isinstance(raw_marks, dict) or not raw_marks:
                raise ValueError("marks must be a non-empty object")
            marks = {str(symbol): float(mark) for symbol, mark in raw_marks.items()}
            if any(
                not symbol or not math.isfinite(mark) or mark <= 0.0
                for symbol, mark in marks.items()
            ):
                raise ValueError("marks must be finite and positive")
            evidence_marks: dict[str, float] = {}
            for row in evidence:
                if not isinstance(row, dict):
                    raise ValueError("evidence row must be an object")
                symbol = str(row["symbol"])
                mark = float(row["mark"])
                if (
                    not symbol
                    or symbol in evidence_marks
                    or not math.isfinite(mark)
                    or mark <= 0.0
                    or _as_utc(str(row["as_of_ts"])) != observation_ts
                ):
                    raise ValueError("evidence mark identity is invalid")
                evidence_marks[symbol] = mark
            if any(marks.get(symbol) != mark for symbol, mark in evidence_marks.items()):
                raise ValueError("scoring marks conflict with same-cycle evidence marks")
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid committed scoring marks: {path}") from exc
        observations.append((observation_cycle, observation_ts, marks, artifact_sha256))
    observations.sort(key=lambda item: (item[1], item[0]))
    return observations


def canonical_daily_score_observation(
    state_dir,
    origin_cycle: int,
    *,
    cadence: str = "rebal",
) -> tuple[int, datetime, dict[str, float], str] | None:
    """Resolve the one admissible daily label without trusting a scorecard row.

    The benchmark, origin timestamp, required coverage, horizon and first eligible observation are
    all derived from committed desk artifacts. A row therefore cannot select another benchmark,
    omit a losing symbol, or move its label to a more convenient later cycle.
    """
    if not learning_origin_is_bound(state_dir, origin_cycle, cadence=cadence):
        return None
    origin_dir = cycle_dir(state_dir, origin_cycle, cadence=cadence)
    try:
        evidence = _read_strict_json(origin_dir / "evidence.json")
        report = _read_strict_json_object(origin_dir / "report.json")
        if type(report.get("cycle")) is not int or report["cycle"] != origin_cycle:
            return None
        origin_ts = _as_utc(str(report["decision_ts"]))
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if not isinstance(evidence, list) or not evidence:
        return None
    symbols: list[str] = []
    for row in evidence:
        if not isinstance(row, dict):
            return None
        symbol = str(row.get("symbol") or "")
        try:
            mark = float(row.get("mark"))
        except (TypeError, ValueError):
            return None
        try:
            evidence_ts = _as_utc(str(row["as_of_ts"]))
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not symbol
            or not math.isfinite(mark)
            or mark <= 0.0
            or evidence_ts != origin_ts
        ):
            return None
        symbols.append(symbol)
    if len(symbols) != len(set(symbols)) or CANONICAL_BTC_SYMBOL not in symbols:
        return None
    earliest_ts = (
        origin_ts
        + timedelta(hours=DAILY_LEARNING_HORIZON_HOURS)
        - SCHEDULED_MARK_TOLERANCE
    )
    required = {*symbols, CANONICAL_BTC_SYMBOL}
    return next(
        (
            observation
            for observation in committed_scoring_observations(state_dir, cadence=cadence)
            if observation[0] > origin_cycle
            and observation[1] >= earliest_ts
            and required.issubset(observation[2])
        ),
        None,
    )


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


def _forecast_round_trip_cost_fields(
    state_dir,
    *,
    origin_cycle: int,
    book: Book,
    leg: BookLeg,
    origin_evidence: dict,
    cadence: str,
) -> dict:
    """Price one standardized ex-ante round trip from immutable origin liquidity.

    This is a measurement label, never a claim that the desk actually entered or exited at these
    costs. Both crossing sides use the same manifest-bound origin L2 snapshot, full target size,
    taker fee, displayed-depth haircut, adverse-selection reserve, and a fixed book-level legging
    reserve. Deriving the reserve from book breadth rather than actual origin turnover keeps held
    forecast renewals comparable with fresh entries.
    """
    precheck_sha256 = completed_artifact_sha256(
        state_dir, origin_cycle, "precheck", cadence=cadence
    )
    risk_model_sha256 = completed_artifact_sha256(
        state_dir, origin_cycle, "risk_model", cadence=cadence
    )
    result = {
        "origin_precheck_sha256": precheck_sha256,
        "origin_risk_model_sha256": risk_model_sha256,
        "round_trip_cost_model": FORECAST_ROUND_TRIP_COST_MODEL,
        "round_trip_legging_reserve_bps": None,
        "round_trip_friction_priced": False,
        "round_trip_entry_friction_usd": None,
        "round_trip_exit_friction_usd": None,
        "round_trip_friction_usd": None,
        "round_trip_friction_frac": None,
    }
    reasons: list[str] = []
    if precheck_sha256 is None:
        reasons.append("origin_precheck_not_manifest_bound")
        return {**result, "round_trip_cost_exclusion_reasons": reasons}
    try:
        origin_dir = cycle_dir(state_dir, origin_cycle, cadence=cadence)
        precheck = _read_strict_json_object(origin_dir / "precheck.json")
        policy_fields = {
            "execution_latency_ms",
            "execution_displayed_depth_fraction",
            "execution_adverse_selection_bps",
            "execution_legging_bps_per_second",
            "execution_allow_partial_fills",
        }
        if precheck.get("execution_policy_applied") is not True or not policy_fields.issubset(
            precheck
        ):
            raise ValueError("origin execution policy is not explicit")
        if not isinstance(precheck["execution_allow_partial_fills"], bool):
            raise ValueError("origin partial-fill policy is not boolean")
        execution = ExecutionRealism(
            latency_ms=float(precheck["execution_latency_ms"]),
            displayed_depth_fraction=float(
                precheck["execution_displayed_depth_fraction"]
            ),
            adverse_selection_bps=float(precheck["execution_adverse_selection_bps"]),
            legging_bps_per_second=float(precheck["execution_legging_bps_per_second"]),
            allow_partial_fills=precheck["execution_allow_partial_fills"],
        )
        legging_reserve_bps = (
            execution.legging_bps_per_second
            * (execution.latency_ms / 1000.0)
            * max(len(book.legs) - 1, 0)
        )
        if not math.isfinite(legging_reserve_bps) or legging_reserve_bps < 0.0:
            raise ValueError("invalid round-trip legging reserve")
        result["round_trip_legging_reserve_bps"] = legging_reserve_bps
    except (KeyError, OSError, TypeError, ValueError):
        reasons.append("origin_execution_policy_unavailable")
        return {**result, "round_trip_cost_exclusion_reasons": reasons}

    required_liquidity = {
        "mark",
        "liquidity_mid",
        "slippage_curve_buy_bps",
        "slippage_curve_sell_bps",
        "depth_usd_ask",
        "depth_usd_bid",
    }
    if not required_liquidity.issubset(origin_evidence):
        reasons.append("origin_two_sided_liquidity_unavailable")
        return {**result, "round_trip_cost_exclusion_reasons": reasons}
    target = float(leg.target_notional)
    try:
        entry = _one_way_friction_usd(
            origin_evidence,
            target,
            _entry_execution_side(leg.side),
            execution_realism=execution,
            legging_reserve_bps=legging_reserve_bps,
        )
        exit_ = _one_way_friction_usd(
            origin_evidence,
            target,
            _exit_execution_side(leg.side),
            execution_realism=execution,
            legging_reserve_bps=legging_reserve_bps,
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        reasons.append("round_trip_cost_calculation_failed")
        return {**result, "round_trip_cost_exclusion_reasons": reasons}

    def full_side_priced(values: tuple[float, float, float, float | None]) -> bool:
        friction, executable_clip, curve_lookup_clip, fill_fraction = values
        return bool(
            all(math.isfinite(value) and value >= 0.0 for value in values[:3])
            and executable_clip > 0.0
            and curve_lookup_clip > 0.0
            and fill_fraction is not None
            and math.isfinite(fill_fraction)
            and fill_fraction >= 1.0 - 1e-12
        )

    if not full_side_priced(entry):
        reasons.append("entry_full_fill_cost_unavailable")
    if not full_side_priced(exit_):
        reasons.append("exit_full_fill_cost_unavailable")
    if reasons:
        return {**result, "round_trip_cost_exclusion_reasons": reasons}

    entry_friction = float(entry[0])
    exit_friction = float(exit_[0])
    total_friction = entry_friction + exit_friction
    return {
        **result,
        "round_trip_friction_priced": True,
        "round_trip_entry_friction_usd": entry_friction,
        "round_trip_exit_friction_usd": exit_friction,
        "round_trip_friction_usd": total_friction,
        "round_trip_friction_frac": total_friction / target,
        "round_trip_cost_exclusion_reasons": [],
    }


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
    cohort_membership: dict = {}
    if policy_version >= 5:
        cohort_membership = _forecast_cohort_membership(
            book,
            origin_cycle=origin_cycle,
            forecast_horizon_hours=int(leg.edge_horizon_hours),
            btc_symbol=btc_symbol,
        )
        required_marks = {
            *cohort_membership["forecast_cohort_expected_symbols"],
            btc_symbol,
        }
        eligible = next(
            (
                observation
                for observation in committed_scoring_observations(
                    state_dir, cadence=cadence
                )
                if observation[0] > origin_cycle
                and observation[1] >= earliest_eligible_ts
                and _marks_cover_positive_finite(observation[2], required_marks)
            ),
            None,
        )
    else:
        # Preserve the exact v1-v4 per-leg observation contract for immutable replay.
        eligible = next(
            (
                observation
                for observation in committed_scoring_observations(
                    state_dir, cadence=cadence
                )
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
    residual_vols = (
        (
            risk_model.get("residual_vol_ewma_shrunk_annualized")
            or risk_model.get("residual_vol_annualized")
            or {}
        )
        if policy_version >= 5
        else (risk_model.get("residual_vol_annualized") or {})
    )
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
    row = {
        **legacy_row,
        "forecast_score_schema_version": policy_version,
        "label_policy": (
            "scheduled_horizon_with_nonoverlapping_renewal_cohorts"
            if policy_version <= 3
            else "scheduled_horizon_with_leg_nonoverlap_and_time_cohort_calibration"
            if policy_version == 4
            else (
                "scheduled_horizon_with_leg_nonoverlap_time_cohorts_and_"
                "origin_round_trip_cost"
            )
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
    if policy_version < 5:
        return row
    cost_fields = _forecast_round_trip_cost_fields(
        state_dir,
        origin_cycle=origin_cycle,
        book=book,
        leg=leg,
        origin_evidence=origin_row or {},
        cadence=cadence,
    )
    friction_frac = cost_fields["round_trip_friction_frac"]
    cost_net_edge = selected_edge - float(friction_frac) if friction_frac is not None else None
    cost_net_eligible = bool(
        learning_eligible
        and cost_fields["round_trip_friction_priced"] is True
        and cost_net_edge is not None
    )
    cost_net_exclusions = list(exclusion_reasons)
    cost_net_exclusions.extend(cost_fields["round_trip_cost_exclusion_reasons"])
    return {
        **row,
        **cohort_membership,
        **cost_fields,
        "realized_round_trip_cost_net_price_edge_frac": cost_net_edge,
        "cost_net_learning_eligible": cost_net_eligible,
        "cost_net_learning_exclusion_reasons": cost_net_exclusions,
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
    """Strictly rederive a current score from its canonical committed observation."""
    if (
        record.outcome_provenance != "manifest_bound"
        or record.score_schema_version != CURRENT_SCORE_SCHEMA_VERSION
        or record.btc_symbol != CANONICAL_BTC_SYMBOL
    ):
        return False

    # Missing serialized fields must not silently acquire model defaults. Extra keys are rejected
    # by the score models themselves; these exact field-set checks cover top-level and nested
    # deletions before full-value replay.
    if set(record.model_fields_set) != set(type(record).model_fields):
        return False
    if set(record.book.model_fields_set) != set(type(record.book).model_fields):
        return False
    if set(record.specialists) != set(SPECIALIST_ROLES) or any(
        set(item.model_fields_set) != set(type(item).model_fields)
        for item in record.specialists.values()
    ):
        return False
    if any(
        set(item.model_fields_set) != set(type(item).model_fields)
        for item in record.candidate_opportunities
    ):
        return False

    try:
        observation = canonical_daily_score_observation(
            state_dir, record.cycle, cadence=cadence
        )
        if observation is None:
            return False
        observation_cycle, observation_ts, marks, artifact_sha256 = observation
        if not (
            observation_cycle == record.outcome_observation_cycle
            and artifact_sha256 == record.outcome_scoring_marks_sha256
            and _marks_sha256(marks) == record.outcome_marks_sha256
            and observation_ts == _as_utc(record.scored_at)
        ):
            return False
        expected = _build_score_record(
            state_dir,
            scored_cycle=record.cycle,
            cur_marks=marks,
            now=observation_ts.isoformat(),
            btc_symbol=CANONICAL_BTC_SYMBOL,
            cadence=cadence,
            outcome_observation_cycle=observation_cycle,
            outcome_scoring_marks_sha256=artifact_sha256,
            outcome_provenance="manifest_bound",
        )
        return expected.model_dump(mode="json") == record.model_dump(mode="json")
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


_LEGACY_PM_GATE_DECLARATION_CYCLES = frozenset({51, 52, 53})


def _legacy_pm_gate_declaration(book: Book, *, cycle: int) -> dict | None:
    """Bind explicit pre-candidate-schema PM prose without inventing candidate rows.

    Only artifacts that truly omitted ``candidate_reviews`` qualify. New production output cannot
    bypass the structured contract by emitting an empty list plus prose. The small marker set is a
    versioned adapter exclusively for the desk's already-committed c51-c53 wording; it recognizes
    only an explicit PM claim that the active managed entry policy caused the no-entry result.
    """
    if cycle not in _LEGACY_PM_GATE_DECLARATION_CYCLES or (
        "candidate_reviews" in book.model_fields_set
    ):
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
        "adapter_version": 2,
        "source_cycle": cycle,
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
            causal_evidence = _legacy_pm_gate_declaration(book, cycle=cycle)
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


def _recurrence_payload(
    state_dir,
    memory_dir,
    records: list[ScoreRecord],
    *,
    through_cycle: int,
    decision_cycle: int,
    cadence: str,
    k: int,
    window: int,
    active_calibration_roles: set[str] | None,
) -> list[dict]:
    """Build feedback using separate outcome and decision clocks.

    Score-derived signals depend on the immutable score rows. The PM gate signal is state-only, so
    its trailing window ends at the latest completed prior cycle even when that cycle has no mature
    outcome yet. Cooldown is measured at the current decision that may consume the feedback, not
    at whichever older origin happened to produce the newest score.
    """
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
            through_cycle=through_cycle,
            cadence=cadence,
            k=k,
        )
    )
    recurrences = _filter_recurrences(
        detected,
        memory_dir=memory_dir,
        # Preserve this private helper's established keyword while supplying the decision clock.
        scored_cycle=decision_cycle,
        active_calibration_roles=active_calibration_roles,
    )
    return [recurrence.model_dump(mode="json") for recurrence in recurrences]


def refresh_recurrences(
    state_dir,
    memory_dir,
    *,
    through_cycle: int,
    decision_cycle: int,
    cadence: str = "rebal",
    k: int = 3,
    window: int = 6,
    active_calibration_roles: set[str] | None = None,
) -> list[dict]:
    """Rebuild the current packet without manufacturing a score observation."""
    if through_cycle < 0 or decision_cycle < 0:
        raise ValueError("recurrence clocks must be non-negative")
    pending = _resolve_pending_dir(memory_dir)
    pending.mkdir(parents=True, exist_ok=True)
    # Consume once even when no score origin matures in this decision cycle.
    (pending / "reflection.json").unlink(missing_ok=True)
    records = _read_scorecard(
        Path(memory_dir) / "scorecard.jsonl",
        state_dir=state_dir,
        cadence=cadence,
    )
    payload = _recurrence_payload(
        state_dir,
        memory_dir,
        records,
        through_cycle=through_cycle,
        decision_cycle=decision_cycle,
        cadence=cadence,
        k=k,
        window=window,
        active_calibration_roles=active_calibration_roles,
    )
    (pending / "recurrences.json").write_text(json.dumps(payload, indent=2))
    return payload


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
    if btc_symbol != CANONICAL_BTC_SYMBOL:
        raise ValueError(f"daily score benchmark must be {CANONICAL_BTC_SYMBOL}")
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
        score_schema_version=CURRENT_SCORE_SCHEMA_VERSION,
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
    recurrence_through_cycle: int | None = None,
    recurrence_decision_cycle: int | None = None,
) -> dict:
    if btc_symbol != CANONICAL_BTC_SYMBOL:
        raise ValueError(f"daily score benchmark must be {CANONICAL_BTC_SYMBOL}")
    recurrence_through = (
        scored_cycle if recurrence_through_cycle is None else int(recurrence_through_cycle)
    )
    recurrence_decision = (
        scored_cycle if recurrence_decision_cycle is None else int(recurrence_decision_cycle)
    )
    if recurrence_through < 0 or recurrence_decision < 0:
        raise ValueError("recurrence clocks must be non-negative")
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
        eligible = canonical_daily_score_observation(
            state_dir, scored_cycle, cadence=cadence
        )
        if eligible is None:
            raise ValueError("score origin has no canonical manifest-bound scoring observation")
        observation_cycle, observation_ts, observation_marks, artifact_sha256 = eligible
        if not (
            observation_cycle == outcome_observation_cycle
            and artifact_sha256 == outcome_scoring_marks_sha256
            and observation_ts == _as_utc(now)
            and _marks_sha256(observation_marks) == _marks_sha256(cur_marks)
        ):
            raise ValueError("score outcome is not the canonical committed scoring observation")
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
            attribution = parse_score_record_json(attribution_path.read_text())
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
        attribution = parse_score_record_json(attribution_path.read_text())
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
    payload = _recurrence_payload(
        state_dir,
        memory_dir,
        records,
        through_cycle=recurrence_through,
        decision_cycle=recurrence_decision,
        cadence=cadence,
        k=k,
        window=window,
        active_calibration_roles=active_calibration_roles,
    )
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
REFLECTION_APPLY_TRANSACTION_SUFFIX = "-apply-transaction.json"
REFLECTION_APPLY_TRANSACTION_SCHEMA_VERSION = 1
REFLECTION_CONSUMPTION_SCHEMA_VERSION = 2
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


def reflection_apply_transaction_path(state_dir, source_cycle: int) -> Path:
    """Return the durable write-ahead intent for one managed-prompt apply."""
    return (
        Path(state_dir)
        / REFLECTION_AUTHORITY_DIR
        / f"cycle-{int(source_cycle)}{REFLECTION_APPLY_TRANSACTION_SUFFIX}"
    )


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
    proposal: dict,
    agents_dir=None,
    anchor_path=None,
    apply_transaction_sha256: str | None = None,
) -> dict:
    """Bind one authenticated proposal to the exact verified post-apply desk state."""
    if outcome not in {"head_applied", "no_head_change"}:
        raise ValueError("invalid reflection authority consumption outcome")
    authority = validate_reflection_authority(
        state_dir,
        source_cycle=source_cycle,
        recurrences_sha256=recurrences_sha256,
    )
    memory_dir = Path(memory_dir)
    state_dir = Path(state_dir)
    heads_path = memory_dir / REFLECTOR_HEADS_NAME
    journal_path = memory_dir / "reflector-journal.md"
    anchor_path = (
        Path(anchor_path)
        if anchor_path is not None
        else reflector_head_anchor_path(state_dir)
    )
    agents_dir = _resolve_recovery_agents_dir(memory_dir, agents_dir)
    canonical_proposal, proposal_sha256, omitted_roles = _canonical_consumption_proposal(
        proposal, authority
    )
    has_edits = bool(canonical_proposal["edits"])
    if (outcome == "head_applied") is not has_edits:
        raise ValueError("reflection consumption outcome conflicts with its canonical proposal")
    _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
    journal = journal_path.read_bytes() if journal_path.exists() else b""
    _validate_consumption_head_events(
        journal,
        source_cycle=source_cycle,
        proposal=canonical_proposal,
        proposal_sha256=proposal_sha256,
        outcome=outcome,
    )
    transaction_path = reflection_apply_transaction_path(state_dir, source_cycle)
    if outcome == "head_applied":
        transaction = _load_reflection_apply_transaction(
            transaction_path,
            state_dir=state_dir,
            memory_dir=memory_dir,
            agents_dir=agents_dir,
            anchor_path=anchor_path,
        )
        transaction_digest = transaction["transaction_sha256"]
        if apply_transaction_sha256 is not None and (
            apply_transaction_sha256 != transaction_digest
        ):
            raise ValueError("reflection consumption names a different apply transaction")
        if (
            transaction["source_cycle"] != source_cycle
            or transaction["proposal_sha256"] != proposal_sha256
            or transaction["recurrences_sha256"] != recurrences_sha256
        ):
            raise ValueError("reflection apply transaction conflicts with consumption")
        apply_transaction_sha256 = transaction_digest
    elif apply_transaction_sha256 is not None:
        raise ValueError("no-head-change consumption cannot name an apply transaction")
    prompt_hashes = {
        role: _sha256_bytes((agents_dir / f"{role}.md").read_bytes()) for role in _ALL_ROLES
    }
    payload = {
        "authority_receipt_sha256": authority["receipt_sha256"],
        "apply_transaction_sha256": apply_transaction_sha256,
        "no_action_reason": canonical_proposal["no_action_reason"],
        "omitted_roles": omitted_roles,
        "outcome": outcome,
        "post_anchor_file_sha256": _sha256_bytes(anchor_path.read_bytes()),
        "post_heads_file_sha256": _sha256_bytes(heads_path.read_bytes()),
        "post_journal_sha256": _sha256_bytes(journal),
        "post_prompt_file_sha256": prompt_hashes,
        "proposal": canonical_proposal,
        "proposal_sha256": proposal_sha256,
        "schema_version": REFLECTION_CONSUMPTION_SCHEMA_VERSION,
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


def _canonical_consumption_proposal(
    proposal: dict, authority: dict
) -> tuple[dict, str, list[str]]:
    validated = ReflectionProposal.model_validate(proposal)
    validate_unique_reflection_edit_roles(validated.edits)
    canonical = validated.model_dump(mode="json")
    edited_roles = {edit["role"] for edit in canonical["edits"]}
    surfaced_roles = {recurrence["role"] for recurrence in authority["recurrences"]}
    if not edited_roles.issubset(surfaced_roles):
        raise ValueError("canonical reflection proposal edits an unauthorized role")
    omitted_roles = sorted(surfaced_roles - edited_roles)
    reason = canonical["no_action_reason"]
    if (not canonical["edits"] or omitted_roles) and not reason.strip():
        raise ValueError("reflection no-action/omitted-role reason must be nonblank")
    return canonical, _sha256_bytes(_canonical_json_bytes(canonical)), omitted_roles


def _validate_consumption_head_events(
    journal: bytes,
    *,
    source_cycle: int,
    proposal: dict,
    proposal_sha256: str,
    outcome: str,
) -> None:
    events: list[dict] = []
    for line in journal.decode().splitlines():
        if not line.startswith("- head_event_v1: "):
            continue
        try:
            event = json.loads(line.removeprefix("- head_event_v1: "))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid reflector head event while binding consumption") from exc
        if int(event.get("source_cycle", -1)) == source_cycle:
            events.append(event)
    expected_roles = sorted(edit["role"] for edit in proposal["edits"])
    event_roles = sorted(str(event.get("role", "")) for event in events)
    if outcome == "head_applied":
        if event_roles != expected_roles or any(
            event.get("proposal_sha256") != proposal_sha256 for event in events
        ):
            raise ValueError("head_applied consumption does not match its head-event proposal")
    elif events:
        raise ValueError("no_head_change consumption conflicts with an applied head event")


def _resolve_recovery_agents_dir(memory_dir: Path, agents_dir) -> Path:
    if agents_dir is not None:
        return Path(agents_dir)
    candidates = (memory_dir.parent / "agents", memory_dir / "agents")
    for candidate in candidates:
        if all((candidate / f"{role}.md").exists() for role in _ALL_ROLES):
            return candidate
    raise ValueError("agents_dir is required to validate proposal-bound reflection consumption")


def _load_reflection_authority_consumption_record(
    state_dir: Path,
    memory_dir: Path,
    source_cycle: int,
) -> tuple[dict, dict]:
    """Authenticate a consumption independently of whether a later head superseded its files."""
    authority_path = reflection_authority_path(state_dir, source_cycle)
    try:
        authority_raw = json.loads(authority_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("consumption has no valid reflection authority") from exc
    authority = validate_reflection_authority(
        state_dir,
        source_cycle=source_cycle,
        recurrences_sha256=str(authority_raw.get("recurrences_sha256", "")),
    )
    consumption_path = reflection_authority_consumption_path(state_dir, source_cycle)
    try:
        consumption = json.loads(consumption_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid reflection authority consumption") from exc
    payload = {key: value for key, value in consumption.items() if key != "consumption_sha256"}
    legacy_fields = {
        "authority_receipt_sha256",
        "consumption_sha256",
        "outcome",
        "post_heads_file_sha256",
        "post_journal_sha256",
        "schema_version",
        "source_cycle",
    }
    proposal_bound_fields = {
        "authority_receipt_sha256",
        "apply_transaction_sha256",
        "consumption_sha256",
        "no_action_reason",
        "omitted_roles",
        "outcome",
        "post_anchor_file_sha256",
        "post_heads_file_sha256",
        "post_journal_sha256",
        "post_prompt_file_sha256",
        "proposal",
        "proposal_sha256",
        "schema_version",
        "source_cycle",
    }
    common_valid = bool(
        consumption.get("authority_receipt_sha256") == authority["receipt_sha256"]
        and consumption.get("source_cycle") == source_cycle
        and consumption.get("outcome") in {"head_applied", "no_head_change"}
        and _is_sha256(consumption.get("consumption_sha256"))
        and consumption["consumption_sha256"] == _sha256_bytes(_canonical_json_bytes(payload))
        and _is_sha256(consumption.get("post_heads_file_sha256"))
        and _is_sha256(consumption.get("post_journal_sha256"))
    )
    if set(consumption) == legacy_fields:
        if consumption.get("schema_version") != REFLECTOR_HEADS_SCHEMA_VERSION or not common_valid:
            raise ValueError("legacy reflection authority consumption mismatch")
        return authority, consumption
    if set(consumption) != proposal_bound_fields or not common_valid:
        raise ValueError("proposal-bound reflection authority consumption mismatch")
    canonical_proposal, proposal_sha256, omitted_roles = _canonical_consumption_proposal(
        consumption["proposal"], authority
    )
    prompt_hashes = consumption.get("post_prompt_file_sha256")
    outcome = consumption["outcome"]
    if (
        consumption.get("schema_version") != REFLECTION_CONSUMPTION_SCHEMA_VERSION
        or consumption.get("proposal") != canonical_proposal
        or consumption.get("proposal_sha256") != proposal_sha256
        or consumption.get("no_action_reason") != canonical_proposal["no_action_reason"]
        or consumption.get("omitted_roles") != omitted_roles
        or not _is_sha256(consumption.get("post_anchor_file_sha256"))
        or not isinstance(prompt_hashes, dict)
        or set(prompt_hashes) != set(_ALL_ROLES)
        or any(not _is_sha256(value) for value in prompt_hashes.values())
        or (outcome == "head_applied") is not bool(canonical_proposal["edits"])
        or (
            outcome == "head_applied"
            and not _is_sha256(consumption.get("apply_transaction_sha256"))
        )
        or (
            outcome == "no_head_change"
            and consumption.get("apply_transaction_sha256") is not None
        )
    ):
        raise ValueError("proposal-bound reflection authority consumption mismatch")
    journal_path = memory_dir / "reflector-journal.md"
    journal = journal_path.read_bytes() if journal_path.exists() else b""
    _validate_consumption_head_events(
        journal,
        source_cycle=source_cycle,
        proposal=canonical_proposal,
        proposal_sha256=proposal_sha256,
        outcome=outcome,
    )
    return authority, consumption


def reflection_consumption_replay_status(
    state_dir,
    memory_dir,
    *,
    source_cycle: int,
    recurrences_sha256: str,
    proposal: dict,
) -> dict | None:
    """Recognize an exact replay of an already-consumed proposal without writing.

    A no-head-change decision has no journal head event, so journal monotonicity alone cannot make
    its authority single-use. Authenticate the immutable consumption before an apply can publish
    a transaction. Schema-v2 consumptions retain the canonical proposal and can be replayed
    idempotently; legacy receipts deliberately cannot authorize another attempt because they did
    not retain enough information to prove proposal equality.
    """
    state_dir = Path(state_dir)
    memory_dir = Path(memory_dir)
    path = reflection_authority_consumption_path(state_dir, source_cycle)
    if not path.exists():
        return None
    authority, consumption = _load_reflection_authority_consumption_record(
        state_dir, memory_dir, source_cycle
    )
    if authority["recurrences_sha256"] != recurrences_sha256:
        raise ValueError("consumed reflection authority has a different recurrence packet")
    if consumption.get("schema_version") != REFLECTION_CONSUMPTION_SCHEMA_VERSION:
        raise ValueError(
            "reflection authority was already consumed by a legacy receipt; "
            "proposal replay is not provable"
        )
    canonical_proposal, proposal_sha256, _omitted_roles = _canonical_consumption_proposal(
        proposal, authority
    )
    expected_outcome = "head_applied" if canonical_proposal["edits"] else "no_head_change"
    if (
        consumption["proposal"] != canonical_proposal
        or consumption["proposal_sha256"] != proposal_sha256
        or consumption["outcome"] != expected_outcome
    ):
        raise ValueError(
            "reflection authority is already consumed by a different canonical proposal"
        )
    return {
        "outcome": expected_outcome,
        "proposal_sha256": proposal_sha256,
        "source_cycle": source_cycle,
    }


def read_only_reflection_probe_issues(
    state_dir,
    memory_dir,
    agents_dir,
    *,
    anchor_path=None,
) -> list[str]:
    """Report recovery work without performing it.

    This is the unlocked host health-probe surface. It must never call a recovery helper or a
    durable writer: a pending transaction or consumed-but-unhandled authority is reported as an
    issue for the next lock-owning full preflight to recover.
    """
    state_dir = Path(state_dir)
    memory_dir = Path(memory_dir)
    agents_dir = Path(agents_dir)
    anchor_path = (
        Path(anchor_path)
        if anchor_path is not None
        else reflector_head_anchor_path(state_dir)
    )
    journal_path = memory_dir / "reflector-journal.md"
    issues = audit_managed_region_provenance(
        agents_dir,
        journal_path,
        reflector_heads_path(journal_path),
        anchor_path,
    )
    authority_dir = state_dir / REFLECTION_AUTHORITY_DIR
    for path in sorted(authority_dir.glob(f"cycle-*{REFLECTION_APPLY_TRANSACTION_SUFFIX}")):
        issues.append(f"pending reflection apply recovery: {path.name}")

    handled_path = memory_dir / "recurrence-handled.json"
    try:
        handled = json.loads(handled_path.read_text()) if handled_path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(f"invalid recurrence-handled.json: {exc}")
        handled = {}
    if not isinstance(handled, dict):
        issues.append("invalid recurrence-handled.json structure")
        handled = {}

    for path in sorted(authority_dir.glob("cycle-*-consumed.json")):
        try:
            source_cycle = int(
                path.name.removeprefix("cycle-").removesuffix("-consumed.json")
            )
            authority, _consumption = _load_reflection_authority_consumption_record(
                state_dir, memory_dir, source_cycle
            )
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            issues.append(f"invalid reflection consumption {path.name}: {exc}")
            continue
        missing_keys: list[str] = []
        for recurrence in authority["recurrences"]:
            key = f"{recurrence['kind']}:{recurrence['role']}"
            try:
                handled_cycle = int(handled.get(key, {}).get("cycle", -1))
            except (AttributeError, TypeError, ValueError):
                handled_cycle = -1
            if handled_cycle < source_cycle:
                missing_keys.append(key)
        if missing_keys:
            issues.append(
                f"consumed reflection cycle {source_cycle} has unhandled recurrence(s): "
                + ", ".join(sorted(missing_keys))
            )
    return issues


def reconcile_consumed_reflection_handled(state_dir, memory_dir) -> list[int]:
    """Replay every authenticated immutable consumption into the monotonic cooldown ledger."""
    state_dir = Path(state_dir)
    memory_dir = Path(memory_dir)
    authority_dir = state_dir / REFLECTION_AUTHORITY_DIR
    recovered: list[int] = []
    for path in sorted(authority_dir.glob("cycle-*-consumed.json")):
        name = path.name
        try:
            source_cycle = int(name.removeprefix("cycle-").removesuffix("-consumed.json"))
        except ValueError as exc:
            raise ValueError(f"invalid reflection consumption filename: {name}") from exc
        authority, _consumption = _load_reflection_authority_consumption_record(
            state_dir, memory_dir, source_cycle
        )
        mark_recurrences_handled(memory_dir, authority["recurrences"], cycle=source_cycle)
        recovered.append(source_cycle)
    if recovered:
        fsync_directory(memory_dir)
    return recovered


def reflection_authority_recovery_status(
    state_dir,
    memory_dir,
    source_cycle: int,
    *,
    agents_dir=None,
    anchor_path=None,
) -> dict | None:
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
    state_dir = Path(state_dir)
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
        payload = {
            key: value for key, value in consumption.items() if key != "consumption_sha256"
        }
        legacy_fields = {
            "authority_receipt_sha256",
            "consumption_sha256",
            "outcome",
            "post_heads_file_sha256",
            "post_journal_sha256",
            "schema_version",
            "source_cycle",
        }
        proposal_bound_fields = {
            "authority_receipt_sha256",
            "apply_transaction_sha256",
            "consumption_sha256",
            "no_action_reason",
            "omitted_roles",
            "outcome",
            "post_anchor_file_sha256",
            "post_heads_file_sha256",
            "post_journal_sha256",
            "post_prompt_file_sha256",
            "proposal",
            "proposal_sha256",
            "schema_version",
            "source_cycle",
        }
        common_invalid = (
            consumption.get("authority_receipt_sha256") != receipt["receipt_sha256"]
            or consumption.get("source_cycle") != source_cycle
            or consumption.get("outcome") not in {"head_applied", "no_head_change"}
            or consumption.get("consumption_sha256")
            != _sha256_bytes(_canonical_json_bytes(payload))
            or consumption.get("post_heads_file_sha256") != current_heads_sha
            or consumption.get("post_journal_sha256") != current_journal_sha
        )
        if set(consumption) == legacy_fields:
            if (
                consumption.get("schema_version") != REFLECTOR_HEADS_SCHEMA_VERSION
                or common_invalid
            ):
                raise ValueError("legacy reflection authority consumption mismatch")
            return {"status": "consumed", "receipt": receipt, "consumption": consumption}
        if set(consumption) != proposal_bound_fields:
            raise ValueError("invalid reflection authority consumption structure")
        agents_dir = _resolve_recovery_agents_dir(memory_dir, agents_dir)
        anchor_path = (
            Path(anchor_path)
            if anchor_path is not None
            else reflector_head_anchor_path(state_dir)
        )
        try:
            canonical_proposal, proposal_sha256, omitted_roles = (
                _canonical_consumption_proposal(consumption["proposal"], receipt)
            )
            prompt_hashes = {
                role: _sha256_bytes((agents_dir / f"{role}.md").read_bytes())
                for role in _ALL_ROLES
            }
            _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
            _validate_consumption_head_events(
                current_journal,
                source_cycle=source_cycle,
                proposal=canonical_proposal,
                proposal_sha256=proposal_sha256,
                outcome=str(consumption["outcome"]),
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("proposal-bound reflection consumption poststate is invalid") from exc
        if (
            consumption.get("schema_version") != REFLECTION_CONSUMPTION_SCHEMA_VERSION
            or common_invalid
            or consumption.get("proposal") != canonical_proposal
            or consumption.get("proposal_sha256") != proposal_sha256
            or consumption.get("no_action_reason") != canonical_proposal["no_action_reason"]
            or consumption.get("omitted_roles") != omitted_roles
            or consumption.get("post_anchor_file_sha256")
            != _sha256_bytes(anchor_path.read_bytes())
            or consumption.get("post_prompt_file_sha256") != prompt_hashes
            or (
                consumption.get("outcome") == "head_applied"
                and not _is_sha256(consumption.get("apply_transaction_sha256"))
            )
            or (
                consumption.get("outcome") == "no_head_change"
                and consumption.get("apply_transaction_sha256") is not None
            )
        ):
            raise ValueError("proposal-bound reflection authority consumption mismatch")
        return {"status": "consumed", "receipt": receipt, "consumption": consumption}
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


def _transaction_snapshot_fields(
    key: str,
    snapshot: tuple[bool, bytes, int | None],
    post_content: bytes,
) -> dict:
    pre_exists, pre_content, pre_mode = snapshot
    post_mode = pre_mode if pre_exists and pre_mode is not None else 0o600
    return {
        "key": key,
        "post_bytes_b64": b64encode(post_content).decode("ascii"),
        "post_mode": post_mode,
        "post_sha256": _sha256_bytes(post_content),
        "pre_bytes_b64": b64encode(pre_content).decode("ascii"),
        "pre_exists": pre_exists,
        "pre_mode": pre_mode,
        "pre_sha256": _sha256_bytes(pre_content) if pre_exists else None,
    }


def _seal_reflection_apply_transaction(payload: dict) -> dict:
    return {
        **payload,
        "transaction_sha256": _sha256_bytes(_canonical_json_bytes(payload)),
    }


def _transaction_bytes(transaction: dict) -> bytes:
    return json.dumps(transaction, indent=2, sort_keys=True).encode() + b"\n"


def _transaction_target_paths(
    transaction: dict,
    *,
    memory_dir: Path,
    agents_dir: Path,
    anchor_path: Path,
    journal_path: Path | None = None,
) -> dict[str, Path]:
    journal_path = journal_path if journal_path is not None else memory_dir / "reflector-journal.md"
    targets = {
        "anchor": anchor_path,
        "heads": reflector_heads_path(journal_path),
        "journal": journal_path,
    }
    for edit in transaction["proposal"]["edits"]:
        role = edit["role"]
        targets[f"prompt:{role}"] = agents_dir / f"{role}.md"
    return targets


def _decode_transaction_bytes(value: object, *, field: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"reflection apply transaction {field} is not base64 text")
    try:
        return b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001 - normalize malformed base64 to a closed validation
        raise ValueError(f"reflection apply transaction {field} is invalid base64") from exc


def _load_reflection_apply_transaction(
    path: Path,
    *,
    state_dir: Path,
    memory_dir: Path,
    agents_dir: Path,
    anchor_path: Path,
    journal_path: Path | None = None,
) -> dict:
    try:
        transaction = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"missing or invalid reflection apply transaction: {path}") from exc
    fields = {
        "authority_receipt_sha256",
        "files",
        "proposal",
        "proposal_sha256",
        "recurrences_sha256",
        "schema_version",
        "source_cycle",
        "transaction_sha256",
    }
    if not isinstance(transaction, dict) or set(transaction) != fields:
        raise ValueError("invalid reflection apply transaction structure")
    payload = {key: value for key, value in transaction.items() if key != "transaction_sha256"}
    source_cycle = transaction.get("source_cycle")
    if (
        transaction.get("schema_version") != REFLECTION_APPLY_TRANSACTION_SCHEMA_VERSION
        or not isinstance(source_cycle, int)
        or isinstance(source_cycle, bool)
        or source_cycle < 1
        or path != reflection_apply_transaction_path(state_dir, source_cycle)
        or not _is_sha256(transaction.get("transaction_sha256"))
        or transaction["transaction_sha256"] != _sha256_bytes(_canonical_json_bytes(payload))
    ):
        raise ValueError("reflection apply transaction self-digest mismatch")
    authority = validate_reflection_authority(
        state_dir,
        source_cycle=source_cycle,
        recurrences_sha256=str(transaction.get("recurrences_sha256", "")),
    )
    canonical_proposal, proposal_sha256, _omitted = _canonical_consumption_proposal(
        transaction.get("proposal"), authority
    )
    if (
        not canonical_proposal["edits"]
        or transaction["proposal"] != canonical_proposal
        or transaction.get("proposal_sha256") != proposal_sha256
        or transaction.get("authority_receipt_sha256") != authority["receipt_sha256"]
    ):
        raise ValueError("reflection apply transaction authority/proposal mismatch")
    targets = _transaction_target_paths(
        transaction,
        memory_dir=memory_dir,
        agents_dir=agents_dir,
        anchor_path=anchor_path,
        journal_path=journal_path,
    )
    records = transaction.get("files")
    if not isinstance(records, list) or [record.get("key") for record in records] != sorted(
        targets
    ):
        raise ValueError("reflection apply transaction target set/order mismatch")
    record_fields = {
        "key",
        "post_bytes_b64",
        "post_mode",
        "post_sha256",
        "pre_bytes_b64",
        "pre_exists",
        "pre_mode",
        "pre_sha256",
    }
    for record in records:
        if not isinstance(record, dict) or set(record) != record_fields:
            raise ValueError("invalid reflection apply transaction file record")
        pre = _decode_transaction_bytes(record["pre_bytes_b64"], field="pre_bytes_b64")
        post = _decode_transaction_bytes(record["post_bytes_b64"], field="post_bytes_b64")
        pre_exists = record["pre_exists"]
        pre_mode = record["pre_mode"]
        post_mode = record["post_mode"]
        if (
            not isinstance(pre_exists, bool)
            or (pre_exists and (not isinstance(pre_mode, int) or not 0 <= pre_mode <= 0o7777))
            or (
                not pre_exists
                and (pre_mode is not None or pre or record["pre_sha256"] is not None)
            )
            or not isinstance(post_mode, int)
            or not 0 <= post_mode <= 0o7777
            or not _is_sha256(record["post_sha256"])
            or record["post_sha256"] != _sha256_bytes(post)
            or (
                pre_exists
                and (
                    not _is_sha256(record["pre_sha256"])
                    or record["pre_sha256"] != _sha256_bytes(pre)
                )
            )
        ):
            raise ValueError("reflection apply transaction file record mismatch")
    return transaction


def _transaction_record_bytes(record: dict, state: str) -> bytes:
    return _decode_transaction_bytes(record[f"{state}_bytes_b64"], field=f"{state}_bytes_b64")


def _transaction_state_matches(path: Path, record: dict, state: str) -> bool:
    if state == "pre" and not record["pre_exists"]:
        return not path.exists()
    if not path.exists():
        return False
    content = path.read_bytes()
    return (
        _sha256_bytes(content) == record[f"{state}_sha256"]
        and content == _transaction_record_bytes(record, state)
        and stat.S_IMODE(path.stat().st_mode) == record[f"{state}_mode"]
    )


def _restore_reflection_transaction_prestate(
    transaction: dict,
    *,
    memory_dir: Path,
    agents_dir: Path,
    anchor_path: Path,
    journal_path: Path | None = None,
) -> None:
    targets = _transaction_target_paths(
        transaction,
        memory_dir=memory_dir,
        agents_dir=agents_dir,
        anchor_path=anchor_path,
        journal_path=journal_path,
    )
    records = {record["key"]: record for record in transaction["files"]}
    divergent = [
        key
        for key, path in sorted(targets.items())
        if not _transaction_state_matches(path, records[key], "pre")
        and not _transaction_state_matches(path, records[key], "post")
    ]
    if divergent:
        raise ValueError(
            "reflection apply transaction has divergent target(s); refusing rollback: "
            + ", ".join(divergent)
        )
    for key in sorted(targets):
        path = targets[key]
        record = records[key]
        if _transaction_state_matches(path, record, "pre"):
            continue
        if record["pre_exists"]:
            durable_write_bytes(
                path,
                _transaction_record_bytes(record, "pre"),
                mode=int(record["pre_mode"]),
            )
        else:
            durable_unlink(path)
    if not all(
        _transaction_state_matches(path, records[key], "pre")
        for key, path in targets.items()
    ):
        raise ValueError("reflection apply transaction prestate restoration failed")


def _validate_reflection_transaction_poststate(
    transaction: dict,
    *,
    state_dir: Path,
    memory_dir: Path,
    agents_dir: Path,
    anchor_path: Path,
    journal_path: Path | None = None,
) -> bool:
    journal_path = journal_path if journal_path is not None else memory_dir / "reflector-journal.md"
    heads_path = reflector_heads_path(journal_path)
    targets = _transaction_target_paths(
        transaction,
        memory_dir=memory_dir,
        agents_dir=agents_dir,
        anchor_path=anchor_path,
        journal_path=journal_path,
    )
    records = {record["key"]: record for record in transaction["files"]}
    if not all(
        _transaction_state_matches(path, records[key], "post")
        for key, path in targets.items()
    ):
        return False
    try:
        _load_reflector_heads(
            agents_dir,
            journal_path,
            heads_path,
            anchor_path,
        )
        _validate_consumption_head_events(
            journal_path.read_bytes(),
            source_cycle=transaction["source_cycle"],
            proposal=transaction["proposal"],
            proposal_sha256=transaction["proposal_sha256"],
            outcome="head_applied",
        )
    except (OSError, TypeError, ValueError):
        return False
    return True


def _publish_reflection_apply_transaction(path: Path, transaction: dict) -> None:
    encoded = _transaction_bytes(transaction)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError(f"conflicting reflection apply transaction: {path}")
        return
    durable_write_bytes(path, encoded, mode=0o400)


def recover_reflection_apply_transactions(
    state_dir,
    memory_dir,
    agents_dir=None,
    *,
    source_cycle: int | None = None,
    anchor_path=None,
) -> list[dict]:
    """Recover every interrupted managed-prompt apply before any head audit or scoring.

    An immutable consumption forbids rollback: its complete poststate must validate. Without a
    consumption, a completely valid poststate is forward-completed; every other state is restored
    byte-for-byte to the intent's prestate. The intent is removed only after consumption/handled
    finalization or after verified rollback, so each operation is idempotent across repeated loss.
    """
    state_dir = Path(state_dir)
    memory_dir = Path(memory_dir)
    agents_dir = _resolve_recovery_agents_dir(memory_dir, agents_dir)
    anchor_path = (
        Path(anchor_path)
        if anchor_path is not None
        else reflector_head_anchor_path(state_dir)
    )
    if source_cycle is None:
        authority_dir = state_dir / REFLECTION_AUTHORITY_DIR
        paths = sorted(authority_dir.glob(f"cycle-*{REFLECTION_APPLY_TRANSACTION_SUFFIX}"))
    else:
        candidate = reflection_apply_transaction_path(state_dir, source_cycle)
        paths = [candidate] if candidate.exists() else []
    recovered: list[dict] = []
    for path in paths:
        transaction = _load_reflection_apply_transaction(
            path,
            state_dir=state_dir,
            memory_dir=memory_dir,
            agents_dir=agents_dir,
            anchor_path=anchor_path,
        )
        cycle = int(transaction["source_cycle"])
        consumption_path = reflection_authority_consumption_path(state_dir, cycle)
        post_valid = _validate_reflection_transaction_poststate(
            transaction,
            state_dir=state_dir,
            memory_dir=memory_dir,
            agents_dir=agents_dir,
            anchor_path=anchor_path,
        )
        if consumption_path.exists():
            if not post_valid:
                raise ValueError(
                    "consumed reflection apply transaction lacks its complete valid poststate"
                )
            status = reflection_authority_recovery_status(
                state_dir,
                memory_dir,
                cycle,
                agents_dir=agents_dir,
                anchor_path=anchor_path,
            )
            if status is None or status["status"] != "consumed":
                raise ValueError("reflection apply consumption could not be authenticated")
            if (
                status.get("consumption", {}).get("apply_transaction_sha256")
                != transaction["transaction_sha256"]
            ):
                raise ValueError("reflection consumption names a different durable transaction")
            outcome = "consumed_poststate_finalized"
        elif post_valid:
            write_reflection_authority_consumption(
                state_dir,
                memory_dir,
                source_cycle=cycle,
                recurrences_sha256=transaction["recurrences_sha256"],
                outcome="head_applied",
                proposal=transaction["proposal"],
                agents_dir=agents_dir,
                anchor_path=anchor_path,
                apply_transaction_sha256=transaction["transaction_sha256"],
            )
            outcome = "complete_poststate_forward_completed"
        else:
            _restore_reflection_transaction_prestate(
                transaction,
                memory_dir=memory_dir,
                agents_dir=agents_dir,
                anchor_path=anchor_path,
            )
            durable_unlink(path)
            recovered.append({"source_cycle": cycle, "outcome": "partial_prestate_restored"})
            continue
        authority = validate_reflection_authority(
            state_dir,
            source_cycle=cycle,
            recurrences_sha256=transaction["recurrences_sha256"],
        )
        mark_recurrences_handled(memory_dir, authority["recurrences"], cycle=cycle)
        fsync_directory(memory_dir)
        durable_unlink(path)
        recovered.append({"source_cycle": cycle, "outcome": outcome})
    reconcile_consumed_reflection_handled(state_dir, memory_dir)
    return recovered


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
    edit: dict,
    cycles: set[int],
    current_cycle: int | None = None,
    *,
    authorized_state_evidence: set[str] | None = None,
) -> str | None:
    """Deterministic evidence-integrity guard (2026-07 review: the Reflector cited per-cycle
    scores for cycles that had NO ScoreRecord — fabricated evidence tuned live decision prompts).

    The fabrication mode is a claim about a PAST cycle's measured score that doesn't exist. A
    reference to the CURRENT cycle (the note's "[cN]" date tag) or a FUTURE cycle (a `retire_if:
    ... by cM` target) legitimately has no scorecard record yet — those are not past claims. So
    only a cited cycle STRICTLY BEFORE `current_cycle` must normally exist in scorecard.jsonl.

    The one narrow exception is an evidence-list row copied exactly from a sealed, same-role
    state-only recurrence supplied by the caller. This lets `pm_gate_inactive` cite the newest
    completed cycle before its forward score matures without granting a cycle-wide exemption.
    Region text, reason, and retire_if never receive that exception, and paraphrased evidence does
    not receive it. When `current_cycle` is None, every cited cycle is past (strict legacy
    behaviour). Returns a refusal reason, or None when clean."""
    import re

    def past_citations(text: str) -> set[int]:
        cited = {
            int(match)
            for match in re.findall(
                r"\bc(?:ycle\s*)?(\d{1,4})\b", text, flags=re.IGNORECASE
            )
        }
        if current_cycle is not None:
            cited = {cycle for cycle in cited if cycle < current_cycle}
        return cited

    ordinary_text = " ".join(
        str(edit.get(field, "")) for field in ("region_text", "reason", "retire_if")
    )
    missing = {cycle for cycle in past_citations(ordinary_text) if cycle not in cycles}
    authorized_rows = authorized_state_evidence or set()
    for raw_row in edit.get("evidence", []):
        row = str(raw_row)
        row_missing = {cycle for cycle in past_citations(row) if cycle not in cycles}
        if row_missing and row not in authorized_rows:
            missing.update(row_missing)
    missing = sorted(missing)
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
    recurrence for — a hallucinated edit to an unmentioned role is skipped. Duplicate role edits
    invalidate the whole proposal. `known_cycles` (when not None) enables the evidence-integrity
    guard: an edit citing a PAST cycle with no ScoreRecord is refused (fabricated evidence).
    `current_cycle` scopes that guard to past-score claims only (the note's own [cN] tag and a
    future retire_if target are legitimately unscored). Every applied head is bound to that source
    cycle, the canonical proposal, and the complete canonical surfaced-recurrence packet."""
    # Validate before even resolving/loading journal state. The explicit helper is intentional:
    # Pydantic model instances created through ``model_construct`` can bypass model validators.
    validated_proposal = ReflectionProposal.model_validate(proposal)
    validate_unique_reflection_edit_roles(validated_proposal.edits)
    canonical_proposal = validated_proposal.model_dump(mode="json")
    agents_dir = Path(agents_dir)
    journal_path = Path(journal_path)
    heads_path = reflector_heads_path(journal_path)
    anchor_path = (
        Path(anchor_path)
        if anchor_path is not None
        else journal_path.with_name(REFLECTOR_HEAD_ANCHOR_NAME)
    )
    heads = _load_reflector_heads(agents_dir, journal_path, heads_path, anchor_path)
    heads_bytes = heads_path.read_bytes()
    prior_anchor = _load_reflector_anchor(anchor_path, heads, heads_bytes)

    # Authenticate the complete recurrence packet before it can grant even the narrowly scoped
    # state-evidence citation exception. Explicit no-action proposals are authenticated too: they
    # consume the same one-shot authority and cooldown as an edit.
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
    old_journal = journal_path.read_bytes() if journal_path.exists() else b""
    if old_journal and not old_journal.endswith(b"\n"):
        raise ValueError("reflector journal is not newline-terminated")
    replay = reflection_consumption_replay_status(
        anchor_path.parent,
        journal_path.parent,
        source_cycle=current_cycle,
        recurrences_sha256=recurrences_sha256,
        proposal=canonical_proposal,
    )
    if replay is not None:
        return {
            "already_consumed": True,
            "applied": [],
            "consumed_outcome": replay["outcome"],
            "proposal_sha256": replay["proposal_sha256"],
            "skipped": [],
        }
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
    authority = validate_reflection_authority(
        anchor_path.parent,
        source_cycle=current_cycle,
        recurrences_sha256=recurrences_sha256,
        expected_heads_file_sha256=_sha256_bytes(heads_bytes),
        expected_journal_sha256=_sha256_bytes(old_journal),
    )
    authorized_state_evidence_by_role: dict[str, set[str]] = {}
    for recurrence in canonical_recurrences:
        if recurrence["kind"] == "pm_gate_inactive" and recurrence["role"] == "pm":
            authorized_state_evidence_by_role.setdefault("pm", set()).update(
                recurrence["evidence"]
            )

    edited_roles = {edit["role"] for edit in canonical_proposal["edits"]}
    omitted_roles = surfaced_roles - edited_roles
    if (not canonical_proposal["edits"] or omitted_roles) and not canonical_proposal[
        "no_action_reason"
    ].strip():
        names = ", ".join(sorted(omitted_roles))
        raise ValueError(
            "an explicit no-action/omitted-role reflection requires a nonempty "
            f"no_action_reason for surfaced role(s): {names or 'none'}"
        )

    applied: list[str] = []
    skipped: list[tuple[str, str]] = []
    prompt_updates: dict[Path, bytes] = {}
    applied_edits: list[tuple[str, dict, str]] = []
    for edit in canonical_proposal["edits"]:
        role = edit["role"]
        path = agents_dir / f"{role}.md"
        if role not in _ALL_ROLES or not path.exists():
            skipped.append((role, "unknown role or missing file"))
            continue
        if allowed_roles is not None and role not in allowed_roles:
            skipped.append((role, "no surfaced recurrence for this role"))
            continue
        if known_cycles is not None:
            refusal = validate_edit_citations(
                edit,
                known_cycles,
                current_cycle=current_cycle,
                authorized_state_evidence=authorized_state_evidence_by_role.get(role, set()),
            )
            if refusal:
                # A malformed evidence claim is not an intentional no-action decision. Refuse the
                # whole attempt so the host cannot consume and cool down its one-shot recurrence.
                raise ValueError(f"{role}: {refusal}")
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
    if skipped:
        details = "; ".join(f"{role}: {reason}" for role, reason in skipped)
        raise ValueError(f"reflection proposal contains unapplied edits: {details}")
    if not applied:
        return {"applied": applied, "skipped": skipped}
    generation = int(heads["generation"]) + 1
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
    post_files: dict[str, tuple[Path, bytes]] = {
        "anchor": (anchor_path, _anchor_bytes(new_anchor)),
        "heads": (heads_path, new_heads_content),
        "journal": (journal_path, bytes(new_journal)),
    }
    for path, content in prompt_updates.items():
        post_files[f"prompt:{path.stem}"] = (path, content)
    transaction_payload = {
        "authority_receipt_sha256": authority["receipt_sha256"],
        "files": [
            _transaction_snapshot_fields(key, _optional_file_snapshot(path), content)
            for key, (path, content) in sorted(post_files.items())
        ],
        "proposal": canonical_proposal,
        "proposal_sha256": proposal_sha256,
        "recurrences_sha256": recurrences_sha256,
        "schema_version": REFLECTION_APPLY_TRANSACTION_SCHEMA_VERSION,
        "source_cycle": current_cycle,
    }
    transaction = _seal_reflection_apply_transaction(transaction_payload)
    transaction_path = reflection_apply_transaction_path(anchor_path.parent, current_cycle)
    _publish_reflection_apply_transaction(transaction_path, transaction)
    records = {record["key"]: record for record in transaction["files"]}
    write_order = sorted(key for key in post_files if key.startswith("prompt:")) + [
        "journal",
        "heads",
        "anchor",
    ]
    try:
        for key in write_order:
            path, content = post_files[key]
            durable_write_bytes(path, content, mode=int(records[key]["post_mode"]))
        if not _validate_reflection_transaction_poststate(
            transaction,
            state_dir=anchor_path.parent,
            memory_dir=journal_path.parent,
            agents_dir=agents_dir,
            anchor_path=anchor_path,
            journal_path=journal_path,
        ):
            raise ValueError("reflection apply transaction poststate validation failed")
    except Exception:
        _restore_reflection_transaction_prestate(
            transaction,
            memory_dir=journal_path.parent,
            agents_dir=agents_dir,
            anchor_path=anchor_path,
            journal_path=journal_path,
        )
        durable_unlink(transaction_path)
        raise
    return {"applied": applied, "skipped": skipped}
