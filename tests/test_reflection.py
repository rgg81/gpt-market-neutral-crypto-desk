import json
from pathlib import Path

import pytest

from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.prompt_guard import BEGIN, END, split_managed
from futures_fund.reflection import (
    apply_reflection,
    audit_managed_region_provenance,
    persist_decision_snapshot,
    score_previous_cycle,
)


def _seed_cycle(state_dir, cycle, *, marks, betas, reads, book, adversary):
    ev = [{"symbol": s, "mark": marks[s], "beta_btc": betas.get(s, 1.0)} for s in marks]
    save_output(state_dir, cycle, "evidence", ev, cadence="rebal")
    save_output(state_dir, cycle, "reads", reads, cadence="rebal")
    save_output(state_dir, cycle, "book", book, cadence="rebal")
    save_output(state_dir, cycle, "adversary", adversary, cadence="rebal")


def test_persist_decision_snapshot_writes_evidence(tmp_path):
    pending = tmp_path / "mem" / "pending"
    pending.mkdir(parents=True)
    ev = [{"symbol": "A", "mark": 1.0, "beta_btc": 1.0}]
    persist_decision_snapshot(str(tmp_path / "st"), 5, evidence=ev, pending_dir=pending)
    saved = json.loads((cycle_dir(str(tmp_path / "st"), 5, cadence="rebal") / "evidence.json")
                       .read_text())
    assert saved[0]["symbol"] == "A"


def test_persist_snapshot_copies_original_book_when_present(tmp_path):
    pending = tmp_path / "mem" / "pending"
    pending.mkdir(parents=True)
    (pending / "pm_book_original.json").write_text(json.dumps({"legs": [], "notes": "orig"}))
    persist_decision_snapshot(str(tmp_path / "st"), 5, evidence=[], pending_dir=pending)
    orig = cycle_dir(str(tmp_path / "st"), 5, cadence="rebal") / "book_original.json"
    assert json.loads(orig.read_text())["notes"] == "orig"


def test_score_previous_cycle_no_prev_is_noop(tmp_path):
    res = score_previous_cycle(str(tmp_path / "st"), str(tmp_path / "mem"),
                               scored_cycle=0, cur_marks={"A": 1.0}, now="t",
                               btc_symbol="BTC/USDT:USDT")
    assert res["scored_cycle"] is None
    assert json.loads((tmp_path / "mem" / "pending" / "recurrences.json").read_text()) == []


def test_score_previous_cycle_scores_and_appends(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    reads = {"sentiment": [{"symbol": "A", "lean": "long", "conviction": 0.9,
                            "rationale": "x", "evidence": []}],
             "technical": [], "futures": []}
    book = {"legs": [{"symbol": "A", "side": "long", "target_notional": 1000.0, "rationale": ""}],
            "stated_deploy_frac": 0.9, "stated_dollar_residual_frac": 0.0,
            "stated_beta_residual": 0.0, "notes": ""}
    _seed_cycle(st, 1, marks={"A": 100.0, "BTC/USDT:USDT": 60000.0},
                betas={"A": 1.0, "BTC/USDT:USDT": 1.0}, reads=reads, book=book,
                adversary={"accept": True, "objections": [], "demanded_changes": []})
    # A rose 10% -> the long call was right; edge positive
    res = score_previous_cycle(st, mem, scored_cycle=1,
                               cur_marks={"A": 110.0, "BTC/USDT:USDT": 60000.0},
                               now="t", btc_symbol="BTC/USDT:USDT")
    assert res["scored_cycle"] == 1
    line = json.loads((Path(mem) / "scorecard.jsonl").read_text().splitlines()[0])
    assert line["cycle"] == 1
    assert line["specialists"]["sentiment"]["hit_rate"] == 1.0
    attribution = json.loads((cycle_dir(st, 1, cadence="rebal") / "attribution.json").read_text())
    assert attribution["book"]["gross_pnl"] == pytest.approx(100.0)  # 1000 * 0.10


def _role_file(tmp_path, role, hard_rule):
    p = tmp_path / "agents" / f"{role}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {role}\n\n## Hard rules\n- {hard_rule}\n\n{BEGIN}\n{END}\n\n## Output\nJSON\n")
    return p


