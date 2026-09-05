"""Crash-safe one-time migration of normal score rows to the current schema.

The historical desk has two trustworthy but differently-shaped representations of a score:

* ``scorecard.jsonl`` is the learning index; old rewrites sometimes materialized model defaults.
* ``cycle/<n>/attribution.json`` is the original per-cycle score payload.

Neither file is allowed to self-authorize a label.  This migration independently resolves the
earliest committed daily observation, rebuilds the score from manifest-bound origin/outcome
artifacts, and accepts an old row only when its historically shared fields replay exactly.  The
complete source generation is archived byte-for-byte before any canonical file is replaced.

Only scorecard and attribution files are in scope.  In particular, account, ledger, equity, and
cycle completion manifests are never written here.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from futures_fund.cycle_io import cycle_dir
from futures_fund.durable_io import (
    canonical_json_bytes,
    canonical_json_sha256,
    durable_unlink,
    durable_write_bytes,
)
from futures_fund.reflection import (
    CANONICAL_BTC_SYMBOL,
    _as_utc,
    _build_score_record,
    canonical_daily_score_observation,
    score_record_is_manifest_bound,
)
from futures_fund.scorecard import (
    CURRENT_SCORE_SCHEMA_VERSION,
    SCORECARD_MIGRATION_WAL_FILE,
    BookScore,
    ScoreRecord,
)

MIGRATION_PROTOCOL_SCHEMA_VERSION = 1
MIGRATION_KIND = "normal_scorecard_schema"
PROTOCOL_FILE = "scorecard-migration-v2.json"
WAL_FILE = SCORECARD_MIGRATION_WAL_FILE
ARCHIVE_ROOT = "scorecard-migration-v2"

# This is the exact historical schema whose scorecard representation was sometimes expanded by
# Pydantic defaults.  Keeping the set literal here is intentional: making it depend on today's
# model would turn a compatibility adapter into an open-ended trust path.
LEGACY_V1_TOP_FIELDS = frozenset(
    {
        "cycle",
        "btc_symbol",
        "scored_at",
        "evaluation_horizon_hours",
        "outcome_marks_sha256",
        "outcome_observation_cycle",
        "outcome_scoring_marks_sha256",
        "outcome_provenance",
        "n_symbols",
        "specialist_return_label",
        "specialists",
        "book",
        "adv_accepted",
        "adv_revised",
        "adv_reason_tags",
    }
)
LEGACY_V1_BOOK_FIELDS = frozenset(
    {
        "n_legs",
        "gross_notional",
        "gross_pnl",
        "return_frac",
        "beta_dollar",
        "alpha_net_beta",
        "alpha_frac",
        "projected_funding_pnl",
        "entry_friction",
        "realized_edge_ex_funding",
        "realized_edge_ex_funding_frac",
        "strategy_net_edge",
        "strategy_net_frac",
        "strategy_net_is_forecast",
    }
)


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _json_file_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _assert_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _assert_finite(child)
    elif isinstance(value, list):
        for child in value:
            _assert_finite(child)


def _parse_json_bytes(content: bytes, *, label: str) -> object:
    try:
        text = content.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite_constant,
        )
        _assert_finite(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON in {label}") from exc


def _parse_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    value = _parse_json_bytes(content, label=label)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {label}")
    return value


@dataclass(frozen=True)
class _SourceRow:
    line_number: int
    raw: dict[str, Any]


def _parse_scorecard(content: bytes, *, label: str) -> list[_SourceRow]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"malformed UTF-8 scorecard: {label}") from exc
    rows: list[_SourceRow] = []
    seen: set[int] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        raw = _parse_json_object(line.encode(), label=f"{label}:{line_number}")
        try:
            cycle = raw["cycle"]
        except KeyError as exc:
            raise ValueError(f"invalid score cycle in {label}:{line_number}") from exc
        if type(cycle) is not int or cycle < 1:  # noqa: E721 - bool must not pass as int
            raise ValueError(f"invalid score cycle in {label}:{line_number}")
        if cycle in seen:
            raise ValueError(f"duplicate scorecard cycle {cycle}")
        seen.add(cycle)
        rows.append(_SourceRow(line_number=line_number, raw=raw))
    return rows


def _same_json(left: object, right: object) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _same_historical_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare historical payloads exactly except for equivalent UTC timestamp spellings."""
    left_body = dict(left)
    right_body = dict(right)
    left_ts = left_body.pop("scored_at", None)
    right_ts = right_body.pop("scored_at", None)
    try:
        same_instant = _as_utc(str(left_ts)) == _as_utc(str(right_ts))
    except (TypeError, ValueError):
        return False
    return same_instant and _same_json(left_body, right_body)


def _legacy_projection(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            {book_key: raw["book"][book_key] for book_key in sorted(LEGACY_V1_BOOK_FIELDS)}
            if key == "book"
            else raw[key]
        )
        for key in sorted(LEGACY_V1_TOP_FIELDS)
    }


def _without_version(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key != "score_schema_version"}


def _shape_is(raw: dict[str, Any], top: frozenset[str], book: frozenset[str]) -> bool:
    return (
        frozenset(raw) == top
        and isinstance(raw.get("book"), dict)
        and frozenset(raw["book"]) == book
    )


def _current_shapes() -> tuple[frozenset[str], frozenset[str]]:
    top = frozenset(ScoreRecord.model_fields) - {"score_schema_version"}
    return top, frozenset(BookScore.model_fields)


