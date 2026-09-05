from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from futures_fund.prompt_guard import BEGIN, END
from futures_fund.reflection import (
    reflection_apply_transaction_path,
    reflection_authority_consumption_path,
    reflection_authority_recovery_status,
    write_reflection_authority,
)
from scripts import reflector_apply


def _seed_reflection(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    role = agents / "sentiment.md"
    role.write_text(f"protected before\n{BEGIN}\nold lesson\n{END}\nprotected after\n")
    for other in ("technical", "futures", "pm", "adversary"):
        (agents / f"{other}.md").write_text(
            f"protected before\n{BEGIN}\n{END}\nprotected after\n"
        )

    memory = tmp_path / "live_memory"
    pending_root = memory / "pending"
    pending = pending_root / "1"
    pending.mkdir(parents=True)
    (pending_root / "current.json").write_text(json.dumps({
        "cycle": 1,
        "dir": str(pending.resolve()),
    }))
    (memory / "reflector-journal.md").write_text(
        "## sentiment — audited legacy seed\n"
        "- retire_if:\n"
        "- evidence: test fixture\n"
        "- region:\nold lesson\n\n"
    )
    (pending / "meta.json").write_text(json.dumps({
        "cycle": 1,
        "now": datetime.now(UTC).isoformat(),
    }))
    recurrences = [{
        "kind": "test_recurrence",
        "role": "sentiment",
        "count": 1,
        "window": 1,
        "evidence": [],
        "suggestion": "test",
    }]
    (pending / "recurrences.json").write_text(json.dumps(recurrences))
    recurrence_digest = sha256(json.dumps(
        recurrences, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    (pending / "recurrences.sha256").write_text(recurrence_digest + "\n")
    (pending / "reflection.json").write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "role": "sentiment",
                        "region_text": "- use source diversity [c1]",
                        "reason": "recurring source concentration",
                        "evidence": [],
                        "retire_if": "resolved by c4",
                    }
                ]
            }
        )
    )
    return agents, memory, role


def _bootstrap(agents, memory):
    state = memory.parent / "live_state"
    assert reflector_apply.main([
        "--memory-dir", str(memory),
        "--agents-dir", str(agents),
        "--state-dir", str(state),
        "--bootstrap-heads",
    ]) == 0
    pointer = json.loads((memory / "pending" / "current.json").read_text())
    source_cycle = int(pointer["cycle"])
    pending = memory / "pending" / str(source_cycle)
    write_reflection_authority(
        state,
        memory,
        source_cycle=source_cycle,
        recurrences_sha256=(pending / "recurrences.sha256").read_text().strip(),
        recurrences=json.loads((pending / "recurrences.json").read_text()),
    )


