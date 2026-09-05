from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from futures_fund.prompt_guard import BEGIN, END
from futures_fund.reflection import (
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
    write_reflection_authority(
        state,
        memory,
        source_cycle=1,
        recurrences_sha256=(memory / "pending" / "1" / "recurrences.sha256").read_text().strip(),
        recurrences=json.loads((memory / "pending" / "1" / "recurrences.json").read_text()),
    )


def _args(agents, memory, *extra):
    return [
        "--memory-dir", str(memory),
        "--agents-dir", str(agents),
        "--state-dir", str(memory.parent / "live_state"),
        *extra,
    ]


def test_no_git_workspace_keeps_guarded_edit_and_journals(tmp_path, capsys):
    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    assert reflector_apply.main(_args(agents, memory)) == 0
    assert "use source diversity" in role.read_text()
    journal = memory / "reflector-journal.md"
    assert journal.exists()
    assert "source concentration" in journal.read_text()
    assert "journal-only (no Git worktree)" in capsys.readouterr().out


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


def test_post_apply_failure_restores_prompt_and_journal(tmp_path, monkeypatch):
    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    original = role.read_bytes()
    journal = memory / "reflector-journal.md"
    heads = memory / "reflector-heads-v1.json"
    anchor = memory.parent / "live_state" / "reflector-head-anchor-v1.json"
    journal_original = journal.read_bytes()
    heads_original = heads.read_bytes()
    anchor_original = anchor.read_bytes()
    apply_original = reflector_apply.apply_reflection

    def fail_after_writing(*args, **kwargs):
        apply_original(*args, **kwargs)
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(reflector_apply, "apply_reflection", fail_after_writing)
    assert reflector_apply.main(_args(agents, memory)) == 0
    assert role.read_bytes() == original
    assert journal.read_bytes() == journal_original
    assert heads.read_bytes() == heads_original
    assert anchor.read_bytes() == anchor_original


def test_handled_marker_failure_rolls_back_consumption_and_all_applied_state(
    tmp_path, monkeypatch
):
    agents, memory, role = _seed_reflection(tmp_path)
    _bootstrap(agents, memory)
    state = memory.parent / "live_state"
    journal = memory / "reflector-journal.md"
    heads = memory / "reflector-heads-v1.json"
    anchor = state / "reflector-head-anchor-v1.json"
    handled = memory / "recurrence-handled.json"
    consumption = reflection_authority_consumption_path(state, 1)
    before = {
        role: role.read_bytes(),
        journal: journal.read_bytes(),
        heads: heads.read_bytes(),
        anchor: anchor.read_bytes(),
    }
    assert not handled.exists()
    assert not consumption.exists()

    def fail_handled(*_args, **_kwargs):
        handled.write_text('{"partial": true}\n')
        raise RuntimeError("injected handled-marker failure")

    monkeypatch.setattr(reflector_apply, "mark_recurrences_handled", fail_handled)
    assert reflector_apply.main(_args(agents, memory)) == 0

    for path, content in before.items():
        assert path.read_bytes() == content
    assert not handled.exists()
    assert not consumption.exists()
    recovery = reflection_authority_recovery_status(state, memory, 1)
    assert recovery is not None
    assert recovery["status"] == "unconsumed"


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
