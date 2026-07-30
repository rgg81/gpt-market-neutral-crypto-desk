import pytest
from pydantic import ValidationError

from futures_fund.desk_contracts import AdversaryVerdict, Book, BookLeg, SpecialistRead


def test_specialist_read_bounds_conviction():
    SpecialistRead(symbol="SOL/USDT:USDT", lean="long", conviction=0.8, rationale="x", evidence=[])
    with pytest.raises(ValidationError):
        SpecialistRead(symbol="X", lean="long", conviction=1.5, rationale="x", evidence=[])
    with pytest.raises(ValidationError):
        SpecialistRead(symbol="X", lean="sideways", conviction=0.5, rationale="x", evidence=[])


def test_book_requires_positive_notional_legs():
    leg = BookLeg(symbol="SOL/USDT:USDT", side="long", target_notional=1000.0, rationale="r")
    b = Book(legs=[leg], stated_deploy_frac=0.9, stated_dollar_residual_frac=0.0,
             stated_beta_residual=0.0)
    assert b.legs[0].side == "long"
    with pytest.raises(ValidationError):
        BookLeg(symbol="X", side="long", target_notional=0.0, rationale="r")
    with pytest.raises(ValidationError):
        BookLeg(symbol="X", side="long", target_notional=float("inf"), rationale="r")


def test_adversary_verdict_roundtrips():
    from tests.conftest import make_verdict
    v = make_verdict(False, objections=["thin evidence on X"], demanded_changes=["drop X"])
    assert v.accept is False and v.objections
    assert len(v.bounds_confirmed) == 12


def test_bare_accept_no_longer_validates():
    """The cycle-4 regression: the literal 66-byte bare accept must FAIL schema validation."""
    with pytest.raises(ValidationError):
        AdversaryVerdict.model_validate(
            {"accept": True, "objections": [], "demanded_changes": []})


def test_adversary_requires_exact_bound_set_and_decision_reasoning():
    from tests.conftest import make_verdict

    duplicate = make_verdict().model_dump()
    duplicate["bounds_confirmed"][-1]["bound_id"] = "B11"
    with pytest.raises(ValidationError, match="exactly once"):
        AdversaryVerdict.model_validate(duplicate)

    failing_accept = make_verdict().model_dump()
    failing_accept["bounds_confirmed"][0]["ok"] = False
    with pytest.raises(ValidationError, match="override_rationale"):
        AdversaryVerdict.model_validate(failing_accept)

    incomplete_reject = make_verdict().model_dump()
    incomplete_reject["accept"] = False
    with pytest.raises(ValidationError, match="objection"):
        AdversaryVerdict.model_validate(incomplete_reject)


def test_reflector_edit_roundtrip():
    from futures_fund.desk_contracts import ReflectionProposal, ReflectorEdit

    e = ReflectorEdit(
        role="sentiment",
        region_text="- demand a 48h catalyst",
        reason="miscalibrated",
        evidence=["c1 edge=-0.02"],
        retire_if="hit>=0.5 by c9",
    )
    p = ReflectionProposal(edits=[e])
    dumped = p.model_dump()
    assert ReflectionProposal.model_validate(dumped).edits[0].role == "sentiment"


def test_reflection_proposal_defaults_empty():
    from futures_fund.desk_contracts import ReflectionProposal

    p = ReflectionProposal()
    assert p.edits == [] and p.no_action_reason == ""


def test_reflector_edit_rejects_unknown_role():
    from futures_fund.desk_contracts import ReflectorEdit

    with pytest.raises(ValidationError):
        ReflectorEdit(
            role="cfo", region_text="x", reason="y", evidence=[], retire_if="z"
        )