def _replace_pending(
    memory, *, cycle: int, recurrences: list[dict], proposal: dict
):
    pending_root = memory / "pending"
    old = pending_root / "1"
    pending = pending_root / str(cycle)
    old.rename(pending)
    meta = json.loads((pending / "meta.json").read_text())
    meta["cycle"] = cycle
    meta["now"] = datetime.now(UTC).isoformat()
    (pending / "meta.json").write_text(json.dumps(meta))
    (pending_root / "current.json").write_text(
        json.dumps({"cycle": cycle, "dir": str(pending.resolve())})
    )
    (pending / "recurrences.json").write_text(json.dumps(recurrences))
    recurrence_digest = sha256(
        json.dumps(recurrences, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (pending / "recurrences.sha256").write_text(recurrence_digest + "\n")
    (pending / "reflection.json").write_text(json.dumps(proposal))
    return pending


def _args(agents, memory, *extra):
    return [
        "--memory-dir", str(memory),
        "--agents-dir", str(agents),
        "--state-dir", str(memory.parent / "live_state"),
        *extra,
    ]


def _file_identity(path):
    metadata = path.stat()
    return path.read_bytes(), metadata.st_ino, metadata.st_mtime_ns


def test_no_git_workspace_keeps_guarded_edit_and_journals(tmp_path, capsys):
    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    assert reflector_apply.main(_args(agents, memory)) == 0
    assert "use source diversity" in role.read_text()
    journal = memory / "reflector-journal.md"
    assert journal.exists()
    assert "source concentration" in journal.read_text()
    assert "journal-only (no Git worktree)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("write_target", "forward_complete"),
    [
        ("sentiment", False),
        ("pm", False),
        ("journal", False),
        ("heads", False),
        ("anchor", True),
    ],
)
def test_apply_transaction_recovers_each_durable_write_boundary(
    tmp_path, monkeypatch, write_target, forward_complete
):
    import futures_fund.reflection as reflection

    agents, memory, _role = _seed_reflection(tmp_path)
    recurrences = [
        {
            "kind": "test_recurrence",
            "role": role,
            "count": 1,
            "window": 1,
            "evidence": [],
            "suggestion": "test",
        }
        for role in ("sentiment", "pm")
    ]
    proposal = {
        "edits": [
            {
                "role": "sentiment",
                "region_text": "- [c2] transaction sentiment",
                "reason": "transaction boundary",
                "evidence": [],
                "retire_if": "",
            },
            {
                "role": "pm",
                "region_text": "- [c2] transaction PM",
                "reason": "transaction boundary",
                "evidence": [],
                "retire_if": "",
            },
        ]
    }
    _replace_pending(memory, cycle=2, recurrences=recurrences, proposal=proposal)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    journal = memory / "reflector-journal.md"
    heads = memory / "reflector-heads-v1.json"
    anchor = state / "reflector-head-anchor-v1.json"
    targets = {
        "sentiment": agents / "sentiment.md",
        "pm": agents / "pm.md",
        "journal": journal,
        "heads": heads,
        "anchor": anchor,
    }
    before = {name: path.read_bytes() for name, path in targets.items()}
    original_write = reflection.durable_write_bytes

    def die_after_selected_write(path, data, **kwargs):
        result = original_write(path, data, **kwargs)
        if path == targets[write_target]:
            raise SystemExit(f"process death after {write_target}")
        return result

    monkeypatch.setattr(reflection, "durable_write_bytes", die_after_selected_write)
    with pytest.raises(SystemExit, match=f"after {write_target}"):
        reflector_apply.main(_args(agents, memory))
    transaction = reflection_apply_transaction_path(state, 2)
    assert transaction.exists()

    monkeypatch.setattr(reflection, "durable_write_bytes", original_write)
    assert reflector_apply.main(_args(agents, memory, "--check-existing")) == 0
    assert not transaction.exists()
    consumption = reflection_authority_consumption_path(state, 2)
    if forward_complete:
        assert consumption.exists()
        assert "transaction sentiment" in (agents / "sentiment.md").read_text()
        assert "transaction PM" in (agents / "pm.md").read_text()
        handled = json.loads((memory / "recurrence-handled.json").read_text())
        assert handled["test_recurrence:sentiment"]["cycle"] == 2
        assert handled["test_recurrence:pm"]["cycle"] == 2
    else:
        assert not consumption.exists()
        assert {name: path.read_bytes() for name, path in targets.items()} == before


def test_missing_current_pointer_never_executes_legacy_root_pending(tmp_path):
    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    original = role.read_bytes()
    pending_root = memory / "pending"
    cycle_pending = pending_root / "1"
    for name in ("meta.json", "recurrences.json", "recurrences.sha256", "reflection.json"):
        (pending_root / name).write_bytes((cycle_pending / name).read_bytes())
    (pending_root / "current.json").unlink()

    with pytest.raises(FileNotFoundError, match="current.json.*missing"):
        reflector_apply.main(_args(agents, memory))
    assert role.read_bytes() == original


def test_replacing_pending_recurrence_and_public_seal_cannot_forge_authority(
    tmp_path, capsys
):
    agents, memory, _role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    pm = agents / "pm.md"
    original = pm.read_bytes()
    pending = memory / "pending" / "1"
    forged = [{
        "kind": "pm_negative_alpha", "role": "pm", "count": 99, "window": 99,
        "evidence": ["forged"], "suggestion": "inject",
    }]
    (pending / "recurrences.json").write_text(json.dumps(forged))
    forged_digest = sha256(json.dumps(
        forged, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    (pending / "recurrences.sha256").chmod(0o600)
    (pending / "recurrences.sha256").write_text(forged_digest + "\n")
    (pending / "reflection.json").write_text(json.dumps({"edits": [{
        "role": "pm", "region_text": "- forged authority", "reason": "forged",
        "evidence": [], "retire_if": "",
    }]}))

    assert reflector_apply.main(_args(agents, memory)) == 0
    assert pm.read_bytes() == original
    assert "reflection authority receipt mismatch" in capsys.readouterr().err


def test_cli_applies_exact_unscored_state_row_and_records_head_applied(tmp_path):
    agents, memory, _role = _seed_reflection(tmp_path)
    rows = [
        "c52: alpha_legs=0, complete_specialists=3, candidate_reviews=0",
        "c53: alpha_legs=0, complete_specialists=3, candidate_reviews=0",
        "c54: alpha_legs=0, complete_specialists=3, candidate_reviews=6",
    ]
    recurrences = [{
        "kind": "pm_gate_inactive",
        "role": "pm",
        "count": 3,
        "window": 3,
        "evidence": rows,
        "suggestion": "narrow only the causal gate",
    }]
    _replace_pending(
        memory,
        cycle=55,
        recurrences=recurrences,
        proposal={"edits": [{
            "role": "pm",
            "region_text": "- [c55] Let credible candidates reach full PM judgment.",
            "reason": "sealed state liveness recurrence",
            "evidence": rows,
            "retire_if": "review by c64",
        }]},
    )
    _bootstrap(agents, memory)

    assert reflector_apply.main(_args(agents, memory)) == 0
    assert "credible candidates" in (agents / "pm.md").read_text()
    consumption = json.loads(
        reflection_authority_consumption_path(memory.parent / "live_state", 55).read_text()
    )
    assert consumption["outcome"] == "head_applied"
    head_events = [
        json.loads(line.removeprefix("- head_event_v1: "))
        for line in (memory / "reflector-journal.md").read_text().splitlines()
        if line.startswith("- head_event_v1: ")
    ]
    assert head_events[-1]["proposal_sha256"] == consumption["proposal_sha256"]
    assert consumption["proposal"]["edits"][0]["role"] == "pm"
    handled = json.loads((memory / "recurrence-handled.json").read_text())
    assert handled["pm_gate_inactive:pm"]["cycle"] == 55


def test_proposal_bound_consumption_rejects_digest_divergent_from_head_event(tmp_path):
    agents, memory, _role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    assert reflector_apply.main(_args(agents, memory)) == 0
    path = reflection_authority_consumption_path(state, 1)
    consumption = json.loads(path.read_text())
    consumption["proposal"]["edits"][0]["reason"] = "rewritten after application"
    consumption["proposal_sha256"] = sha256(
        json.dumps(
            consumption["proposal"], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    payload = {
        key: value for key, value in consumption.items() if key != "consumption_sha256"
    }
    consumption["consumption_sha256"] = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.chmod(0o600)
    path.write_text(json.dumps(consumption, indent=2, sort_keys=True) + "\n")

    with pytest.raises(ValueError, match="poststate is invalid"):
        reflection_authority_recovery_status(state, memory, 1, agents_dir=agents)


def test_cli_refuses_bad_state_citation_without_consuming_or_mutating(tmp_path, capsys):
    agents, memory, _role = _seed_reflection(tmp_path)
    rows = [
        "c52: alpha_legs=0, complete_specialists=3",
        "c53: alpha_legs=0, complete_specialists=3",
        "c54: alpha_legs=0, complete_specialists=3",
    ]
    recurrences = [{
        "kind": "pm_gate_inactive",
        "role": "pm",
        "count": 3,
        "window": 3,
        "evidence": rows,
        "suggestion": "narrow only the causal gate",
    }]
    _replace_pending(
        memory,
        cycle=55,
        recurrences=recurrences,
        proposal={"edits": [{
            "role": "pm",
            "region_text": "- [c55] governed update",
            "reason": "c54 realized_edge=-10%",
            "evidence": rows,
            "retire_if": "review by c64",
        }]},
    )
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    watched = [
        agents / "pm.md",
        memory / "reflector-journal.md",
        memory / "reflector-heads-v1.json",
        state / "reflector-head-anchor-v1.json",
    ]
    before = {path: path.read_bytes() for path in watched}

    assert reflector_apply.main(_args(agents, memory)) == 0
    assert {path: path.read_bytes() for path in watched} == before
    assert not reflection_authority_consumption_path(state, 55).exists()
    assert not (memory / "recurrence-handled.json").exists()
    assert reflection_authority_recovery_status(state, memory, 55)["status"] == "unconsumed"
    assert "cites past cycle(s) [54]" in capsys.readouterr().err


def test_cli_no_action_authenticates_actual_packet_before_cooldown(tmp_path, capsys):
    agents, memory, _role = _seed_reflection(tmp_path)
    original = [{
        "kind": "pm_gate_inactive",
        "role": "pm",
        "count": 3,
        "window": 3,
        "evidence": ["c52 bound", "c53 bound", "c54 bound"],
        "suggestion": "review gate",
    }]
    pending = _replace_pending(
        memory,
        cycle=55,
        recurrences=original,
        proposal={"edits": [], "no_action_reason": "already addressed"},
    )
    _bootstrap(agents, memory)
    tampered = [{**original[0], "evidence": ["c52 forged", "c53 forged", "c54 forged"]}]
    (pending / "recurrences.json").write_text(json.dumps(tampered))
    state = memory.parent / "live_state"

    assert reflector_apply.main(_args(agents, memory)) == 0
    assert not reflection_authority_consumption_path(state, 55).exists()
    assert not (memory / "recurrence-handled.json").exists()
    assert reflection_authority_recovery_status(state, memory, 55)["status"] == "unconsumed"
    assert "does not match its desk_score seal" in capsys.readouterr().err


def test_no_action_consumption_retains_reason_after_pending_proposal_deletion(tmp_path):
    from scripts import desk_score

    agents, memory, _role = _seed_reflection(tmp_path)
    pending = memory / "pending" / "1"
    reason = "the active sentiment calibration already covers this exact recurrence"
    (pending / "reflection.json").write_text(
        json.dumps({"edits": [], "no_action_reason": reason})
    )
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"

    assert reflector_apply.main(_args(agents, memory)) == 0
    consumption_path = reflection_authority_consumption_path(state, 1)
    before = json.loads(consumption_path.read_text())
    assert before["no_action_reason"] == reason
    assert before["omitted_roles"] == ["sentiment"]
    assert before["proposal"]["no_action_reason"] == reason

    recovered = desk_score._recover_reflection_authority_packet(
        str(state), str(memory), pending, 1, agents_dir=agents
    )
    assert recovered is not None and recovered["reflection_authority_recovery"] == "consumed"
    assert not (pending / "reflection.json").exists()
    assert json.loads(consumption_path.read_text()) == before


def test_consumed_no_action_is_single_use_and_exact_replay_is_read_only(
    tmp_path, capsys
):
    agents, memory, role = _seed_reflection(tmp_path)
    pending = memory / "pending" / "1"
    no_action = {"edits": [], "no_action_reason": "the active note already covers it"}
    (pending / "reflection.json").write_text(json.dumps(no_action))
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"

    assert reflector_apply.main(_args(agents, memory)) == 0
    capsys.readouterr()
    watched = [
        role,
        memory / "reflector-journal.md",
        memory / "reflector-heads-v1.json",
        memory / "recurrence-handled.json",
        state / "reflector-head-anchor-v1.json",
        reflection_authority_consumption_path(state, 1),
    ]
    exact_state = {path: _file_identity(path) for path in watched}

    # The exact canonical decision is an idempotent retry, not a second consideration.
    assert reflector_apply.main(_args(agents, memory)) == 0
    replay = capsys.readouterr()
    assert '"already_consumed": true' in replay.out
    assert {path: _file_identity(path) for path in watched} == exact_state
    assert not reflection_apply_transaction_path(state, 1).exists()

    # Reusing the same no-head authority for a different proposal used to mutate the prompts and
    # then brick recovery against the immutable no-action receipt. It must now fail before writes.
    (pending / "reflection.json").write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "role": "sentiment",
                        "region_text": "- altered after consumption",
                        "reason": "retry",
                        "evidence": [],
                        "retire_if": "",
                    }
                ]
            }
        )
    )
    before_divergent = {path: _file_identity(path) for path in watched}
    assert reflector_apply.main(_args(agents, memory)) == 0
    refusal = capsys.readouterr()
    assert "already consumed by a different canonical proposal" in refusal.err
    assert {path: _file_identity(path) for path in watched} == before_divergent
    assert not reflection_apply_transaction_path(state, 1).exists()


