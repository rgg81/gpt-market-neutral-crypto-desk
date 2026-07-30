from futures_fund.models import (
    Cadence,
    Direction,
    PairTestMethod,
    SentimentLevel,
    SleeveName,
    SpreadState,
    SymbolSpec,
    TradeProposal,
    get_args,
)


def test_new_shared_aliases_have_expected_members():
    assert set(get_args(SleeveName)) == {"carry", "pairs", "factor", "sentiment", "discretionary"}
    assert set(get_args(SentimentLevel)) == {
        "very_positive", "positive", "neutral", "negative", "very_negative"
    }
    assert set(get_args(SpreadState)) == {"flat", "long_spread", "short_spread", "stop"}
    assert set(get_args(PairTestMethod)) == {"engle_granger", "johansen"}
    assert set(get_args(Cadence)) == {"weekly", "daily", "rebal"}
    assert set(get_args(Direction)) == {"long", "short"}


def test_trade_proposal_rejects_long_stop_above_entry():
    import pytest
    with pytest.raises(ValueError):
        TradeProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=101.0,
                      atr=1.0, confidence=0.5, horizon_hours=8.0, funding_rate=0.0001)


def test_symbol_spec_sorts_brackets_by_floor():
    from futures_fund.models import MmrBracket
    spec = SymbolSpec(symbol="BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=5.0,
                      mmr_brackets=[
                          MmrBracket(notional_floor=50.0, notional_cap=100.0, mmr=0.01,
                                     maint_amount=1.0, max_leverage=50.0),
                          MmrBracket(notional_floor=0.0, notional_cap=50.0, mmr=0.005,
                                     maint_amount=0.0, max_leverage=100.0),
                      ])
    assert [b.notional_floor for b in spec.sorted_brackets] == [0.0, 50.0]
