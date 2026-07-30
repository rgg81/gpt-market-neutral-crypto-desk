"""Precheck engine tests, including the GOLDEN replay of the historical cycle-4 blowout book.

The bound values are ratified from the 2026-07-10 forensic review; the golden test pins that the
c4 book fires the bounds that would have stopped it and that an honest neutral book passes."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from futures_fund.desk_contracts import AdversaryVerdict, Book, BookLeg
from futures_fund.precheck import compute_precheck

CASH = 19832.83  # cycle-4 cash


def _ev(symbol: str, mark: float, beta: float, slip: float = 5.0,
        funding_bps: float = 0.0, curve: dict | None = None) -> dict:
    return {"symbol": symbol, "mark": mark, "beta_btc": beta, "est_slippage_bps_2k": slip,
            "expected_funding_8h_bps": funding_bps,
            "slippage_curve_bps": curve if curve is not None else {}}


# The REAL cycle-4 book (live_state/rebal/cycle/4/book.json) and its evidence betas.
C4_EVIDENCE = [
    _ev("HYPE/USDT:USDT", 67.199, 0.910), _ev("ZEC/USDT:USDT", 484.626, 1.253),
    _ev("XRP/USDT:USDT", 1.0961, 0.880), _ev("EVAA/USDT:USDT", 2.177, 3.689),
    _ev("LAB/USDT:USDT", 1.1515, 10.422, slip=900.0), _ev("SOL/USDT:USDT", 78.111, 0.945),
    _ev("BTC/USDT:USDT", 63283.3, 1.0),
]
C4_BOOK = Book(legs=[
    BookLeg(symbol="HYPE/USDT:USDT", side="long", target_notional=6500.0),
    BookLeg(symbol="ZEC/USDT:USDT", side="long", target_notional=5400.0),
    BookLeg(symbol="XRP/USDT:USDT", side="long", target_notional=5000.0),
    BookLeg(symbol="EVAA/USDT:USDT", side="long", target_notional=1800.0),
    BookLeg(symbol="LAB/USDT:USDT", side="short", target_notional=9000.0),
    BookLeg(symbol="SOL/USDT:USDT", side="short", target_notional=9700.0),
    BookLeg(symbol="BTC/USDT:USDT", side="long", target_notional=79500.0),
], stated_deploy_frac=1.069, stated_dollar_residual_frac=0.0, stated_beta_residual=0.0)
# The book it replaced (cycle-3 holdings, as current_book at c4 marks).
C4_CURRENT = [
    {"symbol": "ZEC/USDT:USDT", "side": "long", "target_notional": 5384.0},
    {"symbol": "HYPE/USDT:USDT", "side": "long", "target_notional": 3853.0},
    {"symbol": "VANRY/USDT:USDT", "side": "long", "target_notional": 1979.0},
    {"symbol": "LAB/USDT:USDT", "side": "short", "target_notional": 4724.0},
    {"symbol": "BTC/USDT:USDT", "side": "short", "target_notional": 3579.0},
    {"symbol": "ETH/USDT:USDT", "side": "short", "target_notional": 2500.0},
]


def test_golden_cycle4_blowout_fires_the_bounds():
    m = compute_precheck(C4_BOOK, C4_EVIDENCE, cash=CASH, cycle=4, current_book=C4_CURRENT)
    failing = {b.bound_id for b in m.bounds if not b.ok}
    # deploy 5.9x, 68% lopsided, BTC leg 68% of gross and 4x cash, LAB beta-$ 4.7x cash,
    # false stated_*, false-by-omission turnover fields, >2 legs changed, LAB slippage.
    assert {"B1", "B2", "B4", "B5", "B6", "B7", "B8", "B9", "B10"} <= failing
    assert m.deploy_frac == pytest.approx(5.90, abs=0.02)
    assert m.dollar_residual_frac == pytest.approx(0.68, abs=0.01)
    assert m.hedge_frac_cash > 4.0
    assert m.max_leg_beta_usd_symbol == "LAB/USDT:USDT"
    # turnover: VANRY+ETH dropped, XRP/EVAA/SOL added, BTC flipped -> 6 changes
    assert m.turnover_legs_changed == 6


def test_honest_neutral_book_passes_all_bounds():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0), _ev("B/USDT:USDT", 5.0, 1.0),
          _ev("C/USDT:USDT", 2.0, 0.9), _ev("D/USDT:USDT", 1.0, 1.1)]
    book = Book(legs=[
        BookLeg(symbol="A/USDT:USDT", side="long", target_notional=5500.0),
        BookLeg(symbol="C/USDT:USDT", side="long", target_notional=4000.0),
        BookLeg(symbol="B/USDT:USDT", side="short", target_notional=5500.0),
        BookLeg(symbol="D/USDT:USDT", side="short", target_notional=4000.0),
    ], stated_deploy_frac=0.958, stated_dollar_residual_frac=0.0,
       stated_beta_residual=-0.0202)
    current = [{"symbol": "A/USDT:USDT", "side": "long", "target_notional": 5450.0},
               {"symbol": "C/USDT:USDT", "side": "long", "target_notional": 3980.0},
               {"symbol": "B/USDT:USDT", "side": "short", "target_notional": 5480.0},
               {"symbol": "D/USDT:USDT", "side": "short", "target_notional": 4050.0}]
    m = compute_precheck(book, ev, cash=19832.83, cycle=7, current_book=current)
    assert all(b.ok for b in m.bounds), [b for b in m.bounds if not b.ok]
    assert m.turnover_legs_changed == 0            # within the 7% resize band
    assert m.sha256 and len(m.sha256) == 64


def test_duplicate_and_unpriced_legs_fire_b11():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0)]
    book = Book(legs=[
        BookLeg(symbol="A/USDT:USDT", side="long", target_notional=100.0),
        BookLeg(symbol="A/USDT:USDT", side="short", target_notional=100.0),
        BookLeg(symbol="GHOST/USDT:USDT", side="long", target_notional=100.0),
    ])
    m = compute_precheck(book, ev, cash=1000.0, cycle=1)
    b11 = next(b for b in m.bounds if b.bound_id == "B11")
    assert not b11.ok
    assert m.duplicate_symbols == ["A/USDT:USDT"]
    assert m.unpriced_symbols == ["GHOST/USDT:USDT"]


def test_sha_is_stable_and_content_addressed():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0)]
    book = Book(legs=[BookLeg(symbol="A/USDT:USDT", side="long", target_notional=100.0)])
    a = compute_precheck(book, ev, cash=1000.0, cycle=1)
    b = compute_precheck(book, ev, cash=1000.0, cycle=1)
    c = compute_precheck(book, ev, cash=1001.0, cycle=1)
    assert a.sha256 == b.sha256 != c.sha256


def test_recorded_c4_bare_accept_bytes_raise():
    """The exact recorded cycle-4 adversary payload must fail today's schema."""
    with pytest.raises(ValidationError):
        AdversaryVerdict.model_validate(
            {"accept": True, "objections": [], "demanded_changes": []})