def test_probe_existing_reports_transaction_without_recovering_or_writing(
    tmp_path, monkeypatch, capsys
):
    import futures_fund.reflection as reflection

    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    original_write = reflection.durable_write_bytes

    def die_after_prompt(path, data, **kwargs):
        result = original_write(path, data, **kwargs)
        if path == role:
            raise SystemExit("process death after prompt")
        return result

    monkeypatch.setattr(reflection, "durable_write_bytes", die_after_prompt)
    with pytest.raises(SystemExit, match="after prompt"):
        reflector_apply.main(_args(agents, memory))
    monkeypatch.setattr(reflection, "durable_write_bytes", original_write)
    transaction = reflection_apply_transaction_path(state, 1)
    assert transaction.exists()
    watched = [
        role,
        memory / "reflector-journal.md",
        memory / "reflector-heads-v1.json",
        state / "reflector-head-anchor-v1.json",
        transaction,
    ]
    before = {path: _file_identity(path) for path in watched}

    assert reflector_apply.main(_args(agents, memory, "--probe-existing")) == 1
    output = capsys.readouterr().out
    assert '"read_only": true' in output
    assert "pending reflection apply recovery: cycle-1-apply-transaction.json" in output
    assert {path: _file_identity(path) for path in watched} == before
    assert not reflection_authority_consumption_path(state, 1).exists()


