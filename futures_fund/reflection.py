"""File-orchestration glue for the self-learning loop.

Pure file IO — NO network, NO git — so it is fully testable offline. The thin CLI scripts
(`scripts/desk_score.py`, `scripts/reflector_apply.py`) add settings, snapshots, journaling, and
optional Git audit around these."""
from __future__ import annotations

import json
from pathlib import Path

from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.desk_contracts import Book, SpecialistRead
from futures_fund.prompt_guard import (
    PromptGuardError,
    assert_only_region_changed,
    splice_managed,
    split_managed,
)
from futures_fund.scorecard import (
    ScoreRecord,
    classify_objections,
    detect_recurrences,
    forward_returns,
    score_book,
    score_specialist,
)

SPECIALIST_ROLES = ("sentiment", "technical", "futures")


def persist_decision_snapshot(state_dir, cycle: int, *, evidence: list[dict], pending_dir,
                              cadence: str = "rebal") -> None:
    """Save this cycle's evidence snapshot (marks + beta_btc) and, if a revision happened, the
    pre-revision book, into the cycle dir for future scoring."""
    save_output(state_dir, cycle, "evidence", evidence, cadence=cadence)
    orig = Path(pending_dir) / "pm_book_original.json"
    if orig.exists():
        save_output(state_dir, cycle, "book_original", json.loads(orig.read_text()),
                    cadence=cadence)


def _read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _resolve_pending_dir(memory_dir) -> Path:
    """The CURRENT cycle's pending dir via pending_io when the pointer exists; the flat root
    otherwise (legacy layout / offline tests). Import is local to keep this module IO-pure."""
    from futures_fund.pending_io import resolve_pending
    try:
        pending, _meta = resolve_pending(memory_dir)
        return pending
    except (FileNotFoundError, ValueError, KeyError):
        return Path(memory_dir) / "pending"


def score_previous_cycle(state_dir, memory_dir, *, scored_cycle: int, cur_marks: dict[str, float],
                         now: str, btc_symbol: str, cadence: str = "rebal", k: int = 3,
                         window: int = 6) -> dict:
    pending = _resolve_pending_dir(memory_dir)
    pending.mkdir(parents=True, exist_ok=True)
    rec_path = pending / "recurrences.json"
    # consume-once: a stale proposal from a prior cycle must never be applied by reflector_apply
    (pending / "reflection.json").unlink(missing_ok=True)
    d = cycle_dir(state_dir, scored_cycle, cadence=cadence)
    if scored_cycle < 1 or not (d / "evidence.json").exists():
        rec_path.write_text("[]")
        return {"scored_cycle": None, "recurrences": []}

    prev_ev = _read_json(d / "evidence.json", [])
    marks_prev = {e["symbol"]: float(e["mark"]) for e in prev_ev}
    betas = {e["symbol"]: float(e.get("beta_btc", 1.0)) for e in prev_ev}
    rets = forward_returns(marks_prev, cur_marks)
    btc_ret = rets.get(btc_symbol, 0.0)

    reads_raw = _read_json(d / "reads.json", {})
    specialists = {}
    for role in SPECIALIST_ROLES:
        reads = [SpecialistRead.model_validate(x) for x in reads_raw.get(role, [])]
        specialists[role] = score_specialist(role, reads, rets)

    book = Book.model_validate(_read_json(d / "book.json", {"legs": []}))
    bscore = score_book(book, rets, betas, btc_ret)

    # Lenient parse: scoring only needs accept/objections, and recorded verdicts may predate
    # the strict AdversaryVerdict schema (which now REQUIRES the precheck echo fields).
    adv_raw = _read_json(d / "adversary.json", {"accept": True})
    adv_accept = bool(adv_raw.get("accept", True))
    adv_objections = list(adv_raw.get("objections", []))
    record = ScoreRecord(
        cycle=scored_cycle, scored_at=now, n_symbols=len(rets), specialists=specialists,
        book=bscore, adv_accepted=adv_accept, adv_revised=not adv_accept,
        adv_reason_tags=classify_objections(adv_objections) if not adv_accept else [],
    )
    save_output(state_dir, scored_cycle, "attribution", record.model_dump(mode="json"),
                cadence=cadence)

    sc_path = Path(memory_dir) / "scorecard.jsonl"
    with sc_path.open("a") as f:
        f.write(record.model_dump_json() + "\n")
    records = [ScoreRecord.model_validate_json(ln)
               for ln in sc_path.read_text().splitlines() if ln.strip()]
    # dedup by cycle (keep last) so a fail-soft resume that re-scores a cycle can't double-count
    by_cycle = {r.cycle: r for r in records}
    records = [by_cycle[c] for c in sorted(by_cycle)]
    recs = detect_recurrences(records, k=k, window=window)
    payload = [r.model_dump(mode="json") for r in recs]
    rec_path.write_text(json.dumps(payload, indent=2))
    return {"scored_cycle": scored_cycle, "recurrences": payload}


