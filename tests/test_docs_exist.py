from __future__ import annotations

from pathlib import Path


def test_readme_exists_and_describes_the_llm_desk():
    text = Path("README.md").read_text()
    low = text.lower()
    assert "desk_evidence.py" in text, "README must show how to run a cycle (desk_evidence.py)"
    assert "paper" in low, "README must state the desk is paper-only"
    assert "neutral" in low, "README must state the dollar+beta-neutral mandate"
    assert "adversary" in low, "README must describe the adversary challenge"


def test_agents_md_exists_with_operating_rules():
    text = Path("AGENTS.md").read_text()
    low = text.lower()
    assert "live" in low and "false" in low, "AGENTS.md must affirm live=false"
    assert "paper only" in low, "AGENTS.md must affirm PAPER ONLY"
    assert "adversary" in low, "AGENTS.md must state the Adversary agent is the sole veto"
    assert "subscription" in low, "AGENTS.md must state the subscription-only rule"
    assert "gpt-5.6-sol" in low and "xhigh" in low