def test_recovery_preserves_divergent_target_and_intent(tmp_path, monkeypatch, capsys):
    import futures_fund.reflection as reflection

    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    original_write = reflection.durable_write_bytes

    def die_after_prompt(path, data, **kwargs):
        result = original_write(path, data, **kwargs)
        if path == role:
            raise SystemExit("process death after prompt")
        return result

    monkeypatch.setattr(reflection, "durable_write_bytes", die_after_prompt)
    with pytest.raises(SystemExit, match="after prompt"):
        reflector_apply.main(_args(agents, memory))
    monkeypatch.setattr(reflection, "durable_write_bytes", original_write)
    transaction = reflection_apply_transaction_path(state, 1)
    assert transaction.exists()

    # Atomic transaction writes can leave only exact pre/post bytes. A third state is external
    # divergence and must be preserved for diagnosis instead of silently overwritten by rollback.
    role.write_bytes(b"foreign protected change\n" + role.read_bytes())
    watched = [
        role,
        memory / "reflector-journal.md",
        memory / "reflector-heads-v1.json",
        state / "reflector-head-anchor-v1.json",
        transaction,
    ]
    before = {path: _file_identity(path) for path in watched}

    assert reflector_apply.main(_args(agents, memory, "--check-existing")) == 1
    output = capsys.readouterr().out
    assert "divergent target(s); refusing rollback: prompt:sentiment" in output
    assert {path: _file_identity(path) for path in watched} == before
    assert transaction.exists()
    assert not reflection_authority_consumption_path(state, 1).exists()


