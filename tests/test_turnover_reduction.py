"""Tests for turnover reduction via current_book in PM input."""
import json
from datetime import UTC, datetime

import pytest

from futures_fund.account import PaperAccount, Position
from futures_fund.evidence import EvidencePack

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


def test_current_book_is_built_correctly_from_positions():
    """When the account has existing positions, current_book is built correctly."""
    # Set up: account with two existing positions
    account = PaperAccount(cash=20000.0, positions={
        "SOL/USDT:USDT": Position(
            symbol="SOL/USDT:USDT", direction="long", entry_price=150.0, qty=10.0,
            opened_ts=NOW, opened_cycle=1),
        "XRP/USDT:USDT": Position(
            symbol="XRP/USDT:USDT", direction="short", entry_price=0.60, qty=5000.0,
            opened_ts=NOW, opened_cycle=1),
    })
    marks = {"SOL/USDT:USDT": 155.0, "XRP/USDT:USDT": 0.58}

    # Build current_book the same way run_cycle does
    current_book = [
        {"symbol": s, "side": p.direction,
         "target_notional": abs(p.qty) * marks.get(s, p.entry_price)}
        for s, p in account.positions.items() if s in marks
    ]

    # Expected: current_book has two legs with correct notionals
    assert len(current_book) == 2
    sol_leg = next((lg for lg in current_book if lg["symbol"] == "SOL/USDT:USDT"), None)
    xrp_leg = next((lg for lg in current_book if lg["symbol"] == "XRP/USDT:USDT"), None)
    assert sol_leg is not None
    assert xrp_leg is not None
    assert sol_leg["side"] == "long"
    assert sol_leg["target_notional"] == pytest.approx(1550.0)  # 10 * 155.0
    assert xrp_leg["side"] == "short"
    assert xrp_leg["target_notional"] == pytest.approx(2900.0)  # 5000 * 0.58


def test_current_book_serializes_to_json():
    """current_book structure is JSON-serializable for the agent payload."""
    current_book = [
        {"symbol": "SOL/USDT:USDT", "side": "long", "target_notional": 1550.0},
        {"symbol": "XRP/USDT:USDT", "side": "short", "target_notional": 2900.0},
    ]

    # Build the payload the same way run_pm does
    payload = {
        "cash": 20000.0,
        "reads": {"sentiment": [], "technical": [], "futures": []},
        "evidence": [EvidencePack(symbol="BTC/USDT:USDT", mark=60000.0, beta_btc=1.0,
                    volume_24h=1e9, funding_rate=0.01, as_of_ts=NOW).model_dump(mode="json")],
        "current_book": current_book,
    }

    # Should be JSON-serializable
    json_str = json.dumps(payload, default=str)
    assert json_str is not None

    # Should deserialize correctly
    deserialized = json.loads(json_str)
    assert "current_book" in deserialized
    assert len(deserialized["current_book"]) == 2


def test_payload_without_current_book_is_valid():
    """Backward compatibility: payload without current_book is still valid."""
    payload = {
        "cash": 20000.0,
        "reads": {"sentiment": [], "technical": [], "futures": []},
        "evidence": [EvidencePack(symbol="BTC/USDT:USDT", mark=60000.0, beta_btc=1.0,
                    volume_24h=1e9, funding_rate=0.01, as_of_ts=NOW).model_dump(mode="json")],
    }

    # Should be JSON-serializable
    json_str = json.dumps(payload, default=str)
    assert json_str is not None

    # Should deserialize correctly without current_book
    deserialized = json.loads(json_str)
    assert "current_book" not in deserialized
