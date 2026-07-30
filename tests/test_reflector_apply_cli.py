from __future__ import annotations

import json

from futures_fund.prompt_guard import BEGIN, END
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
    pending = memory / "pending"
    pending.mkdir(parents=True)
    (pending / "meta.json").write_text('{"cycle": 1}')
    (pending / "recurrences.json").write_text('[{"role": "sentiment"}]')
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


def test_no_git_workspace_keeps_guarded_edit_and_journals(tmp_path, capsys):
    agents, memory, role = _seed_reflection(tmp_path)
    assert reflector_apply.main(
        ["--memory-dir", str(memory), "--agents-dir", str(agents)]
    ) == 0
    assert "use source diversity" in role.read_text()
    journal = memory / "reflector-journal.md"
    assert journal.exists()
    assert "source concentration" in journal.read_text()
    assert "journal-only (no Git worktree)" in capsys.readouterr().out


def test_post_apply_failure_restores_prompt_and_journal(tmp_path, monkeypatch):
    agents, memory, role = _seed_reflection(tmp_path)
    original = role.read_bytes()
    apply_original = reflector_apply.apply_reflection

    def fail_after_writing(*args, **kwargs):
        apply_original(*args, **kwargs)
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(reflector_apply, "apply_reflection", fail_after_writing)
    assert reflector_apply.main(
        ["--memory-dir", str(memory), "--agents-dir", str(agents)]
    ) == 0
    assert role.read_bytes() == original
    assert not (memory / "reflector-journal.md").exists()


def test_check_existing_fails_for_unjournaled_import_then_passes_after_apply(
    tmp_path, capsys
):
    agents, memory, role = _seed_reflection(tmp_path)
    assert reflector_apply.main(
        ["--memory-dir", str(memory), "--agents-dir", str(agents), "--check-existing"]
    ) == 1
    assert "not backed" in capsys.readouterr().out

    assert reflector_apply.main(
        ["--memory-dir", str(memory), "--agents-dir", str(agents)]
    ) == 0
    assert reflector_apply.main(
        ["--memory-dir", str(memory), "--agents-dir", str(agents), "--check-existing"]
    ) == 0
    assert "backed by the local journal" in capsys.readouterr().out
