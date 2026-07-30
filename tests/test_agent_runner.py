import pytest

from futures_fund.agent_runner import StubAgentRunner, parse_or_raise
from futures_fund.desk_contracts import SpecialistRead


def test_stub_returns_canned_by_role():
    read = SpecialistRead(
        symbol="SOL/USDT:USDT", lean="long", conviction=0.7, rationale="r", evidence=[]
    )
    runner = StubAgentRunner(canned={"sentiment": read})
    out = runner.run("sentiment", "ignored prompt", SpecialistRead)
    assert out.symbol == "SOL/USDT:USDT" and out.lean == "long"


def test_parse_or_raise_validates_json():
    good = '{"symbol":"X","lean":"short","conviction":0.4,"rationale":"r","evidence":[]}'
    assert parse_or_raise(SpecialistRead, good).lean == "short"
    with pytest.raises(ValueError):
        parse_or_raise(SpecialistRead, '{"symbol":"X","lean":"nope","conviction":2}')