def _materialized_legacy_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    parsed = ScoreRecord.model_validate(raw, strict=True)
    if parsed.score_schema_version is not None:
        raise ValueError("legacy row unexpectedly declares a current schema")
    return parsed.model_dump(mode="json", exclude={"score_schema_version"})


def _canonical_target(
    state_dir: Path,
    raw: dict[str, Any],
    *,
    cadence: str,
) -> dict[str, Any]:
    if raw.get("outcome_provenance") != "manifest_bound":
        raise ValueError("normal score migration received a non-manifest row")
    if raw.get("btc_symbol") != CANONICAL_BTC_SYMBOL:
        raise ValueError("manifest-bound score does not use the canonical BTC benchmark")
    try:
        cycle = int(raw["cycle"])
        source_observation_cycle = int(raw["outcome_observation_cycle"])
        source_scored_at = str(raw["scored_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("manifest-bound score has an invalid outcome identity") from exc
    observation = canonical_daily_score_observation(state_dir, cycle, cadence=cadence)
    if observation is None:
        raise ValueError(f"cycle {cycle} has no canonical committed daily score observation")
    observation_cycle, observation_ts, marks, artifact_sha256 = observation
    try:
        source_ts = _as_utc(source_scored_at)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cycle {cycle} has an invalid score timestamp") from exc
    if (
        source_observation_cycle != observation_cycle
        or raw.get("outcome_scoring_marks_sha256") != artifact_sha256
        or source_ts != observation_ts
        or CANONICAL_BTC_SYMBOL not in marks
    ):
        raise ValueError(f"cycle {cycle} score does not name its canonical earliest outcome")
    target_record = _build_score_record(
        state_dir,
        scored_cycle=cycle,
        cur_marks=marks,
        # The archive preserves the exact historical spelling (including a possible ``Z``).
        # Current rows use the canonical UTC spelling emitted by the observation resolver so the
        # strict verifier and every later rebuild have one byte-stable representation.
        now=observation_ts.isoformat(),
        btc_symbol=CANONICAL_BTC_SYMBOL,
        cadence=cadence,
        outcome_observation_cycle=observation_cycle,
        outcome_scoring_marks_sha256=artifact_sha256,
        outcome_provenance="manifest_bound",
    )
    if target_record.score_schema_version != CURRENT_SCORE_SCHEMA_VERSION:
        raise RuntimeError("current score builder did not emit the current schema version")
    target = target_record.model_dump(mode="json")
    identity_fields = (
        "cycle",
        "btc_symbol",
        "evaluation_horizon_hours",
        "outcome_marks_sha256",
        "outcome_observation_cycle",
        "outcome_scoring_marks_sha256",
        "outcome_provenance",
    )
    if any(not _same_json(raw.get(field), target.get(field)) for field in identity_fields):
        raise ValueError(f"cycle {cycle} score identity does not replay committed artifacts")
    if _as_utc(str(target["scored_at"])) != source_ts:
        raise ValueError(f"cycle {cycle} score timestamp instant was not preserved")
    return target


def _classify_manifest_row(
    score_raw: dict[str, Any],
    attribution_raw: dict[str, Any],
    target: dict[str, Any],
) -> str:
    current_top, current_book = _current_shapes()
    current_top_with_version = current_top | {"score_schema_version"}
    cycle = int(score_raw["cycle"])

    if _shape_is(attribution_raw, LEGACY_V1_TOP_FIELDS, LEGACY_V1_BOOK_FIELDS):
        if not _same_historical_payload(
            _legacy_projection(attribution_raw), _legacy_projection(target)
        ):
            raise ValueError(f"cycle {cycle} legacy attribution does not replay canonical fields")
        if _shape_is(score_raw, LEGACY_V1_TOP_FIELDS, LEGACY_V1_BOOK_FIELDS):
            if not _same_json(score_raw, attribution_raw):
                raise ValueError(f"cycle {cycle} sparse scorecard conflicts with attribution")
            return "legacy_v1_sparse"
        if _shape_is(score_raw, current_top, current_book):
            materialized = _materialized_legacy_defaults(attribution_raw)
            if not _same_json(score_raw, materialized):
                raise ValueError(
                    f"cycle {cycle} materialized legacy scorecard conflicts with attribution"
                )
            return "legacy_v1_materialized_defaults"
        raise ValueError(f"cycle {cycle} scorecard has an unsupported legacy shape")

    if _shape_is(attribution_raw, current_top, current_book):
        if not _same_historical_payload(attribution_raw, _without_version(target)):
            raise ValueError(f"cycle {cycle} implicit-current attribution is not canonical")
        if not _shape_is(score_raw, current_top, current_book) or not _same_json(
            score_raw, attribution_raw
        ):
            raise ValueError(f"cycle {cycle} implicit-current scorecard conflicts with attribution")
        return "implicit_current"

    if _shape_is(attribution_raw, current_top_with_version, current_book):
        if not _same_json(attribution_raw, target):
            raise ValueError(f"cycle {cycle} current attribution is not canonical")
        if not _shape_is(score_raw, current_top_with_version, current_book) or not _same_json(
            score_raw, attribution_raw
        ):
            raise ValueError(f"cycle {cycle} current scorecard conflicts with attribution")
        return "current"

    raise ValueError(f"cycle {cycle} attribution has an unsupported score schema")


@dataclass(frozen=True)
class _AttributionPlan:
    cycle: int
    classification: str
    canonical_path: Path
    source_bytes: bytes
    source_raw: dict[str, Any]
    target_bytes: bytes
    target_raw: dict[str, Any]


@dataclass(frozen=True)
class _MigrationPlan:
    state_dir: Path
    memory_dir: Path
    cadence: str
    source_scorecard_bytes: bytes
    target_scorecard_bytes: bytes
    source_rows: tuple[_SourceRow, ...]
    target_rows: tuple[dict[str, Any], ...]
    attributions: tuple[_AttributionPlan, ...]


def _prepare_plan(state_dir: Path, memory_dir: Path, *, cadence: str) -> _MigrationPlan | None:
    scorecard_path = memory_dir / "scorecard.jsonl"
    if not scorecard_path.exists():
        return None
    source_bytes = scorecard_path.read_bytes()
    source_rows = _parse_scorecard(source_bytes, label=str(scorecard_path))
    source_cycles = [int(item.raw["cycle"]) for item in source_rows]
    if source_cycles != sorted(source_cycles):
        raise ValueError("scorecard cycles must be strictly increasing before migration")
    target_by_cycle: dict[int, dict[str, Any]] = {}
    attributions: list[_AttributionPlan] = []
    for source in source_rows:
        raw = source.raw
        try:
            parsed = ScoreRecord.model_validate(raw, strict=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid scorecard row {scorecard_path}:{source.line_number}"
            ) from exc
        cycle = parsed.cycle
        if parsed.outcome_provenance == "legacy_unverified":
            if canonical_daily_score_observation(state_dir, cycle, cadence=cadence) is not None:
                raise ValueError(
                    f"cycle {cycle} claims legacy provenance despite a canonical bound outcome"
                )
            attribution_path = cycle_dir(state_dir, cycle, cadence=cadence) / "attribution.json"
            if attribution_path.exists():
                attribution_raw = _parse_json_object(
                    attribution_path.read_bytes(), label=str(attribution_path)
                )
                if attribution_raw.get("outcome_provenance") == "manifest_bound":
                    raise ValueError(
                        f"cycle {cycle} scorecard attempts to downgrade a manifest attribution"
                    )
            # The current writer serializes parsed models. Normalize once now so its next append
            # cannot turn an omitted discriminator/default into a false post-closure downgrade.
            target_by_cycle[cycle] = parsed.model_dump(mode="json")
            continue
        target = _canonical_target(state_dir, raw, cadence=cadence)
        attribution_path = cycle_dir(state_dir, cycle, cadence=cadence) / "attribution.json"
        try:
            attribution_bytes = attribution_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cycle {cycle} manifest score lacks source attribution") from exc
        attribution_raw = _parse_json_object(
            attribution_bytes, label=str(attribution_path)
        )
        classification = _classify_manifest_row(raw, attribution_raw, target)
        # A final strict check makes the target format and the current production verifier agree.
        target_record = ScoreRecord.model_validate(target, strict=True)
        if not score_record_is_manifest_bound(state_dir, target_record, cadence=cadence):
            raise ValueError(f"cycle {cycle} rebuilt score fails current provenance verification")
        target_by_cycle[cycle] = target
        attributions.append(
            _AttributionPlan(
                cycle=cycle,
                classification=classification,
                canonical_path=attribution_path,
                source_bytes=attribution_bytes,
                source_raw=attribution_raw,
                target_bytes=_json_file_bytes(target),
                target_raw=target,
            )
        )
    ordered_cycles = sorted(target_by_cycle)
    target_rows = tuple(target_by_cycle[cycle] for cycle in ordered_cycles)
    target_scorecard = b"".join(_json_file_bytes(row) for row in target_rows)
    return _MigrationPlan(
        state_dir=state_dir,
        memory_dir=memory_dir,
        cadence=cadence,
        source_scorecard_bytes=source_bytes,
        target_scorecard_bytes=target_scorecard,
        source_rows=tuple(source_rows),
        target_rows=target_rows,
        attributions=tuple(sorted(attributions, key=lambda item: item.cycle)),
    )


def _generation_descriptor(plan: _MigrationPlan) -> dict[str, Any]:
    return {
        "schema_version": MIGRATION_PROTOCOL_SCHEMA_VERSION,
        "kind": MIGRATION_KIND,
        "paper_only": True,
        "target_score_schema_version": CURRENT_SCORE_SCHEMA_VERSION,
        "cadence": plan.cadence,
        "source_scorecard_sha256": _sha256_bytes(plan.source_scorecard_bytes),
        "target_scorecard_sha256": _sha256_bytes(plan.target_scorecard_bytes),
        "scorecard_cycles": [int(row.raw["cycle"]) for row in plan.source_rows],
        "manifest_cycles": [item.cycle for item in plan.attributions],
        "attributions": {
            str(item.cycle): {
                "classification": item.classification,
                "source_sha256": _sha256_bytes(item.source_bytes),
                "target_sha256": _sha256_bytes(item.target_bytes),
            }
            for item in plan.attributions
        },
    }


def _manifest_for_plan(plan: _MigrationPlan) -> tuple[str, dict[str, Any]]:
    descriptor = _generation_descriptor(plan)
    generation_id = canonical_json_sha256(descriptor)
    body = {**descriptor, "generation_id": generation_id}
    return generation_id, {**body, "manifest_sha256": canonical_json_sha256(body)}


def _generation_dir(memory_dir: Path, generation_id: str) -> Path:
    return memory_dir / ARCHIVE_ROOT / "generations" / generation_id


def _ensure_exact_archive(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"conflicting score migration archive file: {path}")
        return
    durable_write_bytes(path, content, mode=0o400)


def _stage_generation(plan: _MigrationPlan) -> tuple[str, dict[str, Any], dict[str, Any]]:
    generation_id, manifest = _manifest_for_plan(plan)
    directory = _generation_dir(plan.memory_dir, generation_id)
    _ensure_exact_archive(directory / "source-scorecard.jsonl", plan.source_scorecard_bytes)
    _ensure_exact_archive(directory / "target-scorecard.jsonl", plan.target_scorecard_bytes)
    for item in plan.attributions:
        _ensure_exact_archive(
            directory / "source-attributions" / f"cycle-{item.cycle}.json",
            item.source_bytes,
        )
        _ensure_exact_archive(
            directory / "target-attributions" / f"cycle-{item.cycle}.json",
            item.target_bytes,
        )
    _ensure_exact_archive(directory / "manifest.json", _json_file_bytes(manifest))
    wal_body = {
        "schema_version": MIGRATION_PROTOCOL_SCHEMA_VERSION,
        "kind": MIGRATION_KIND,
        "paper_only": True,
        "target_score_schema_version": CURRENT_SCORE_SCHEMA_VERSION,
        "generation_id": generation_id,
        "manifest_sha256": manifest["manifest_sha256"],
        "source_scorecard_sha256": manifest["source_scorecard_sha256"],
        "target_scorecard_sha256": manifest["target_scorecard_sha256"],
        "attributions": manifest["attributions"],
    }
    wal = {**wal_body, "intent_sha256": canonical_json_sha256(wal_body)}
    wal_path = plan.memory_dir / WAL_FILE
    if wal_path.exists():
        existing = _parse_json_object(wal_path.read_bytes(), label=str(wal_path))
        if not _same_json(existing, wal):
            raise ValueError("conflicting score migration WAL")
    else:
        durable_write_bytes(wal_path, _json_file_bytes(wal))
    return generation_id, manifest, wal


def _validate_digest(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_wal(memory_dir: Path) -> dict[str, Any]:
    path = memory_dir / WAL_FILE
    raw = _parse_json_object(path.read_bytes(), label=str(path))
    expected_fields = {
        "schema_version",
        "kind",
        "paper_only",
        "target_score_schema_version",
        "generation_id",
        "manifest_sha256",
        "source_scorecard_sha256",
        "target_scorecard_sha256",
        "attributions",
        "intent_sha256",
    }
    body = {key: value for key, value in raw.items() if key != "intent_sha256"}
    if (
        set(raw) != expected_fields
        or raw.get("schema_version") != MIGRATION_PROTOCOL_SCHEMA_VERSION
        or raw.get("kind") != MIGRATION_KIND
        or raw.get("paper_only") is not True
        or raw.get("target_score_schema_version") != CURRENT_SCORE_SCHEMA_VERSION
        or not _validate_digest(raw.get("generation_id"))
        or not _validate_digest(raw.get("manifest_sha256"))
        or not _validate_digest(raw.get("source_scorecard_sha256"))
        or not _validate_digest(raw.get("target_scorecard_sha256"))
        or raw.get("intent_sha256") != canonical_json_sha256(body)
        or not isinstance(raw.get("attributions"), dict)
    ):
        raise ValueError("invalid score migration WAL")
    return raw


def _validated_manifest(memory_dir: Path, generation_id: str) -> dict[str, Any]:
    directory = _generation_dir(memory_dir, generation_id)
    path = directory / "manifest.json"
    raw = _parse_json_object(path.read_bytes(), label=str(path))
    body = {key: value for key, value in raw.items() if key != "manifest_sha256"}
    expected_fields = {
        "schema_version",
        "kind",
        "paper_only",
        "target_score_schema_version",
        "cadence",
        "source_scorecard_sha256",
        "target_scorecard_sha256",
        "scorecard_cycles",
        "manifest_cycles",
        "attributions",
        "generation_id",
        "manifest_sha256",
    }
    descriptor = {key: value for key, value in body.items() if key != "generation_id"}
    if (
        set(raw) != expected_fields
        or raw.get("schema_version") != MIGRATION_PROTOCOL_SCHEMA_VERSION
        or raw.get("kind") != MIGRATION_KIND
        or raw.get("paper_only") is not True
        or raw.get("target_score_schema_version") != CURRENT_SCORE_SCHEMA_VERSION
        or raw.get("generation_id") != generation_id
        or generation_id != canonical_json_sha256(descriptor)
        or raw.get("manifest_sha256") != canonical_json_sha256(body)
        or not isinstance(raw.get("attributions"), dict)
        or not isinstance(raw.get("scorecard_cycles"), list)
        or not isinstance(raw.get("manifest_cycles"), list)
    ):
        raise ValueError("invalid score migration generation manifest")
    source_score = (directory / "source-scorecard.jsonl").read_bytes()
    target_score = (directory / "target-scorecard.jsonl").read_bytes()
    if (
        _sha256_bytes(source_score) != raw.get("source_scorecard_sha256")
        or _sha256_bytes(target_score) != raw.get("target_scorecard_sha256")
    ):
        raise ValueError("score migration scorecard archive hash mismatch")
    if any(
        type(cycle) is not int  # noqa: E721 - bool and coercible numbers must fail closed
        for cycle in [*raw["manifest_cycles"], *raw["scorecard_cycles"]]
    ):
        raise ValueError("invalid score migration cycle list")
    manifest_cycles = list(raw["manifest_cycles"])
    scorecard_cycles = list(raw["scorecard_cycles"])
    if (
        manifest_cycles != sorted(set(manifest_cycles))
        or scorecard_cycles != sorted(set(scorecard_cycles))
        or not set(manifest_cycles).issubset(scorecard_cycles)
        or set(raw["attributions"]) != {str(cycle) for cycle in manifest_cycles}
    ):
        raise ValueError("invalid score migration cycle inventory")
    for cycle in manifest_cycles:
        item = raw["attributions"].get(str(cycle))
        if not isinstance(item, dict) or set(item) != {
            "classification",
            "source_sha256",
            "target_sha256",
        }:
            raise ValueError("invalid score migration attribution manifest")
        for side in ("source", "target"):
            digest = item.get(f"{side}_sha256")
            archive_path = directory / f"{side}-attributions" / f"cycle-{cycle}.json"
            if not _validate_digest(digest) or _sha256_bytes(archive_path.read_bytes()) != digest:
                raise ValueError("score migration attribution archive hash mismatch")
    return raw


def _archived_target_rows(
    state_dir: Path,
    memory_dir: Path,
    manifest: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    directory = _generation_dir(memory_dir, manifest["generation_id"])
    rows = _parse_scorecard(
        (directory / "target-scorecard.jsonl").read_bytes(),
        label="archived target scorecard",
    )
    result = {int(item.raw["cycle"]): item.raw for item in rows}
    if sorted(result) != manifest["scorecard_cycles"]:
        raise ValueError("archived target scorecard cycle inventory mismatch")
    for cycle in manifest["manifest_cycles"]:
        raw = result[cycle]
        record = ScoreRecord.model_validate(raw, strict=True)
        if (
            record.score_schema_version != CURRENT_SCORE_SCHEMA_VERSION
            or record.outcome_provenance != "manifest_bound"
            or not score_record_is_manifest_bound(
                state_dir, record, cadence=str(manifest["cadence"])
            )
        ):
            raise ValueError(f"archived target score cycle {cycle} is no longer provenance-valid")
        attr_raw = _parse_json_object(
            (
                directory
                / "target-attributions"
                / f"cycle-{cycle}.json"
            ).read_bytes(),
            label=f"archived target attribution cycle {cycle}",
        )
        if not _same_json(attr_raw, raw):
            raise ValueError(f"archived target attribution cycle {cycle} conflicts with score")
    return result


def _lineage_state(path: Path, source_sha256: str, target_sha256: str) -> str:
    if not path.exists():
        return "third"
    digest = _sha256_bytes(path.read_bytes())
    if digest == target_sha256:
        return "target"
    if digest == source_sha256:
        return "source"
    return "third"


def _valid_target_extension(
    state_dir: Path,
    current: bytes,
    target: bytes,
    manifest: dict[str, Any],
) -> bool:
    """Accept only an exact target prefix followed by new, strict manifest-bound v2 rows.

    The host desk lock normally makes this path unnecessary.  It remains a recovery guard for a
    process that had already opened the scorecard before the migration acquired the host lock and
    published a completed score after the aggregate target but before the protocol receipt.
    """
    if not target or not target.endswith(b"\n") or not current.startswith(target):
        return False
    try:
        target_rows = _parse_scorecard(target, label="archived target scorecard")
        current_rows = _parse_scorecard(current, label="extended target scorecard")
    except ValueError:
        return False
    target_cycles = {int(item.raw["cycle"]) for item in target_rows}
    if len(current_rows) <= len(target_rows):
        return False
    current_cycles = [int(item.raw["cycle"]) for item in current_rows]
    if current_cycles != sorted(current_cycles):
        return False
    for item in current_rows[len(target_rows) :]:
        cycle = int(item.raw["cycle"])
        if cycle in target_cycles:
            return False
        try:
            record = ScoreRecord.model_validate(item.raw, strict=True)
        except (TypeError, ValueError):
            return False
        if (
            record.score_schema_version != CURRENT_SCORE_SCHEMA_VERSION
            or record.outcome_provenance != "manifest_bound"
            or not score_record_is_manifest_bound(
                state_dir, record, cadence=str(manifest["cadence"])
            )
            or not _row_matches_canonical_attribution(
                state_dir,
                item.raw,
                cadence=str(manifest["cadence"]),
            )
        ):
            return False
        target_cycles.add(cycle)
    return True


def _scorecard_lineage_state(
    state_dir: Path,
    path: Path,
    source_sha256: str,
    target_sha256: str,
    target_bytes: bytes,
    manifest: dict[str, Any],
) -> str:
    state = _lineage_state(path, source_sha256, target_sha256)
    if state != "third":
        return state
    if path.exists() and _valid_target_extension(
        state_dir, path.read_bytes(), target_bytes, manifest
    ):
        return "target_extended"
    return "third"


def _row_matches_canonical_attribution(
    state_dir: Path, raw: dict[str, Any], *, cadence: str
) -> bool:
    cycle = raw.get("cycle")
    if type(cycle) is not int:  # noqa: E721 - exact JSON integer required
        return False
    path = cycle_dir(state_dir, cycle, cadence=cadence) / "attribution.json"
    try:
        attribution = _parse_json_object(path.read_bytes(), label=str(path))
        ScoreRecord.model_validate(attribution, strict=True)
    except (OSError, TypeError, ValueError):
        return False
    return _same_json(raw, attribution)


def _completion_for(manifest: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema_version": MIGRATION_PROTOCOL_SCHEMA_VERSION,
        "kind": MIGRATION_KIND,
        "paper_only": True,
        "target_score_schema_version": CURRENT_SCORE_SCHEMA_VERSION,
        "generation_id": manifest["generation_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "target_scorecard_sha256": manifest["target_scorecard_sha256"],
    }
    return {**body, "completion_sha256": canonical_json_sha256(body)}


def _protocol_for(manifest: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema_version": MIGRATION_PROTOCOL_SCHEMA_VERSION,
        "kind": MIGRATION_KIND,
        "paper_only": True,
        "status": "complete",
        "target_score_schema_version": CURRENT_SCORE_SCHEMA_VERSION,
        "generation_id": manifest["generation_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "completion_sha256": _completion_for(manifest)["completion_sha256"],
        "migrated_cycles": list(manifest["manifest_cycles"]),
    }
    return {**body, "protocol_sha256": canonical_json_sha256(body)}


def _ensure_exact_json(path: Path, expected: dict[str, Any], *, label: str) -> None:
    if path.exists():
        actual = _parse_json_object(path.read_bytes(), label=str(path))
        if not _same_json(actual, expected):
            raise ValueError(f"conflicting {label}")
        return
    durable_write_bytes(path, _json_file_bytes(expected))


def _assert_existing_exact_json(
    path: Path, expected: dict[str, Any], *, label: str
) -> None:
    if not path.exists():
        return
    actual = _parse_json_object(path.read_bytes(), label=str(path))
    if not _same_json(actual, expected):
        raise ValueError(f"conflicting {label}")


def _recover_wal_unlocked(state_dir: Path, memory_dir: Path) -> dict[str, Any]:
    wal = _validated_wal(memory_dir)
    generation_id = str(wal["generation_id"])
    manifest = _validated_manifest(memory_dir, generation_id)
    if any(
        not _same_json(wal.get(field), manifest.get(field))
        for field in (
            "manifest_sha256",
            "source_scorecard_sha256",
            "target_scorecard_sha256",
            "attributions",
        )
    ):
        raise ValueError("score migration WAL conflicts with archived manifest")
    _archived_target_rows(state_dir, memory_dir, manifest)
    directory = _generation_dir(memory_dir, generation_id)
    completion = _completion_for(manifest)
    completion_path = directory / "complete.json"
    protocol = _protocol_for(manifest)
    protocol_path = memory_dir / PROTOCOL_FILE
    # A crash may leave either terminal receipt beside the WAL. Validate any existing terminal
    # state before replacing canonical files; a conflicting receipt is a third state, not
    # permission to mutate and discover the conflict afterward.
    _assert_existing_exact_json(
        completion_path, completion, label="score migration completion"
    )
    _assert_existing_exact_json(
        protocol_path, protocol, label="score migration protocol"
    )

    # Validate the entire canonical generation before writing any part of it.  A mix of staged
    # source and target states is recoverable; any third state is evidence of an external race or
    # tampering and must not be overwritten by a guessed repair.
    canonical: list[tuple[Path, str, str, bytes]] = []
    for cycle in manifest["manifest_cycles"]:
        hashes = manifest["attributions"][str(cycle)]
        canonical.append(
            (
                cycle_dir(state_dir, cycle, cadence=str(manifest["cadence"]))
                / "attribution.json",
                hashes["source_sha256"],
                hashes["target_sha256"],
                (
                    directory / "target-attributions" / f"cycle-{cycle}.json"
                ).read_bytes(),
            )
        )
    canonical.append(
        (
            memory_dir / "scorecard.jsonl",
            manifest["source_scorecard_sha256"],
            manifest["target_scorecard_sha256"],
            (directory / "target-scorecard.jsonl").read_bytes(),
        )
    )
    invalid = [
        str(path)
        for path, source, target, _ in canonical[:-1]
        if _lineage_state(path, source, target) == "third"
    ]
    score_path, score_source, score_target, score_content = canonical[-1]
    score_state = _scorecard_lineage_state(
        state_dir,
        score_path,
        score_source,
        score_target,
        score_content,
        manifest,
    )
    if score_state == "third":
        invalid.append(str(score_path))
    if invalid:
        raise ValueError(
            "score migration canonical file is neither staged source nor target: "
            + ", ".join(invalid)
        )

    # Per-cycle attributions are intentionally durable before the aggregate learning index.
    for path, source, target, content in canonical[:-1]:
        if _lineage_state(path, source, target) == "source":
            durable_write_bytes(path, content)
    if score_state == "source":
        durable_write_bytes(score_path, score_content)

    _ensure_exact_json(completion_path, completion, label="score migration completion")
    _ensure_exact_json(protocol_path, protocol, label="score migration protocol")
    durable_unlink(memory_dir / WAL_FILE)
    return {
        "migrated": True,
        "recovered": True,
        "already_complete": False,
        "generation_id": generation_id,
        "migrated_cycles": list(manifest["manifest_cycles"]),
    }


def _validated_protocol(memory_dir: Path) -> dict[str, Any]:
    path = memory_dir / PROTOCOL_FILE
    raw = _parse_json_object(path.read_bytes(), label=str(path))
    expected_fields = {
        "schema_version",
        "kind",
        "paper_only",
        "status",
        "target_score_schema_version",
        "generation_id",
        "manifest_sha256",
        "completion_sha256",
        "migrated_cycles",
        "protocol_sha256",
    }
    body = {key: value for key, value in raw.items() if key != "protocol_sha256"}
    if (
        set(raw) != expected_fields
        or raw.get("schema_version") != MIGRATION_PROTOCOL_SCHEMA_VERSION
        or raw.get("kind") != MIGRATION_KIND
        or raw.get("paper_only") is not True
        or raw.get("status") != "complete"
        or raw.get("target_score_schema_version") != CURRENT_SCORE_SCHEMA_VERSION
        or not _validate_digest(raw.get("generation_id"))
        or not _validate_digest(raw.get("manifest_sha256"))
        or not _validate_digest(raw.get("completion_sha256"))
        or raw.get("protocol_sha256") != canonical_json_sha256(body)
        or not isinstance(raw.get("migrated_cycles"), list)
    ):
        raise ValueError("invalid completed score migration protocol")
    return raw


def _verify_completed_unlocked(
    state_dir: Path, memory_dir: Path, protocol: dict[str, Any]
) -> dict[str, Any]:
    manifest = _validated_manifest(memory_dir, str(protocol["generation_id"]))
    if (
        protocol["manifest_sha256"] != manifest["manifest_sha256"]
        or protocol["migrated_cycles"] != manifest["manifest_cycles"]
    ):
        raise ValueError("completed score migration protocol conflicts with generation")
    completion_path = _generation_dir(
        memory_dir, str(protocol["generation_id"])
    ) / "complete.json"
    completion = _parse_json_object(completion_path.read_bytes(), label=str(completion_path))
    expected_completion = _completion_for(manifest)
    if not _same_json(completion, expected_completion) or (
        protocol["completion_sha256"] != completion["completion_sha256"]
    ):
        raise ValueError("completed score migration receipt conflicts with generation")
    archived_targets = _archived_target_rows(state_dir, memory_dir, manifest)

    scorecard_path = memory_dir / "scorecard.jsonl"
    current_rows = _parse_scorecard(scorecard_path.read_bytes(), label=str(scorecard_path))
    current_by_cycle = {int(item.raw["cycle"]): item.raw for item in current_rows}
    if list(current_by_cycle) != sorted(current_by_cycle):
        raise ValueError("post-migration scorecard cycles are not strictly increasing")
    originally_manifest_bound = set(manifest["manifest_cycles"])
    for cycle, expected in archived_targets.items():
        actual = current_by_cycle.get(cycle)
        if actual is None:
            raise ValueError(f"completed score migration cycle {cycle} was changed or downgraded")
        if _same_json(actual, expected):
            # Archived legacy rows are allowed to mature after the protocol closes, but the
            # aggregate scorecard and per-cycle attribution are one logical record.  Once the
            # attribution has become a canonical manifest-bound v2 record, restoring only the
            # archived legacy score row is a downgrade, not an idempotent completed state.
            if cycle not in originally_manifest_bound:
                attribution_path = cycle_dir(
                    state_dir, cycle, cadence=str(manifest["cadence"])
                ) / "attribution.json"
                try:
                    attribution_raw = _parse_json_object(
                        attribution_path.read_bytes(), label=str(attribution_path)
                    )
                    attribution = ScoreRecord.model_validate(attribution_raw, strict=True)
                except (OSError, TypeError, ValueError):
                    attribution = None
                if (
                    attribution is not None
                    and attribution.score_schema_version == CURRENT_SCORE_SCHEMA_VERSION
                    and attribution.outcome_provenance == "manifest_bound"
                    and score_record_is_manifest_bound(
                        state_dir,
                        attribution,
                        cadence=str(manifest["cadence"]),
                    )
                ):
                    raise ValueError(
                        f"completed score migration legacy cycle {cycle} was changed or downgraded"
                    )
            continue
        # A legacy-unverified row can legitimately become a canonical v2 row when a later complete
        # observation finally matures. Only that one-way, fully replayed upgrade is allowed; rows
        # that were already manifest-bound at migration remain byte-semantically frozen.
        if cycle in originally_manifest_bound:
            raise ValueError(f"completed score migration cycle {cycle} was changed or downgraded")
        try:
            upgraded = ScoreRecord.model_validate(actual, strict=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"completed score migration legacy cycle {cycle} changed invalidly"
            ) from exc
        if (
            upgraded.score_schema_version != CURRENT_SCORE_SCHEMA_VERSION
            or upgraded.outcome_provenance != "manifest_bound"
            or not score_record_is_manifest_bound(
                state_dir, upgraded, cadence=str(manifest["cadence"])
            )
            or not _row_matches_canonical_attribution(
                state_dir, actual, cadence=str(manifest["cadence"])
            )
        ):
            raise ValueError(
                f"completed score migration legacy cycle {cycle} changed invalidly"
            )
    for cycle, raw in current_by_cycle.items():
        if cycle in archived_targets:
            continue
        try:
            record = ScoreRecord.model_validate(raw, strict=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid post-migration score cycle {cycle}") from exc
        if (
            record.score_schema_version != CURRENT_SCORE_SCHEMA_VERSION
            or record.outcome_provenance != "manifest_bound"
            or not score_record_is_manifest_bound(
                state_dir, record, cadence=str(manifest["cadence"])
            )
            or not _row_matches_canonical_attribution(
                state_dir, raw, cadence=str(manifest["cadence"])
            )
        ):
            raise ValueError(f"untrusted post-migration score cycle {cycle}")
    for cycle in manifest["manifest_cycles"]:
        hashes = manifest["attributions"][str(cycle)]
        path = cycle_dir(
            state_dir, cycle, cadence=str(manifest["cadence"])
        ) / "attribution.json"
        if not path.exists() or _sha256_bytes(path.read_bytes()) != hashes["target_sha256"]:
            raise ValueError(
                f"completed score migration attribution cycle {cycle} was changed or downgraded"
            )
    return {
        "migrated": False,
        "recovered": False,
        "already_complete": True,
        "generation_id": protocol["generation_id"],
        "migrated_cycles": list(protocol["migrated_cycles"]),
    }


@contextmanager
def _exclusive_memory_lock(memory_dir: Path) -> Iterator[None]:
    if not memory_dir.exists() or not memory_dir.is_dir():
        raise ValueError("score migration requires an existing memory directory")
    descriptor = os.open(memory_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _inferred_desk_lock(
    state_dir: Path, memory_dir: Path, desk_lock_path: str | Path | None
) -> Path:
    if desk_lock_path is not None:
        return Path(desk_lock_path)
    try:
        state_parent = state_dir.resolve().parent
        memory_parent = memory_dir.resolve().parent
    except OSError as exc:
        raise ValueError("cannot resolve desk roots for score migration lock") from exc
    if state_parent != memory_parent:
        raise ValueError("state and memory must be siblings to infer the desk migration lock")
    return state_parent / "logs" / "desk-cycle.lock"


@contextmanager
def _exclusive_host_desk_lock(path: Path) -> Iterator[None]:
    """Join the launcher's exact single-flight lock without exposing an unsafe bypass."""
    if not path.exists() or not path.is_file():
        raise ValueError(f"desk cycle lock must already exist: {path}")
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another desk cycle owns the host lock; migration refused") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _recover_unlocked(state_dir: Path, memory_dir: Path) -> dict[str, Any] | None:
    wal_path = memory_dir / WAL_FILE
    protocol_path = memory_dir / PROTOCOL_FILE
    if wal_path.exists():
        return _recover_wal_unlocked(state_dir, memory_dir)
    if protocol_path.exists():
        return _verify_completed_unlocked(
            state_dir, memory_dir, _validated_protocol(memory_dir)
        )
    return None


def recover_scorecard_migration(
    state_dir: str | Path,
    memory_dir: str | Path,
    *,
    cadence: str = "rebal",
    btc_symbol: str = CANONICAL_BTC_SYMBOL,
    desk_lock_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Recover an interrupted generation or verify the completed one idempotently."""
    if btc_symbol != CANONICAL_BTC_SYMBOL:
        raise ValueError("score migration benchmark is fixed to the canonical BTC symbol")
    if cadence != "rebal":
        raise ValueError("normal score migration cadence is fixed to rebal")
    state = Path(state_dir)
    memory = Path(memory_dir)
    host_lock = _inferred_desk_lock(state, memory, desk_lock_path)
    with _exclusive_host_desk_lock(host_lock):
        with _exclusive_memory_lock(memory):
            result = _recover_unlocked(state, memory)
            if result is not None and cadence != _validated_manifest(
                memory, str(result["generation_id"])
            )["cadence"]:
                raise ValueError("score migration cadence conflicts with completed generation")
            return result


def migrate_scorecard(
    state_dir: str | Path,
    memory_dir: str | Path,
    *,
    cadence: str = "rebal",
    btc_symbol: str = CANONICAL_BTC_SYMBOL,
    desk_lock_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate, archive, and atomically roll the normal score generation to schema v2."""
    if btc_symbol != CANONICAL_BTC_SYMBOL:
        raise ValueError("score migration benchmark is fixed to the canonical BTC symbol")
    if cadence != "rebal":
        raise ValueError("normal score migration cadence is fixed to rebal")
    state = Path(state_dir)
    memory = Path(memory_dir)
    host_lock = _inferred_desk_lock(state, memory, desk_lock_path)
    with _exclusive_host_desk_lock(host_lock):
        with _exclusive_memory_lock(memory):
            recovered = _recover_unlocked(state, memory)
            if recovered is not None:
                manifest = _validated_manifest(memory, str(recovered["generation_id"]))
                if cadence != manifest["cadence"]:
                    raise ValueError(
                        "score migration cadence conflicts with completed generation"
                    )
                return recovered
            plan = _prepare_plan(state, memory, cadence=cadence)
            if plan is None:
                return {
                    "migrated": False,
                    "recovered": False,
                    "already_complete": False,
                    "reason": "scorecard_missing",
                    "migrated_cycles": [],
                }
            generation_id, _manifest, _wal = _stage_generation(plan)
            result = _recover_wal_unlocked(state, memory)
            if result["generation_id"] != generation_id:
                raise RuntimeError("score migration recovered a different generation")
            result["recovered"] = False
            return result