def test_probe_existing_reports_consumed_but_unhandled_without_repairing(
    tmp_path, capsys
):
    from futures_fund.reflection import write_reflection_authority_consumption

    agents, memory, _role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    pending = memory / "pending" / "1"
    reason = "the active note already covers this recurrence"
    write_reflection_authority_consumption(
        state,
        memory,
        source_cycle=1,
        recurrences_sha256=(pending / "recurrences.sha256").read_text().strip(),
        outcome="no_head_change",
        proposal={"edits": [], "no_action_reason": reason},
        agents_dir=agents,
    )
    consumption = reflection_authority_consumption_path(state, 1)
    before = _file_identity(consumption)

    assert reflector_apply.main(_args(agents, memory, "--probe-existing")) == 1
    output = capsys.readouterr().out
    assert "consumed reflection cycle 1 has unhandled recurrence(s)" in output
    assert "test_recurrence:sentiment" in output
    assert not (memory / "recurrence-handled.json").exists()
    assert _file_identity(consumption) == before


def test_post_apply_process_failure_forward_completes_verified_poststate(
    tmp_path, monkeypatch, capsys
):
    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    original = role.read_bytes()
    journal = memory / "reflector-journal.md"
    heads = memory / "reflector-heads-v1.json"
    anchor = memory.parent / "live_state" / "reflector-head-anchor-v1.json"
    state = memory.parent / "live_state"
    journal_original = journal.read_bytes()
    heads_original = heads.read_bytes()
    anchor_original = anchor.read_bytes()
    apply_original = reflector_apply.apply_reflection

    def fail_after_writing(*args, **kwargs):
        apply_original(*args, **kwargs)
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(reflector_apply, "apply_reflection", fail_after_writing)
    assert reflector_apply.main(_args(agents, memory)) == 0
    assert role.read_bytes() != original
    assert journal.read_bytes() != journal_original
    assert heads.read_bytes() != heads_original
    assert anchor.read_bytes() != anchor_original
    consumption = reflection_authority_consumption_path(state, 1)
    assert json.loads(consumption.read_text())["outcome"] == "head_applied"
    assert json.loads((memory / "recurrence-handled.json").read_text())[
        "test_recurrence:sentiment"
    ]["cycle"] == 1
    error = capsys.readouterr().err
    assert "reflector_apply failed (fail-soft); transaction recovered" in error
    assert "recovery remains pending" not in error