def test_apply_reflection_edits_region_only(tmp_path):
    p = _role_file(tmp_path, "sentiment", "never invent a headline")
    proposal = {"edits": [{"role": "sentiment", "region_text": "- demand a 48h catalyst",
                           "reason": "miscalibrated", "evidence": ["c1"],
                           "retire_if": "hit>=0.5"}]}
    res = apply_reflection(proposal, tmp_path / "agents", tmp_path / "journal.md")
    assert res["applied"] == ["sentiment"] and res["skipped"] == []
    text = p.read_text()
    _pre, region, _suf = split_managed(text)
    assert "48h catalyst" in region
    assert "never invent a headline" in text            # hard rule intact
    assert (tmp_path / "journal.md").exists()


def test_active_managed_region_must_match_local_reflector_journal(tmp_path):
    agents = tmp_path / "agents"
    for role in ("sentiment", "technical", "futures", "pm", "adversary"):
        _role_file(tmp_path, role, "protected")
    journal = tmp_path / "journal.md"
    proposal = {
        "edits": [{
            "role": "futures",
            "region_text": "- [c2] cap conviction until recovery",
            "reason": "measured recurrence",
            "evidence": ["c1 edge=-0.01"],
            "retire_if": "edge >= 0 by c6",
        }]
    }
    assert apply_reflection(proposal, agents, journal)["applied"] == ["futures"]
    assert audit_managed_region_provenance(agents, journal) == []

    sentiment = agents / "sentiment.md"
    sentiment.write_text(
        sentiment.read_text().replace(
            f"{BEGIN}\n{END}",
            f"{BEGIN}\n- [c9] imported predecessor score\n{END}",
        )
    )
    assert audit_managed_region_provenance(agents, journal) == [
        "sentiment: active managed region is not backed by reflector-journal.md"
    ]


def test_apply_reflection_skips_oversize_region(tmp_path):
    _role_file(tmp_path, "pm", "Deploy >=90%")
    proposal = {"edits": [{"role": "pm", "region_text": "x" * 5000, "reason": "", "evidence": [],
                           "retire_if": ""}]}
    res = apply_reflection(proposal, tmp_path / "agents", tmp_path / "journal.md")
    assert res["applied"] == [] and res["skipped"][0][0] == "pm"


def test_apply_reflection_skips_unknown_role(tmp_path):
    proposal = {"edits": [{"role": "cfo", "region_text": "x", "reason": "", "evidence": [],
                           "retire_if": ""}]}
    res = apply_reflection(proposal, tmp_path / "agents", tmp_path / "journal.md")
    assert res["applied"] == [] and res["skipped"][0][0] == "cfo"


def test_scorecard_dedup_by_cycle_prevents_double_count(tmp_path):
    """A resume that re-scores the same cycle must not double-count in the K-gate."""
    st, mem = str(tmp_path / "st"), str(tmp_path / "mem")
    reads = {"sentiment": [{"symbol": "A", "lean": "long", "conviction": 0.9,
                            "rationale": "x", "evidence": []}], "technical": [], "futures": []}
    book = {"legs": [{"symbol": "A", "side": "long", "target_notional": 1000.0, "rationale": ""}]}
    # seed cycle 1 evidence/reads/book/adversary via _seed_cycle helper already in this file
    _seed_cycle(st, 1, marks={"A": 100.0, "BTC/USDT:USDT": 60000.0},
                betas={"A": 1.0, "BTC/USDT:USDT": 1.0}, reads=reads, book=book,
                adversary={"accept": True, "objections": [], "demanded_changes": []})
    # score cycle 1 THREE times against a DOWN move (A long that fell -> a "bad" record each time).
    # WITHOUT dedup, 3 identical cycle-1 bad records satisfy the K=3 gate and fire a
    # specialist_miscalibrated recurrence off ONE real cycle; dedup collapses them to 1 -> no fire.
    for _ in range(3):
        score_previous_cycle(st, mem, scored_cycle=1,
                             cur_marks={"A": 90.0, "BTC/USDT:USDT": 60000.0},
                             now="t", btc_symbol="BTC/USDT:USDT")
    lines = [ln for ln in (Path(mem) / "scorecard.jsonl").read_text().splitlines() if ln.strip()]
    assert len(lines) == 3                        # all appended (append-only log)
    recs = json.loads((Path(mem) / "pending" / "recurrences.json").read_text())
    # one real bad cycle duplicated 3x must NOT trip the K=3 gate
    assert not any(r["kind"] == "specialist_miscalibrated" for r in recs)


