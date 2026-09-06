import pytest
from pydantic import ValidationError

from futures_fund.config import Settings, load_settings


def test_llm_desk_settings_defaults():
    s = load_settings()
    assert s.live is False                     # PAPER ONLY, forever
    assert s.account_size_usdt == 20000.0
    assert s.universe_top_n == 40              # top-40 by 24h volume (widened 2026-07-15)
    assert s.universe.symbol_count == 20       # post-gate cap desk_evidence now honors
    assert s.cadence_tf_minutes == 1440        # 24h full-GPT cycle
    assert s.agent_model == "gpt-5.6-sol"
    assert s.btc_symbol == "BTC/USDT:USDT"
    assert s.data.binance_klines_proxy_url == "http://127.0.0.1:8000"
    assert s.data.candle_proxy_timeout_seconds == 15.0
    assert s.data.binance_proxy_project_dir == "~/binance-proxy"
    assert s.data.binance_proxy_start_timeout_seconds == 15.0
    assert s.execution.latency_ms == 500.0
    assert s.execution.displayed_depth_fraction == 0.5
    assert s.execution.adverse_selection_bps == 1.0
    assert s.execution.legging_bps_per_second == 0.25
    assert s.execution.allow_partial_fills is True
    assert s.cross_section.universe_size == 50
    assert s.cross_section.sleeve_size == 10
    assert s.cross_section.volume_lookback_days == 180
    assert s.cross_section.performance_lookback_days == 7
    assert s.cross_section.gross_target_frac == 1.0
    assert s.cross_section.max_decision_age_minutes == 90


def test_live_mode_cannot_be_configured():
    with pytest.raises(ValidationError):
        Settings(live=True)


def test_execution_realism_config_is_bounded():
    with pytest.raises(ValidationError):
        Settings(execution={"displayed_depth_fraction": 0.0})
    with pytest.raises(ValidationError):
        Settings(execution={"adverse_selection_bps": -0.1})