def test_post_apply_failure_truthfully_reports_pending_recovery(
    tmp_path, monkeypatch, capsys
):
    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    apply_original = reflector_apply.apply_reflection

    def fail_after_writing_and_diverge(*args, **kwargs):
        apply_original(*args, **kwargs)
        role.write_bytes(b"foreign protected change\n" + role.read_bytes())
        raise RuntimeError("simulated post-apply failure with divergence")

    monkeypatch.setattr(
        reflector_apply, "apply_reflection", fail_after_writing_and_diverge
    )
    assert reflector_apply.main(_args(agents, memory)) == 0
    error = capsys.readouterr().err
    assert "reflector_apply recovery also failed" in error
    assert "reflector_apply failed (fail-soft); recovery remains pending" in error
    assert "transaction recovered" not in error
    assert reflection_apply_transaction_path(state, 1).exists()
    assert not reflection_authority_consumption_path(state, 1).exists()


def test_consumed_poststate_recovers_handled_marker_after_process_death(
    tmp_path, monkeypatch
):
    import futures_fund.reflection as reflection

    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    journal = memory / "reflector-journal.md"
    heads = memory / "reflector-heads-v1.json"
    anchor = state / "reflector-head-anchor-v1.json"
    handled = memory / "recurrence-handled.json"
    consumption = reflection_authority_consumption_path(state, 1)
    before = {path: path.read_bytes() for path in (role, journal, heads, anchor)}
    assert not handled.exists()
    assert not consumption.exists()

    original_mark = reflection.mark_recurrences_handled

    def die_before_handled(*_args, **_kwargs):
        raise SystemExit("simulated process death before handled marker")

    monkeypatch.setattr(reflection, "mark_recurrences_handled", die_before_handled)
    with pytest.raises(SystemExit, match="process death"):
        reflector_apply.main(_args(agents, memory))

    assert all(path.read_bytes() != content for path, content in before.items())
    assert not handled.exists()
    assert consumption.exists()
    assert reflection_apply_transaction_path(state, 1).exists()

    monkeypatch.setattr(reflection, "mark_recurrences_handled", original_mark)
    assert reflector_apply.main(_args(agents, memory, "--check-existing")) == 0
    assert not reflection_apply_transaction_path(state, 1).exists()
    assert json.loads(handled.read_text())["test_recurrence:sentiment"]["cycle"] == 1
    recovery = reflection_authority_recovery_status(
        state, memory, 1, agents_dir=agents
    )
    assert recovery is not None and recovery["status"] == "consumed"


def test_check_existing_requires_explicit_bootstrap_then_tracks_latest_head(
    tmp_path, capsys
):
    agents, memory, role = _seed_reflection(tmp_path)
    assert reflector_apply.main(_args(agents, memory, "--check-existing")) == 1
    assert "--bootstrap-heads" in capsys.readouterr().out

    _bootstrap(agents, memory)
    assert reflector_apply.main(_args(agents, memory, "--check-existing")) == 0
    assert "exact v1 latest head" in capsys.readouterr().out

    assert reflector_apply.main(_args(agents, memory)) == 0
    assert reflector_apply.main(_args(agents, memory, "--check-existing")) == 0
    assert "exact v1 latest head" in capsys.readouterr().out

    state = memory.parent / "live_state"
    watched = [
        role,
        memory / "reflector-journal.md",
        memory / "reflector-heads-v1.json",
        memory / "recurrence-handled.json",
        state / "reflector-head-anchor-v1.json",
        reflection_authority_consumption_path(state, 1),
    ]
    before = {path: _file_identity(path) for path in watched}
    assert reflector_apply.main(_args(agents, memory, "--probe-existing")) == 0
    assert '"managed_region_probe": "OK"' in capsys.readouterr().out
    assert {path: _file_identity(path) for path in watched} == before