# ---- B12: size-aware break-even (the cycle-11 regression) ----

# WLD's real cycle-11 numbers: funding -2.11bps (a LONG earns it), and a convex slippage curve
# whose $2k point (5.1bps) badly understates the $5k clip that was actually traded.
WLD_CURVE = {"2k": 5.1, "5k": 26.0, "10k": 60.0}


def test_b12_catches_the_cycle11_underpriced_rotation():
    """A $5,000 WLD long priced off the $2k probe looked like a 7.7-cycle payback; at its REAL
    clip size the friction is ~4x larger and the payback blows past 10 cycles. B12 must fail."""
    ev = [_ev("WLD/USDT:USDT", 1.0, 0.41, slip=5.1, funding_bps=-2.11, curve=WLD_CURVE),
          _ev("XRP/USDT:USDT", 1.0, 1.07, funding_bps=-0.206, curve={"2k": 0.9, "5k": 1.2}),
          _ev("DOGE/USDT:USDT", 1.0, 1.03, funding_bps=0.087, curve={"2k": 2.4, "5k": 3.0}),
          _ev("BTC/USDT:USDT", 1.0, 1.0, funding_bps=0.774, curve={"2k": 0.7, "5k": 0.9})]
    book = Book(legs=[
        BookLeg(symbol="WLD/USDT:USDT", side="long", target_notional=5000.0, is_new=True),
        BookLeg(symbol="XRP/USDT:USDT", side="long", target_notional=5180.0),
        BookLeg(symbol="DOGE/USDT:USDT", side="short", target_notional=4540.0),
        BookLeg(symbol="BTC/USDT:USDT", side="short", target_notional=5460.0),
    ], turnover_legs_changed=2)
    current = [{"symbol": "ETH/USDT:USDT", "side": "long", "target_notional": 4981.65},
               {"symbol": "XRP/USDT:USDT", "side": "long", "target_notional": 4847.74},
               {"symbol": "DOGE/USDT:USDT", "side": "short", "target_notional": 4876.50},
               {"symbol": "BTC/USDT:USDT", "side": "short", "target_notional": 5460.09}]
    m = compute_precheck(book, ev, cash=20108.12, cycle=11, current_book=current)
    b12 = next(b for b in m.bounds if b.bound_id == "B12")
    # friction = 2 * (26.0 + 5) bps * $5,000 = $31.00 ; carry = 2.11bps * $5,000 = $1.055/cyc
    # payback = ~29.4 cycles >> 10  -> the trade the desk actually made is REJECTED
    assert not b12.ok
    assert m.worst_changed_leg_payback_cycles > 10.0


def test_b12_passes_a_genuinely_fast_payback_leg():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=2.0, curve={"2k": 1.0, "5k": 1.5}),
          _ev("B/USDT:USDT", 1.0, 1.0, funding_bps=-2.0, curve={"2k": 1.0, "5k": 1.5})]
    book = Book(legs=[
        BookLeg(symbol="B/USDT:USDT", side="long", target_notional=5000.0),
        BookLeg(symbol="A/USDT:USDT", side="short", target_notional=5000.0, is_new=True),
    ], turnover_legs_changed=1)
    current = [{"symbol": "B/USDT:USDT", "side": "long", "target_notional": 5000.0}]
    m = compute_precheck(book, ev, cash=10500.0, cycle=2, current_book=current)
    b12 = next(b for b in m.bounds if b.bound_id == "B12")
    # friction = 2*(1.5+5)bps*5000 = $6.50 ; carry = 2.0bps*5000 = $1.00/cyc -> 6.5 cycles <= 10
    assert b12.ok and m.worst_changed_leg_payback_cycles < 10.0


def test_b12_is_inert_when_no_leg_changed():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=1.0, curve={"2k": 1.0})]
    book = Book(legs=[BookLeg(symbol="A/USDT:USDT", side="short", target_notional=1000.0)])
    current = [{"symbol": "A/USDT:USDT", "side": "short", "target_notional": 1000.0}]
    m = compute_precheck(book, ev, cash=1050.0, cycle=3, current_book=current)
    assert next(b for b in m.bounds if b.bound_id == "B12").ok
