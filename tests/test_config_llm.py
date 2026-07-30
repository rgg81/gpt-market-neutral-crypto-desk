import pytest
from pydantic import ValidationError

from futures_fund.config import Settings, load_settings


def test_llm_desk_settings_defaults():
    s = load_settings()
    assert s.live is False                     # PAPER ONLY, forever
    assert s.account_size_usdt == 20000.0
    assert s.universe_top_n == 40              # top-40 by 24h volume (widened 2026-07-15)
    assert s.universe.symbol_count == 20       # post-gate cap desk_evidence now honors
    assert s.cadence_tf_minutes == 480         # 8h
    assert s.agent_model == "gpt-5.6-sol"
    assert s.btc_symbol == "BTC/USDT:USDT"


def test_live_mode_cannot_be_configured():
    with pytest.raises(ValidationError):
        Settings(live=True)
