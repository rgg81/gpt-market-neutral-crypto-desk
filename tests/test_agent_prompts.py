from pathlib import Path


def _text(path: str) -> str:
    return Path(path).read_text()


def _normalized(path: str) -> str:
    return " ".join(_text(path).split())


def test_weight_roles_are_compact_and_cover_the_exact_agent_chain():
    alpha = _text("agents/allocator-alpha.md")
    risk = _text("agents/allocator-risk.md")
    pm = _text("agents/weight-pm.md")
    adversary = _text("agents/weight-adversary.md")
    revision = _text("agents/weight-pm-revision.md")

    assert sum(len(text.split()) for text in (alpha, risk, pm, adversary)) < 1_200
    assert len(revision.split()) < 300
    assert '"role": "alpha_allocator"' in alpha
    assert '"role": "risk_allocator"' in risk
    assert '"role": "pm"' in pm
    assert "allocation_adversary.json" in adversary
    assert 'role `pm_revision`' in revision


def test_allocators_cannot_select_symbols_sides_or_cash():
    for path in ("agents/allocator-alpha.md", "agents/allocator-risk.md"):
        prompt = _text(path).lower()
        assert "immutable" in prompt
        assert "all 20" in prompt or "every packet asset" in prompt
        assert "each side must sum" in prompt
        assert "weight" in prompt and "min/max" in prompt
        assert "do not browse" in prompt or "no web" in prompt
    assert "may not question the selected symbols or sides" in _text(
        "agents/allocator-risk.md"
    )
    assert "may not choose cash" in _text("agents/allocator-risk.md")


def test_pm_binds_both_proposals_and_resolves_only_weights():
    pm = _text("agents/weight-pm.md")
    assert "allocator_digest.json" in pm
    assert '"alpha_allocator": "allocator_digest.proposal_sha256.alpha_allocator"' in pm
    assert '"risk_allocator": "allocator_digest.proposal_sha256.risk_allocator"' in pm
    assert "symbols and sides cannot change" in pm
    assert "Do not choose cash" in pm
    assert "turnover" in pm and "funding" in pm and "volatility" in pm


def test_adversary_has_one_bound_revision_and_no_symbol_authority():
    adversary = _text("agents/weight-adversary.md")
    revision = _text("agents/weight-pm-revision.md")
    runbook = _normalized("docs/desk-cycle-runbook.md")
    assert "reject only a material sizing\ndefect" in adversary.lower()
    assert "may not add/remove/flip symbols" in adversary
    assert "max_weight_by_symbol" in adversary
    assert "max_turnover_frac_equity" in adversary
    assert "exactly one PM revision" in runbook
    assert "There is no second adversarial pass" in runbook
    assert "revision_digest.source_proposal_sha256" in revision


def test_runbook_is_exact_compact_production_path():
    runbook = _text("docs/desk-cycle-runbook.md")
    normalized = _normalized("docs/desk-cycle-runbook.md")
    required_in_order = (
        "scripts/ensure_binance_proxy.py",
        "scripts/desk_data_preflight.py",
        "scripts/desk_recover.py",
        "scripts/desk_watchdog.py",
        "scripts/desk_cross_section_prepare.py",
        "scripts/desk_cross_section_validate.py alpha",
        "scripts/desk_cross_section_validate.py risk",
        "scripts/desk_cross_section_validate.py consensus-input",
        "scripts/desk_cross_section_validate.py pm",
        "scripts/desk_cross_section_validate.py precheck",
        "scripts/desk_cross_section_validate.py adversary",
        "scripts/desk_cross_section_validate.py finalize",
        "scripts/desk_cross_section_reconcile.py",
        "scripts/desk_status_performance.py",
    )
    positions = [normalized.index(item) for item in required_in_order]
    assert positions == sorted(positions)
    assert "180 completed UTC days" in runbook
    assert "best ten funding-adjusted returns as longs" in runbook
    assert "worst ten as shorts" in runbook
    assert "85–115%" in runbook
    assert "at most 2% dollar residual" in runbook


def test_ops_prompt_delegates_to_complete_runbook_without_legacy_agents():
    ops = _text("ops/desk-cycle-prompt.md")
    assert "all of `docs/desk-cycle-runbook.md`" in ops
    assert "Spawn Alpha Allocator and Risk Allocator concurrently" in ops
    assert "Weight PM" in ops and "Weight\nAdversary" in ops
    assert "Do not browse the web" in ops
    for legacy in ("desk_evidence.py", "desk_score.py", "desk_precheck.py", "desk_reconcile.py"):
        assert legacy not in ops


def test_mission_defines_price_plus_actual_funding_ranking():
    mission = _text("MISSION.md")
    assert "quote-asset volume over the latest 180 completed UTC days" in mission
    assert "price return - Σ(funding rate × settlement mark / starting price)" in mission
    assert "ten best as longs and the ten worst as shorts" in mission
    assert "Every selected name is held" in mission
    assert (
        "daily decision loop may revise weights, but never the weekly symbols or sides"
        in mission
    )
    assert "Profit is the objective, not a guarantee" in mission


def test_runtime_identity_is_gpt_sol_xhigh_paper_only():
    documents = [_text(path) for path in ("AGENTS.md", "MISSION.md", "docs/desk-cycle-runbook.md")]
    for text in documents:
        assert "gpt-5.6-sol" in text
        assert "xhigh" in text
        assert "PAPER" in text
    assert "OPENAI_API_KEY" in documents[0]
    assert "live" in documents[0].lower() and "false" in documents[0].lower()