def test_score_previous_cycle_clears_stale_reflection(tmp_path):
    st, mem = str(tmp_path / "st"), str(tmp_path / "mem")
    pending = Path(mem) / "pending"
    pending.mkdir(parents=True)
    (pending / "reflection.json").write_text('{"edits": [{"role": "sentiment"}]}')
    score_previous_cycle(st, mem, scored_cycle=0, cur_marks={"A": 1.0}, now="t",
                         btc_symbol="BTC/USDT:USDT")
    assert not (pending / "reflection.json").exists()   # consume-once cleared it


def test_adv_revised_derived_from_verdict(tmp_path):
    st, mem = str(tmp_path / "st"), str(tmp_path / "mem")
    reads = {"sentiment": [], "technical": [], "futures": []}
    book = {"legs": [{"symbol": "A", "side": "long", "target_notional": 1000.0, "rationale": ""}]}
    _seed_cycle(st, 1, marks={"A": 100.0, "BTC/USDT:USDT": 60000.0},
                betas={"A": 1.0, "BTC/USDT:USDT": 1.0}, reads=reads, book=book,
                adversary={"accept": False, "objections": ["too concentrated"],
                           "demanded_changes": ["spread it"]})
    # NOTE: no book_original.json written — adv_revised must still be True from accept=False
    score_previous_cycle(st, mem, scored_cycle=1,
                         cur_marks={"A": 110.0, "BTC/USDT:USDT": 60000.0},
                         now="t", btc_symbol="BTC/USDT:USDT")
    rec = json.loads((cycle_dir(st, 1, cadence="rebal") / "attribution.json").read_text())
    assert rec["adv_revised"] is True
    assert rec["adv_accepted"] is False


def test_apply_reflection_skips_role_without_recurrence(tmp_path):
    p = _role_file(tmp_path, "sentiment", "never invent a headline")
    proposal = {"edits": [{"role": "sentiment", "region_text": "- note", "reason": "",
                           "evidence": [], "retire_if": ""}]}
    res = apply_reflection(proposal, tmp_path / "agents", tmp_path / "j.md",
                           allowed_roles={"technical"})   # sentiment NOT surfaced
    assert res["applied"] == []
    assert res["skipped"][0][0] == "sentiment"
    _pre, region, _suf = split_managed(p.read_text())
    assert region.strip() == ""                          # unchanged


def test_apply_reflection_dedupes_multiple_edits_per_role(tmp_path):
    p = _role_file(tmp_path, "sentiment", "never invent a headline")
    proposal = {"edits": [
        {"role": "sentiment", "region_text": "- first", "reason": "", "evidence": [],
         "retire_if": ""},
        {"role": "sentiment", "region_text": "- second", "reason": "", "evidence": [],
         "retire_if": ""}]}
    res = apply_reflection(proposal, tmp_path / "agents", tmp_path / "j.md",
                           allowed_roles={"sentiment"})
    assert res["applied"] == ["sentiment"]               # applied ONCE, not twice
    _pre, region, _suf = split_managed(p.read_text())
    assert "second" in region and "first" not in region  # last edit wins


def test_reconcile_wiring_persists_then_scores(tmp_path):
    """Mirror what desk_reconcile + desk_score do in sequence, offline: reconcile persists the
    evidence snapshot for cycle N; the next cycle's score reads it back."""
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    pending = Path(mem) / "pending"
    pending.mkdir(parents=True)
    ev1 = [{"symbol": "A", "mark": 100.0, "beta_btc": 1.0},
           {"symbol": "BTC/USDT:USDT", "mark": 60000.0, "beta_btc": 1.0}]
    persist_decision_snapshot(st, 1, evidence=ev1, pending_dir=pending)
    save_output(st, 1, "reads", {"sentiment": [], "technical": [], "futures": []},
                cadence="rebal")
    save_output(st, 1, "book", {"legs": []}, cadence="rebal")
    save_output(st, 1, "adversary", {"accept": True}, cadence="rebal")
    res = score_previous_cycle(st, mem, scored_cycle=1,
                               cur_marks={"A": 105.0, "BTC/USDT:USDT": 60000.0},
                               now="t", btc_symbol="BTC/USDT:USDT")
    assert res["scored_cycle"] == 1