_ALL_ROLES = (*SPECIALIST_ROLES, "pm", "adversary")


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


def audit_managed_region_provenance(agents_dir, journal_path) -> list[str]:
    """List active reflector regions that are not backed by this desk's local journal.

    This catches plausible-looking calibration text copied from a predecessor desk: the prompt
    alone cannot authenticate its cycle numbers, while every legitimate local reflector edit is
    journaled together with its exact role and region body.
    """
    agents_dir = Path(agents_dir)
    journaled = journaled_managed_regions(journal_path)
    issues: list[str] = []
    for role in _ALL_ROLES:
        path = agents_dir / f"{role}.md"
        if not path.exists():
            issues.append(f"{role}: missing prompt")
            continue
        try:
            _prefix, region, _suffix = split_managed(path.read_text())
        except PromptGuardError as exc:
            issues.append(f"{role}: {exc}")
            continue
        active = region.strip()
        if active and active not in journaled.get(role, []):
            issues.append(f"{role}: active managed region is not backed by reflector-journal.md")
    return issues


def scored_cycles(memory_dir) -> set[int]:
    """The set of cycle numbers that actually have a ScoreRecord in scorecard.jsonl."""
    sc_path = Path(memory_dir) / "scorecard.jsonl"
    if not sc_path.exists():
        return set()
    out: set[int] = set()
    for ln in sc_path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            out.add(int(json.loads(ln).get("cycle")))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return out


def validate_edit_citations(edit: dict, cycles: set[int],
                            current_cycle: int | None = None) -> str | None:
    """Deterministic evidence-integrity guard (2026-07 review: the Reflector cited per-cycle
    scores for cycles that had NO ScoreRecord — fabricated evidence tuned live decision prompts).

    The fabrication mode is a claim about a PAST cycle's measured score that doesn't exist. A
    reference to the CURRENT cycle (the note's "[cN]" date tag) or a FUTURE cycle (a `retire_if:
    ... by cM` target) legitimately has no scorecard record yet — those are not evidence claims.
    So only a cited cycle STRICTLY BEFORE `current_cycle` must exist in scorecard.jsonl. When
    `current_cycle` is None, every cited cycle must exist (strict legacy behaviour). Returns a
    refusal reason, or None when clean."""
    import re
    text = " ".join([
        str(edit.get("region_text", "")), str(edit.get("reason", "")),
        " ".join(str(e) for e in edit.get("evidence", [])),
    ])
    cited = {int(m) for m in re.findall(r"\bc(?:ycle\s*)?(\d{1,4})\b", text, flags=re.IGNORECASE)}
    if current_cycle is not None:
        cited = {c for c in cited if c < current_cycle}   # only PAST-score claims are checkable
    missing = sorted(c for c in cited if c not in cycles)
    if missing:
        return (f"cites past cycle(s) {missing} with no ScoreRecord in scorecard.jsonl — "
                "fabricated or unverifiable evidence; edit refused")
    return None


def _journal(journal_path, role: str, edit: dict) -> None:
    path = Path(journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"## {role} — {edit.get('reason', '')}",
             f"- retire_if: {edit.get('retire_if', '')}",
             f"- evidence: {'; '.join(edit.get('evidence', []))}",
             f"- region:\n{edit.get('region_text', '')}\n"]
    with path.open("a") as f:
        f.write("\n".join(lines) + "\n")


def apply_reflection(proposal: dict, agents_dir, journal_path, *,
                     allowed_roles: set[str] | None = None,
                     known_cycles: set[int] | None = None,
                     current_cycle: int | None = None) -> dict:
    """Apply a reflection proposal to the agent prompts, guarded to the managed region.

    `allowed_roles` (when not None) restricts edits to roles the scorecard surfaced a
    recurrence for — a hallucinated edit to an unmentioned role is skipped. At most one
    edit per role (last wins). `known_cycles` (when not None) enables the evidence-integrity
    guard: an edit citing a PAST cycle with no ScoreRecord is refused (fabricated evidence).
    `current_cycle` scopes that guard to past-score claims only (the note's own [cN] tag and a
    future retire_if target are legitimately unscored)."""
    agents_dir = Path(agents_dir)
    applied: list[str] = []
    skipped: list[tuple[str, str]] = []
    # dedupe per role (last edit wins) so a role is edited at most once per cycle
    edits_by_role: dict = {}
    order: list = []
    for edit in proposal.get("edits", []):
        role = edit.get("role")
        if role not in order:
            order.append(role)
        edits_by_role[role] = edit
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
        path.write_text(new)
        _journal(journal_path, role, edit)
        applied.append(role)
    return {"applied": applied, "skipped": skipped}
