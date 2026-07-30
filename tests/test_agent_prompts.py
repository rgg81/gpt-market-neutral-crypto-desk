from pathlib import Path

import pytest

from futures_fund.prompt_guard import split_managed

ROLES = ["sentiment", "technical", "futures", "pm", "adversary"]
INVARIANTS = {
    "sentiment": "Never invent",
    "technical": "invent levels",
    "futures": "no invented figures",
    "pm": "Deploy 90-115% of cash",
    "adversary": "anti-hallucination",
}


@pytest.mark.parametrize("role", ROLES)
def test_agent_has_one_managed_block_with_invariant_outside(role):
    text = Path(f"agents/{role}.md").read_text()
    prefix, region, suffix = split_managed(text)  # raises if 0 or >1 block
    # The region may be empty (seeded) or populated (the reflector added calibration notes) — the
    # permanent invariant is that the role's HARD RULE stays OUTSIDE the managed region, never
    # inside it, so a reflector edit can never touch it. We only check that the invariant is present
    # OUTSIDE; mentions inside the region (e.g., in prose notes) are allowed and don't weaken it.
    outside = prefix + suffix
    assert INVARIANTS[role] in outside             # the hard rule is OUT of the region


@pytest.mark.parametrize("role", ["sentiment", "technical", "futures"])
def test_specialists_require_complete_cycle_coverage(role):
    text = Path(f"agents/{role}.md").read_text().lower()
    assert "exactly one object per input coin" in text
    assert "input order" in text


def test_pm_and_adversary_share_the_full_construction_contract():
    pm = Path("agents/pm.md").read_text()
    adversary = Path("agents/adversary.md").read_text()
    assert "seat_carry_bps" in pm
    assert "B1-B12" in pm
    assert "One net leg per symbol" in pm
    assert "ALL TWELVE" in adversary
    assert "each exactly once" in adversary
    assert "binding_user_directive" in pm
    assert "binding_user_directive" in adversary
    assert "0.98–1.02" in adversary
    assert "Returning an empty/cash book is forbidden" in pm


def test_sentiment_claims_are_reopened_and_adversary_audits_every_nonflat_url():
    sentiment = Path("agents/sentiment.md").read_text()
    adversary = Path("agents/adversary.md").read_text()
    orchestrator = Path("ops/desk-cycle-prompt.md").read_text()
    assert "Re-open every final URL" in sentiment
    assert "Never upgrade a proposal into an activation" in sentiment
    assert "price-analysis" in sentiment
    assert "every URL for every non-flat sentiment call" in adversary
    assert "citation_checks" in adversary
    assert "citation_checks must cover every non-flat sentiment symbol" in orchestrator


def test_live_managed_regions_are_backed_by_local_journal():
    from futures_fund.reflection import audit_managed_region_provenance

    assert audit_managed_region_provenance(
        "agents", "live_memory/reflector-journal.md"
    ) == []


def test_full_deployment_directive_contract_survives_its_one_shot_archive():
    pending = Path("ops/next-cycle-directive.md")
    applied = Path("live_memory/directives/applied/cycle-2.md")
    directive_path = pending if pending.exists() else applied
    assert directive_path.exists()
    if directive_path == applied:
        assert not pending.exists()  # successful cycle 2 consumed the one-shot directive
    directive = directive_path.read_text()
    orchestrator = Path("ops/desk-cycle-prompt.md").read_text()
    assert "$20,000 total gross notional" in directive
    assert "$10,000 long and $10,000 short" in directive
    assert "98–102%" in directive
    assert "may not return an empty book" in directive
    assert "B9 / the two-changed-leg limit is waived" in directive
    assert "B12 / the 10-cycle entry-payback limit may be overridden" in directive
    assert "B10 slippage cap" in directive and "B11" in directive
    assert "binding_user_directive" in orchestrator
    assert "archive it only after a successful reconcile" in orchestrator
    assert "On stand-down, HALT, or noncompliance" in orchestrator
    assert "validate the FINAL precheck" in orchestrator
    assert "noncompliant final revision HALTs before reconcile" in orchestrator


def test_reflector_resolves_the_per_cycle_pending_pointer():
    text = Path("agents/reflector.md").read_text()
    assert "pending/current.json" in text
    assert "that exact cycle dir" in text
