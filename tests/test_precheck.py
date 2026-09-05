"""Precheck engine tests, including the GOLDEN replay of the historical cycle-4 blowout book.

The bound values are ratified from the 2026-07-10 forensic review; the golden test pins that the
c4 book fires the bounds that would have stopped it and that an honest neutral book passes."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from futures_fund.desk_contracts import AdversaryVerdict, Book, BookLeg
from futures_fund.precheck import (
    MAX_PAYBACK_FUNDING_INTERVALS,
    PrecheckMetrics,
    compute_precheck,
    precheck_sha256,
)
from futures_fund.slippage import ExecutionRealism

CASH = 19832.83  # cycle-4 cash


def _ev(
    symbol: str,
    mark: float,
    beta: float,
    slip: float = 5.0,
    funding_bps: float = 0.0,
    curve: dict | None = None,
    buy_curve: dict | None = None,
    sell_curve: dict | None = None,
    liquidity_mid: float | None = None,
) -> dict:
    row = {
        "symbol": symbol,
        "mark": mark,
        "beta_btc": beta,
        "est_slippage_bps_2k": slip,
        "expected_funding_8h_bps": funding_bps,
        "slippage_curve_bps": curve if curve is not None else {},
    }
    if buy_curve is not None:
        row["slippage_curve_buy_bps"] = buy_curve
    if sell_curve is not None:
        row["slippage_curve_sell_bps"] = sell_curve
    if liquidity_mid is not None or buy_curve is not None or sell_curve is not None:
        # Current directional curves are denominated at their same-snapshot L2 midpoint. Most
        # tests intentionally make it equal the decision mark; divergence regressions override it.
        row["liquidity_mid"] = mark if liquidity_mid is None else liquidity_mid
    return row


# The REAL cycle-4 book (live_state/rebal/cycle/4/book.json) and its evidence betas.
C4_EVIDENCE = [
    _ev("HYPE/USDT:USDT", 67.199, 0.910),
    _ev("ZEC/USDT:USDT", 484.626, 1.253),
    _ev("XRP/USDT:USDT", 1.0961, 0.880),
    _ev("EVAA/USDT:USDT", 2.177, 3.689),
    _ev("LAB/USDT:USDT", 1.1515, 10.422, slip=900.0),
    _ev("SOL/USDT:USDT", 78.111, 0.945),
    _ev("BTC/USDT:USDT", 63283.3, 1.0),
]
C4_BOOK = Book(
    legs=[
        BookLeg(symbol="HYPE/USDT:USDT", side="long", target_notional=6500.0),
        BookLeg(symbol="ZEC/USDT:USDT", side="long", target_notional=5400.0),
        BookLeg(symbol="XRP/USDT:USDT", side="long", target_notional=5000.0),
        BookLeg(symbol="EVAA/USDT:USDT", side="long", target_notional=1800.0),
        BookLeg(symbol="LAB/USDT:USDT", side="short", target_notional=9000.0),
        BookLeg(symbol="SOL/USDT:USDT", side="short", target_notional=9700.0),
        BookLeg(symbol="BTC/USDT:USDT", side="long", target_notional=79500.0),
    ],
    stated_deploy_frac=1.069,
    stated_dollar_residual_frac=0.0,
    stated_beta_residual=0.0,
)
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
    # VANRY+ETH dropped, XRP/EVAA/SOL added, BTC flipped, and HYPE/ZEC/LAB resized:
    # 2 drops + 3 adds + 1 flip + 3 resizes = 9. Even ZEC's small real resize counts.
    assert m.turnover_legs_changed == 9


def test_honest_neutral_book_passes_all_bounds():
    ev = [
        _ev("A/USDT:USDT", 10.0, 1.0),
        _ev("B/USDT:USDT", 5.0, 1.0),
        _ev("C/USDT:USDT", 2.0, 0.9),
        _ev("D/USDT:USDT", 1.0, 1.1),
    ]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=5500.0),
            BookLeg(symbol="C/USDT:USDT", side="long", target_notional=4000.0),
            BookLeg(symbol="B/USDT:USDT", side="short", target_notional=5500.0),
            BookLeg(symbol="D/USDT:USDT", side="short", target_notional=4000.0),
        ],
        stated_deploy_frac=0.958,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=-0.0202,
    )
    current = [
        {"symbol": "A/USDT:USDT", "side": "long", "target_notional": 5500.0},
        {"symbol": "C/USDT:USDT", "side": "long", "target_notional": 4000.0},
        {"symbol": "B/USDT:USDT", "side": "short", "target_notional": 5500.0},
        {"symbol": "D/USDT:USDT", "side": "short", "target_notional": 4000.0},
    ]
    m = compute_precheck(book, ev, cash=19832.83, cycle=7, current_book=current)
    assert all(b.ok for b in m.bounds), [b for b in m.bounds if not b.ok]
    assert m.turnover_legs_changed == 0
    assert m.sha256 and len(m.sha256) == 64


@pytest.mark.parametrize("action", ["new", "flip", "increase", "hedge_to_alpha"])
def test_b10_applies_tighter_50bp_screen_to_every_aggressive_alpha_action(action):
    symbol = "BTC/USDT:USDT" if action == "hedge_to_alpha" else "A/USDT:USDT"
    side = "long"
    prior: list[dict] = []
    target_notional = 100.0
    is_new = action in {"new", "flip"}
    if action == "flip":
        prior = [{"symbol": symbol, "side": "short", "target_notional": 100.0}]
    elif action == "increase":
        prior = [{"symbol": symbol, "side": side, "target_notional": 50.0}]
    elif action == "hedge_to_alpha":
        prior = [
            {
                "symbol": symbol,
                "side": side,
                "target_notional": 100.0,
                "seat_role": "hedge",
            }
        ]
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side=side,
                target_notional=target_notional,
                seat_role="alpha",
                expected_price_edge_frac=0.10,
                is_new=is_new,
                hold_breaking_reason="fresh-entry-quality action" if is_new else "",
            )
        ],
        turnover_legs_changed=0 if action == "hedge_to_alpha" else 1,
    )

    metrics = compute_precheck(
        book,
        [_ev(symbol, 100.0, 1.0, slip=60.0)],
        cash=1_000.0,
        cycle=1,
        current_book=prior,
    )

    b10 = next(bound for bound in metrics.bounds if bound.bound_id == "B10")
    assert not b10.ok
    leg = metrics.legs[0]
    assert leg.seat_role == "alpha"
    assert leg.material_effect in {"entry", "flip", "increase"}
    assert [row.rule_id for row in metrics.hard_ban_violations] == [
        "aggressive_alpha_2k_slippage"
    ]


@pytest.mark.parametrize("action", ["hold", "reduction", "drop"])
def test_b10_keeps_75bp_ceiling_for_holds_and_loss_control(action):
    symbol = "A/USDT:USDT"
    current_notional = 100.0 if action in {"hold", "drop"} else 150.0
    prior = [{"symbol": symbol, "side": "long", "target_notional": current_notional}]
    legs = [] if action == "drop" else [
        BookLeg(symbol=symbol, side="long", target_notional=100.0)
    ]
    book = Book(legs=legs, turnover_legs_changed=0 if action == "hold" else 1)

    metrics = compute_precheck(
        book,
        [_ev(symbol, 100.0, 1.0, slip=60.0)],
        cash=1_000.0,
        cycle=1,
        current_book=prior,
    )

    assert next(bound for bound in metrics.bounds if bound.bound_id == "B10").ok


def test_b10_fails_when_an_aggressive_alpha_lacks_the_2k_screen():
    evidence = _ev("A/USDT:USDT", 100.0, 1.0)
    evidence.pop("est_slippage_bps_2k")
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=100.0,
                expected_price_edge_frac=0.10,
                is_new=True,
                hold_breaking_reason="fresh entry",
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(book, [evidence], cash=1_000.0, cycle=1)

    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B10").ok


def test_b10_absolute_75bp_ceiling_includes_a_dropped_exit():
    symbol = "A/USDT:USDT"
    metrics = compute_precheck(
        Book(legs=[], turnover_legs_changed=1),
        [_ev(symbol, 100.0, 1.0, slip=80.0)],
        cash=1_000.0,
        cycle=1,
        current_book=[{"symbol": symbol, "side": "long", "target_notional": 100.0}],
    )

    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B10").ok


@pytest.mark.parametrize(
    ("action", "momentum_pct", "rule_id"),
    [
        ("new", -50.0, "post_crash_short"),
        ("flip", -50.0, "post_crash_short"),
        ("new", 50.0, "fade_short"),
        ("flip", 50.0, "fade_short"),
    ],
)
def test_precheck_records_objective_new_or_flipped_short_momentum_bans(
    action, momentum_pct, rule_id
):
    symbol = "A/USDT:USDT"
    current = [] if action == "new" else [
        {"symbol": symbol, "side": "long", "target_notional": 100.0}
    ]
    evidence = {**_ev(symbol, 100.0, 1.0), "momentum_pct": momentum_pct}
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="short",
                target_notional=100.0,
                expected_price_edge_frac=0.10,
                is_new=True,
                hold_breaking_reason="new short test",
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(
        book, [evidence], cash=1_000.0, cycle=1, current_book=current
    )

    assert len(metrics.hard_ban_violations) == 1
    violation = metrics.hard_ban_violations[0]
    assert violation.rule_id == rule_id
    assert violation.action == action
    assert violation.side == "short"
    assert violation.momentum_pct == momentum_pct


@pytest.mark.parametrize("action", ["new", "flip", "increase", "hedge_to_alpha"])
def test_precheck_records_low_depth_oversize_for_every_aggressive_alpha_action(action):
    symbol = "BTC/USDT:USDT" if action == "hedge_to_alpha" else "A/USDT:USDT"
    current: list[dict] = []
    is_new = action in {"new", "flip"}
    if action == "flip":
        current = [{"symbol": symbol, "side": "short", "target_notional": 2_000.0}]
    elif action == "increase":
        current = [{"symbol": symbol, "side": "long", "target_notional": 1_000.0}]
    elif action == "hedge_to_alpha":
        current = [
            {
                "symbol": symbol,
                "side": "long",
                "target_notional": 2_000.0,
                "seat_role": "hedge",
            }
        ]
    evidence = {
        **_ev(symbol, 100.0, 1.0),
        "depth_usd_bid": 50_000.0,
        "depth_usd_ask": 200_000.0,
    }
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="long",
                target_notional=2_000.0,
                seat_role="alpha",
                expected_price_edge_frac=0.10,
                is_new=is_new,
                hold_breaking_reason="aggressive low-depth test" if is_new else "",
            )
        ],
        turnover_legs_changed=0 if action == "hedge_to_alpha" else 1,
    )

    metrics = compute_precheck(
        book, [evidence], cash=5_000.0, cycle=1, current_book=current
    )

    violation = next(
        row
        for row in metrics.hard_ban_violations
        if row.rule_id == "aggressive_alpha_low_depth"
    )
    assert violation.action == action
    assert violation.target_notional == 2_000.0
    assert violation.depth_usd_bid == 50_000.0
    assert violation.depth_usd_ask == 200_000.0


@pytest.mark.parametrize("action", ["hold", "reduction"])
def test_low_depth_oversize_hard_ban_does_not_trap_incumbent_loss_control(action):
    symbol = "A/USDT:USDT"
    current_notional = 2_000.0 if action == "hold" else 3_000.0
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=2_000.0)],
        turnover_legs_changed=0 if action == "hold" else 1,
    )
    evidence = {
        **_ev(symbol, 100.0, 1.0),
        "depth_usd_bid": 50_000.0,
        "depth_usd_ask": 50_000.0,
    }

    metrics = compute_precheck(
        book,
        [evidence],
        cash=5_000.0,
        cycle=1,
        current_book=[
            {"symbol": symbol, "side": "long", "target_notional": current_notional}
        ],
    )

    assert metrics.hard_ban_violations == []


def test_duplicate_and_unpriced_legs_fire_b11():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0)]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=100.0),
            BookLeg(symbol="A/USDT:USDT", side="short", target_notional=100.0),
            BookLeg(symbol="GHOST/USDT:USDT", side="long", target_notional=100.0),
        ]
    )
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
        AdversaryVerdict.model_validate({"accept": True, "objections": [], "demanded_changes": []})


# ---- B12: size-aware break-even (the cycle-11 regression) ----

# WLD's real cycle-11 numbers: funding -2.11bps (a LONG earns it), and a convex slippage curve
# whose $2k point (5.1bps) badly understates the $5k clip that was actually traded.
WLD_CURVE = {"2k": 5.1, "5k": 26.0, "10k": 60.0}


def test_b12_catches_the_cycle11_underpriced_rotation():
    """A $5,000 WLD long priced off the $2k probe looked like a 7.7-interval payback; at its REAL
    clip the friction is ~4x larger and payback exceeds ten funding intervals. B12 must fail."""
    ev = [
        _ev("WLD/USDT:USDT", 1.0, 0.41, slip=5.1, funding_bps=-2.11, curve=WLD_CURVE),
        _ev("XRP/USDT:USDT", 1.0, 1.07, funding_bps=-0.206, curve={"2k": 0.9, "5k": 1.2}),
        _ev("DOGE/USDT:USDT", 1.0, 1.03, funding_bps=0.087, curve={"2k": 2.4, "5k": 3.0}),
        _ev("BTC/USDT:USDT", 1.0, 1.0, funding_bps=0.774, curve={"2k": 0.7, "5k": 0.9}),
    ]
    book = Book(
        legs=[
            BookLeg(symbol="WLD/USDT:USDT", side="long", target_notional=5000.0, is_new=True),
            BookLeg(symbol="XRP/USDT:USDT", side="long", target_notional=5180.0),
            BookLeg(symbol="DOGE/USDT:USDT", side="short", target_notional=4540.0),
            BookLeg(symbol="BTC/USDT:USDT", side="short", target_notional=5460.0),
        ],
        turnover_legs_changed=2,
    )
    current = [
        {"symbol": "ETH/USDT:USDT", "side": "long", "target_notional": 4981.65},
        {"symbol": "XRP/USDT:USDT", "side": "long", "target_notional": 4847.74},
        {"symbol": "DOGE/USDT:USDT", "side": "short", "target_notional": 4876.50},
        {"symbol": "BTC/USDT:USDT", "side": "short", "target_notional": 5460.09},
    ]
    m = compute_precheck(book, ev, cash=20108.12, cycle=11, current_book=current)
    b12 = next(b for b in m.bounds if b.bound_id == "B12")
    # friction = 2 * (26.0 + 5) bps * $5,000 = $31.00 ; carry = 2.11bps * $5,000 = $1.055/cyc
    # payback = ~29.4 funding intervals >> 10 -> the actual trade is rejected
    assert not b12.ok
    assert m.worst_changed_leg_payback_cycles > 10.0


def test_b12_passes_a_genuinely_fast_payback_leg():
    ev = [
        _ev("A/USDT:USDT", 1.0, 1.0, funding_bps=2.0, curve={"2k": 1.0, "5k": 1.5}),
        _ev("B/USDT:USDT", 1.0, 1.0, funding_bps=-2.0, curve={"2k": 1.0, "5k": 1.5}),
    ]
    book = Book(
        legs=[
            BookLeg(symbol="B/USDT:USDT", side="long", target_notional=5000.0),
            BookLeg(symbol="A/USDT:USDT", side="short", target_notional=5000.0, is_new=True),
        ],
        turnover_legs_changed=1,
    )
    current = [{"symbol": "B/USDT:USDT", "side": "long", "target_notional": 5000.0}]
    m = compute_precheck(book, ev, cash=10500.0, cycle=2, current_book=current)
    b12 = next(b for b in m.bounds if b.bound_id == "B12")
    # friction = 2*(1.5+5)bps*5000 = $6.50 ; carry = 2.0bps*5000 = $1.00/cyc -> 6.5 cycles <= 10
    assert b12.ok and m.worst_changed_leg_payback_cycles < 10.0


@pytest.mark.parametrize(
    ("target_payback", "expected_b12_ok"),
    [
        (MAX_PAYBACK_FUNDING_INTERVALS - 0.0000004, True),
        (MAX_PAYBACK_FUNDING_INTERVALS + 0.0000004, False),
    ],
)
def test_b12_persists_exact_payback_across_the_ten_interval_boundary(
    target_payback: float,
    expected_b12_ok: bool,
):
    notional = 1000.0
    horizon_intervals = 168.0 / 8.0
    # With zero quoted slippage, round-trip friction is exactly $1. Choose the one-time price
    # edge so its within-horizon payback lands less than half a six-decimal unit from B12's limit.
    edge_frac = horizon_intervals / (notional * target_payback)
    evidence = [
        _ev(
            "A/USDT:USDT",
            1.0,
            1.0,
            funding_bps=0.0,
            curve={"2k": 0.0},
        )
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=notional,
                is_new=True,
                hold_breaking_reason="boundary regression",
                expected_price_edge_frac=edge_frac,
                edge_horizon_hours=168,
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(book, evidence, cash=2000.0, cycle=2, current_book=[])
    b12 = next(bound for bound in metrics.bounds if bound.bound_id == "B12")
    payback = metrics.change_costs[0].payback_intervals

    assert payback == pytest.approx(target_payback, abs=1e-12)
    assert b12.value == pytest.approx(target_payback, abs=1e-12)
    assert (payback <= MAX_PAYBACK_FUNDING_INTERVALS) is expected_b12_ok
    assert (b12.value <= MAX_PAYBACK_FUNDING_INTERVALS) is expected_b12_ok
    assert b12.ok is expected_b12_ok
    persisted = json.loads(metrics.model_dump_json())["change_costs"][0]["payback_intervals"]
    assert (persisted <= MAX_PAYBACK_FUNDING_INTERVALS) is expected_b12_ok


@pytest.mark.parametrize(
    ("horizon_hours", "edge_frac", "forecast_edge_usd"),
    [(24, 0.002, 4.0), (72, 0.00475, 9.5)],
)
def test_b12_never_repeats_a_one_time_price_forecast_past_its_horizon(
    horizon_hours: int,
    edge_frac: float,
    forecast_edge_usd: float,
):
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=0.0, curve={"2k": 20.0})]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=2000.0,
                is_new=True,
                expected_price_edge_frac=edge_frac,
                edge_horizon_hours=horizon_hours,
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(book, ev, cash=4000.0, cycle=2, current_book=[])
    leg = metrics.legs[0]

    # Round-trip friction is $10. The declared forecast is a one-time move, not a rate that may
    # be replayed until interval ten; with no carry, a forecast below $10 never breaks even.
    assert leg.changed_slice_friction_usd == pytest.approx(10.0)
    assert leg.changed_slice_expected_edge_through_horizon_usd == pytest.approx(forecast_edge_usd)
    assert leg.changed_slice_net_edge_through_horizon_after_friction_usd < 0.0
    assert leg.required_price_edge_frac_for_max_payback == pytest.approx(0.005)
    assert metrics.worst_changed_leg_payback_cycles == 9999.0
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


@pytest.mark.parametrize(
    ("horizon_hours", "expected_required_frac"),
    [
        (24, 0.0053),  # $10 friction plus three intervals of $0.20 adverse carry
        (72, 0.0059),  # ... plus nine intervals; do not extend to interval ten
        (168, 0.0126),  # ten of 21 forecast intervals available, carry accrues for ten
    ],
)
def test_required_price_edge_uses_piecewise_horizon_for_adverse_carry(
    horizon_hours: int,
    expected_required_frac: float,
):
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=1.0, curve={"2k": 20.0})]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=2000.0,
                is_new=True,
                edge_horizon_hours=horizon_hours,
            )
        ],
        turnover_legs_changed=1,
    )
    metrics = compute_precheck(book, ev, cash=4000.0, cycle=2, current_book=[])
    assert metrics.legs[0].required_price_edge_frac_for_max_payback == pytest.approx(
        expected_required_frac
    )


def test_b12_flip_prices_prior_close_new_entry_and_eventual_exit():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=0.0, curve={"2k": 1.0, "5k": 10.0, "10k": 20.0})]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="short",
                target_notional=2000.0,
                expected_price_edge_frac=0.01,
                edge_horizon_hours=24,
            )
        ],
        turnover_legs_changed=1,
    )
    current = [{"symbol": "A/USDT:USDT", "side": "long", "target_notional": 5000.0}]
    metrics = compute_precheck(book, ev, cash=7000.0, cycle=2, current_book=current)
    # Execution crosses one combined $7k delta: (20+5)bps*$7k=$17.50. The eventual
    # $2k exit costs another (1+5)bps*$2k=$1.20. A one-time $20/24h forecast accrues linearly
    # inside its three-interval horizon, so the $18.70 friction repays in 2.805 intervals.
    assert metrics.worst_changed_leg_payback_cycles == pytest.approx(2.805)
    leg = metrics.legs[0]
    assert leg.change == "flipped"
    assert leg.turnover_notional == pytest.approx(7000.0)


def test_b12_entry_uses_actual_entry_and_future_exit_sides_not_aggregate_worst():
    evidence = [
        _ev(
            "A/USDT:USDT",
            1.0,
            1.0,
            curve={"2k": 99.0},
            buy_curve={"2k": 20.0},
            sell_curve={"2k": 2.0},
        )
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=2000.0,
                is_new=True,
                hold_breaking_reason="asymmetric entry-cost regression",
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(book, evidence, cash=4000.0, cycle=2, current_book=[])

    # BUY entry crosses asks at 20bps; future SELL exit crosses bids at 2bps; each pays 5bps.
    # The 99bps conservative aggregate is a display/legacy field and must not be charged twice.
    assert metrics.change_costs[0].friction_usd == pytest.approx(6.4)

    missing_future_exit = compute_precheck(
        book,
        [
            {
                **evidence[0],
                "slippage_curve_sell_bps": {},
            }
        ],
        cash=4000.0,
        cycle=3,
        current_book=[],
    )
    assert missing_future_exit.change_costs[0].friction_priced is False
    assert not next(bound for bound in missing_future_exit.bounds if bound.bound_id == "B12").ok


def test_b12_values_fixed_entry_quantity_at_the_curve_liquidity_mid():
    evidence = [
        _ev(
            "A/USDT:USDT",
            90.0,
            1.0,
            buy_curve={"10k": 1.0, "20k": 100.0},
            sell_curve={"10k": 1.0, "20k": 100.0},
            liquidity_mid=100.0,
        )
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=10_000.0,
                is_new=True,
                hold_breaking_reason="decision/L2 denomination regression",
                expected_price_edge_frac=0.004,
                edge_horizon_hours=24,
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(book, evidence, cash=20_000.0, cycle=20, current_book=[])
    cost = metrics.change_costs[0]

    # The PM fixes 10,000 / 90 units. At the curve's $100 midpoint both entry and projected exit
    # are $11,111.11 clips, so each must select the 20k/100bps tier, not the 10k/1bp tier.
    assert cost.decision_turnover_usd == 10_000.0
    assert cost.executable_turnover_usd == pytest.approx(11_111.111111, abs=1e-6)
    assert cost.future_exit_executable_notional_usd == pytest.approx(11_111.111111, abs=1e-6)
    assert cost.friction_usd == pytest.approx(233.333333, abs=1e-6)
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_b12_values_reduction_and_drop_quantities_at_the_curve_liquidity_mid():
    evidence = [
        _ev(
            "A/USDT:USDT",
            90.0,
            1.0,
            funding_bps=200.0,
            buy_curve={"10k": 1.0, "20k": 100.0},
            sell_curve={"10k": 1.0, "20k": 100.0},
            liquidity_mid=100.0,
        )
    ]
    reduction = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="long",
                    target_notional=10_000.0,
                )
            ],
            turnover_legs_changed=1,
        ),
        evidence,
        cash=30_000.0,
        cycle=21,
        current_book=[
            {
                "symbol": "A/USDT:USDT",
                "side": "long",
                "target_notional": 20_000.0,
            }
        ],
    )
    drop = compute_precheck(
        Book(legs=[], turnover_legs_changed=1),
        evidence,
        cash=30_000.0,
        cycle=22,
        current_book=[
            {
                "symbol": "A/USDT:USDT",
                "side": "long",
                "target_notional": 10_000.0,
            }
        ],
    )

    for metrics, action in ((reduction, "reduction"), (drop, "drop")):
        cost = metrics.change_costs[0]
        assert cost.action == action
        assert cost.decision_turnover_usd == 10_000.0
        assert cost.executable_turnover_usd == pytest.approx(11_111.111111, abs=1e-6)
        # Long loss control SELLs the fixed quantity into the 20k/100bps bid tier once.
        assert cost.friction_usd == pytest.approx(116.666667, abs=1e-6)


def test_b12_values_flip_delta_and_future_exit_at_the_curve_liquidity_mid():
    evidence = [
        _ev(
            "A/USDT:USDT",
            90.0,
            1.0,
            buy_curve={"10k": 1.0, "20k": 100.0},
            sell_curve={"10k": 1.0, "20k": 100.0},
            liquidity_mid=100.0,
        )
    ]
    metrics = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="short",
                    target_notional=5_000.0,
                    is_new=True,
                    hold_breaking_reason="fixed-quantity flip regression",
                )
            ],
            turnover_legs_changed=1,
        ),
        evidence,
        cash=10_000.0,
        cycle=23,
        current_book=[
            {
                "symbol": "A/USDT:USDT",
                "side": "long",
                "target_notional": 5_000.0,
            }
        ],
    )
    cost = metrics.change_costs[0]

    # The $10k decision delta becomes an $11,111 SELL (20k tier). The future short exit is the
    # retained 5k/90 quantity, worth $5,555 at L2 and therefore covered by the 10k BUY tier.
    assert cost.executable_turnover_usd == pytest.approx(11_111.111111, abs=1e-6)
    assert cost.future_exit_executable_notional_usd == pytest.approx(5_555.555556, abs=1e-6)
    assert cost.friction_usd == pytest.approx(120.0, abs=1e-6)


@pytest.mark.parametrize("liquidity_mid", [None, 0.0, -1.0])
def test_b12_fails_closed_when_directional_evidence_has_no_valid_liquidity_mid(
    liquidity_mid: float | None,
):
    evidence_row = _ev(
        "A/USDT:USDT",
        90.0,
        1.0,
        buy_curve={"10k": 1.0},
        sell_curve={"10k": 1.0},
        liquidity_mid=liquidity_mid,
    )
    if liquidity_mid is None:
        evidence_row.pop("liquidity_mid")
    metrics = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="long",
                    target_notional=5_000.0,
                    is_new=True,
                    hold_breaking_reason="missing L2 midpoint regression",
                )
            ],
            turnover_legs_changed=1,
        ),
        [evidence_row],
        cash=10_000.0,
        cycle=24,
        current_book=[],
    )

    assert metrics.change_costs[0].executable_turnover_usd is None
    assert metrics.change_costs[0].friction_priced is False
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_b12_keeps_immutable_aggregate_only_evidence_on_legacy_dollar_basis():
    evidence = [_ev("A/USDT:USDT", 90.0, 1.0, curve={"10k": 1.0})]
    metrics = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="long",
                    target_notional=10_000.0,
                    is_new=True,
                    hold_breaking_reason="legacy replay",
                )
            ],
            turnover_legs_changed=1,
        ),
        evidence,
        cash=20_000.0,
        cycle=25,
        current_book=[],
    )
    cost = metrics.change_costs[0]

    assert cost.liquidity_mid is None
    assert cost.executable_turnover_usd == 10_000.0
    assert cost.future_exit_executable_notional_usd == 10_000.0
    assert cost.friction_usd == pytest.approx(12.0)


def test_b12_does_not_charge_a_noop_when_mark_and_liquidity_mid_diverge():
    evidence = [
        _ev(
            "A/USDT:USDT",
            90.0,
            1.0,
            buy_curve={"10k": 1.0, "20k": 100.0},
            sell_curve={"10k": 1.0, "20k": 100.0},
            liquidity_mid=100.0,
        )
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=10_000.0,
            )
        ]
    )
    metrics = compute_precheck(
        book,
        evidence,
        cash=10_000.0,
        cycle=26,
        current_book=[
            {
                "symbol": "A/USDT:USDT",
                "side": "long",
                "target_notional": 10_000.0,
            }
        ],
    )

    assert metrics.change_costs == []
    assert metrics.turnover_usd == 0.0
    assert next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_b12_drop_uses_only_the_actual_one_way_exit_side():
    evidence = [
        _ev(
            "A/USDT:USDT",
            1.0,
            1.0,
            funding_bps=2.0,
            curve={},
            buy_curve={},
            sell_curve={"2k": 2.0},
        )
    ]
    current = [
        {
            "symbol": "A/USDT:USDT",
            "side": "long",
            "target_notional": 2000.0,
        }
    ]

    metrics = compute_precheck(
        Book(legs=[], turnover_legs_changed=1),
        evidence,
        cash=4000.0,
        cycle=2,
        current_book=current,
    )

    # Closing the long is a SELL into the fully covered bid side. Missing asks are irrelevant to
    # this one-way loss-control action, so its friction remains truthfully priced.
    assert metrics.change_costs[0].friction_usd == pytest.approx(1.4)
    assert metrics.change_costs[0].friction_priced is True
    assert next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok

    short_drop = compute_precheck(
        Book(legs=[], turnover_legs_changed=1),
        evidence,
        cash=4000.0,
        cycle=3,
        current_book=[{**current[0], "side": "short"}],
    )
    assert short_drop.change_costs[0].friction_priced is False
    assert not next(bound for bound in short_drop.bounds if bound.bound_id == "B12").ok


def test_b12_resize_uses_round_trip_for_increase_and_one_way_for_reduction():
    evidence = [
        _ev(
            "A/USDT:USDT",
            1.0,
            1.0,
            curve={"2k": 99.0},
            buy_curve={"2k": 20.0},
            sell_curve={"2k": 2.0},
        )
    ]
    increased = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="short",
                    target_notional=3000.0,
                )
            ],
            turnover_legs_changed=1,
        ),
        evidence,
        cash=4000.0,
        cycle=2,
        current_book=[
            {
                "symbol": "A/USDT:USDT",
                "side": "short",
                "target_notional": 1000.0,
            }
        ],
    )
    reduced = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="short",
                    target_notional=1000.0,
                )
            ],
            turnover_legs_changed=1,
        ),
        evidence,
        cash=4000.0,
        cycle=3,
        current_book=[
            {
                "symbol": "A/USDT:USDT",
                "side": "short",
                "target_notional": 3000.0,
            }
        ],
    )

    # Short increase SELLs now at 2bps and reserves a future BUY at 20bps, plus two fees.
    assert increased.change_costs[0].friction_usd == pytest.approx(6.4)
    # Short reduction is one BUY into asks at 20bps, plus one fee.
    assert reduced.change_costs[0].friction_usd == pytest.approx(5.0)


def test_b12_flip_prices_immediate_delta_and_future_exit_on_their_actual_sides():
    evidence = [
        _ev(
            "A/USDT:USDT",
            1.0,
            1.0,
            curve={"2k": 99.0, "5k": 99.0},
            buy_curve={"2k": 20.0, "5k": 30.0},
            sell_curve={"2k": 2.0, "5k": 4.0},
        )
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="short",
                target_notional=2000.0,
                is_new=True,
                hold_breaking_reason="asymmetric flip-cost regression",
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(
        book,
        evidence,
        cash=4000.0,
        cycle=2,
        current_book=[
            {
                "symbol": "A/USDT:USDT",
                "side": "long",
                "target_notional": 2000.0,
            }
        ],
    )

    # Long -> short sends one $4k SELL delta (5k sell point, 4bps) then reserves a $2k BUY exit
    # (20bps). Fees add 5bps to each actual clip: $3.60 + $5.00 = $8.60.
    assert metrics.change_costs[0].friction_usd == pytest.approx(8.6)


def test_b12_fails_closed_when_combined_flip_exceeds_measured_curve():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=0.0, curve={"10k": 1.0})]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="short",
                target_notional=6000.0,
                expected_price_edge_frac=0.10,
                edge_horizon_hours=8,
            )
        ],
        turnover_legs_changed=1,
    )
    current = [{"symbol": "A/USDT:USDT", "side": "long", "target_notional": 6000.0}]
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=2, current_book=current)
    assert metrics.worst_changed_leg_payback_cycles == 9999.0
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok
    assert metrics.legs[0].changed_slice_friction_usd is None
    assert metrics.legs[0].required_price_edge_frac_for_max_payback is None
    # A fail-closed unpriced clip must still produce strict JSON; Infinity/NaN would corrupt the
    # persisted precheck and its content hash.
    json.dumps(metrics.model_dump(mode="json"), allow_nan=False)


def test_b12_fails_closed_when_fresh_evidence_has_no_fully_covered_curve_point():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=0.0, curve={})]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=1500.0,
                is_new=True,
                expected_price_edge_frac=0.10,
                edge_horizon_hours=24,
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(book, ev, cash=3000.0, cycle=2, current_book=[])

    assert metrics.legs[0].changed_slice_friction_usd is None
    assert metrics.worst_changed_leg_payback_cycles == 9999.0
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_b12_is_inert_when_no_leg_changed():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=1.0, curve={"2k": 1.0})]
    book = Book(legs=[BookLeg(symbol="A/USDT:USDT", side="short", target_notional=1000.0)])
    current = [{"symbol": "A/USDT:USDT", "side": "short", "target_notional": 1000.0}]
    m = compute_precheck(book, ev, cash=1050.0, cycle=3, current_book=current)
    assert next(b for b in m.bounds if b.bound_id == "B12").ok


def test_material_resizes_count_and_bad_resize_economics_fail_b12():
    ev = [
        _ev("A/USDT:USDT", 1.0, 1.0, funding_bps=2.0, curve={"2k": 1.0}),
        _ev("B/USDT:USDT", 1.0, 1.0, funding_bps=-2.0, curve={"2k": 1.0}),
    ]
    current = [
        {"symbol": "A/USDT:USDT", "side": "short", "target_notional": 4000.0},
        {"symbol": "B/USDT:USDT", "side": "long", "target_notional": 8000.0},
    ]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="short", target_notional=6000.0),
            BookLeg(symbol="B/USDT:USDT", side="long", target_notional=6000.0),
        ],
        stated_deploy_frac=1.0,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
        turnover_legs_changed=2,
    )
    m = compute_precheck(book, ev, cash=12_000.0, cycle=8, current_book=current)
    assert m.turnover_legs_changed == 2
    assert m.legs_resized == ["A/USDT:USDT", "B/USDT:USDT"]
    # Increasing positive-carry A is economic; reducing positive-carry B is not.
    assert not next(b for b in m.bounds if b.bound_id == "B12").ok


def test_b9_caps_aggressive_changes_without_trapping_material_risk_reductions():
    ev = [
        _ev(
            f"{symbol}/USDT:USDT",
            1.0,
            1.0,
            funding_bps=2.0 if side == "short" else -2.0,
            curve={"2k": 1.0},
        )
        for symbol, side in (("A", "long"), ("B", "long"), ("C", "short"), ("D", "short"))
    ]
    current = [
        {"symbol": f"{symbol}/USDT:USDT", "side": side, "target_notional": 5000.0}
        for symbol, side in (("A", "long"), ("B", "long"), ("C", "short"), ("D", "short"))
    ]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=6000.0),
            BookLeg(symbol="B/USDT:USDT", side="long", target_notional=4000.0),
            BookLeg(symbol="C/USDT:USDT", side="short", target_notional=6000.0),
            BookLeg(symbol="D/USDT:USDT", side="short", target_notional=4000.0),
        ],
        turnover_legs_changed=4,
    )
    m = compute_precheck(book, ev, cash=20_000.0, cycle=9, current_book=current)
    assert m.turnover_legs_changed == 4
    assert m.turnover_aggressive_legs_changed == 2
    assert m.turnover_risk_reductions == 2
    assert next(b for b in m.bounds if b.bound_id == "B9").ok


def test_more_than_two_entries_or_material_increases_still_fail_b9():
    ev = [
        _ev(f"{symbol}/USDT:USDT", 1.0, 1.0, funding_bps=2.0, curve={"2k": 1.0})
        for symbol in ("A", "B", "C", "D")
    ]
    current = [
        {"symbol": f"{symbol}/USDT:USDT", "side": "short", "target_notional": 5000.0}
        for symbol in ("A", "B", "C", "D")
    ]
    book = Book(
        legs=[
            BookLeg(symbol=f"{symbol}/USDT:USDT", side="short", target_notional=6000.0)
            for symbol in ("A", "B", "C", "D")
        ],
        turnover_legs_changed=4,
    )
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=10, current_book=current)
    assert metrics.turnover_aggressive_legs_changed == 4
    assert metrics.turnover_risk_reductions == 0
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B9").ok


def test_small_resize_staircase_is_truthful_costed_turnover_not_a_noop():
    ev = [
        _ev(f"{symbol}/USDT:USDT", 1.0, 1.0, funding_bps=0.0, curve={"2k": 1.0})
        for symbol in "ABCD"
    ]
    current = [
        {
            "symbol": f"{symbol}/USDT:USDT",
            "side": "long" if symbol < "C" else "short",
            "target_notional": 4000.0,
        }
        for symbol in "ABCD"
    ]
    book = Book(
        legs=[
            BookLeg(symbol=row["symbol"], side=row["side"], target_notional=4280.0)
            for row in current
        ],
        turnover_legs_changed=4,
    )
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=10, current_book=current)
    assert metrics.turnover_usd == pytest.approx(1120.0)
    assert metrics.turnover_legs_changed == 4
    assert metrics.turnover_aggressive_legs_changed == 4
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B9").ok
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_b8_checks_is_new_and_hold_breaking_reason_claims():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0)]
    book = Book(
        legs=[BookLeg(symbol="A/USDT:USDT", side="long", target_notional=1000.0)],
        turnover_legs_changed=1,
    )
    metrics = compute_precheck(book, ev, cash=2000.0, cycle=10, current_book=[])
    assert len(metrics.turnover_claim_errors) == 2
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B8").ok


def test_b12_combines_explicit_price_edge_with_carry_for_a_new_relative_value_seat():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=0.0, curve={"2k": 1.0})]
    no_edge = Book(
        legs=[BookLeg(symbol="A/USDT:USDT", side="short", target_notional=2000.0, is_new=True)],
        turnover_legs_changed=1,
    )
    with_edge = no_edge.model_copy(
        update={
            "legs": [
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="short",
                    target_notional=2000.0,
                    is_new=True,
                    expected_price_edge_frac=0.01,
                    edge_horizon_hours=24,
                )
            ]
        }
    )
    no_edge_metrics = compute_precheck(no_edge, ev, cash=4000.0, cycle=11, current_book=[])
    edge_metrics = compute_precheck(with_edge, ev, cash=4000.0, cycle=11, current_book=[])
    assert not next(b for b in no_edge_metrics.bounds if b.bound_id == "B12").ok
    assert next(b for b in edge_metrics.bounds if b.bound_id == "B12").ok
    assert edge_metrics.legs[0].expected_price_edge_usd == pytest.approx(20.0)
    assert edge_metrics.legs[0].zero_price_edge_payback_intervals == 9999.0
    assert edge_metrics.legs[0].required_price_edge_frac_for_max_payback == pytest.approx(0.0012)
    assert edge_metrics.portfolio_expected_price_edge_usd_per_8h == pytest.approx(20.0 / 3.0)


def test_dropped_positive_carry_seat_is_uncapped_risk_reduction_but_still_costed_by_b12():
    ev = [_ev("A/USDT:USDT", 1.0, 1.0, funding_bps=2.0, curve={"2k": 1.0})]
    book = Book(legs=[], turnover_legs_changed=1)
    current = [{"symbol": "A/USDT:USDT", "side": "short", "target_notional": 2000.0}]
    metrics = compute_precheck(book, ev, cash=4000.0, cycle=12, current_book=current)
    assert metrics.turnover_aggressive_legs_changed == 0
    assert metrics.turnover_risk_reductions == 1
    assert next(b for b in metrics.bounds if b.bound_id == "B9").ok
    assert not next(b for b in metrics.bounds if b.bound_id == "B12").ok


def test_changed_or_dropped_symbol_without_evidence_fails_b11_and_b12_closed():
    book = Book(legs=[], turnover_legs_changed=1)
    current = [{"symbol": "MISSING/USDT:USDT", "side": "long", "target_notional": 2000.0}]
    metrics = compute_precheck(book, [], cash=4000.0, cycle=12, current_book=current)
    assert metrics.unpriced_symbols == ["MISSING/USDT:USDT"]
    assert not next(b for b in metrics.bounds if b.bound_id == "B11").ok
    assert not next(b for b in metrics.bounds if b.bound_id == "B12").ok
    assert metrics.worst_changed_leg_payback_symbol == "MISSING/USDT:USDT"
    assert metrics.total_action_friction_usd is None
    assert not metrics.total_action_friction_fully_priced
    assert metrics.change_costs[0].friction_priced is False
    json.dumps(metrics.model_dump(mode="json"), allow_nan=False)

    changed_book = Book(
        legs=[
            BookLeg(
                symbol="MISSING/USDT:USDT",
                side="long",
                target_notional=2500.0,
                is_new=True,
            )
        ],
        turnover_legs_changed=1,
    )
    changed = compute_precheck(changed_book, [], cash=4000.0, cycle=13, current_book=[])
    assert not next(b for b in changed.bounds if b.bound_id == "B11").ok
    assert not next(b for b in changed.bounds if b.bound_id == "B12").ok
    assert changed.total_aggressive_expected_edge_through_horizon_pre_friction_usd is None
    assert changed.total_aggressive_expected_net_edge_through_horizon_after_friction_usd is None


def test_hedge_to_alpha_role_change_is_fresh_alpha_action_with_zero_turnover():
    ev = [_ev("BTC/USDT:USDT", 60_000.0, 1.0, curve={"2k": 1.0})]
    current = [
        {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "target_notional": 2000.0,
            "seat_role": "hedge",
        }
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="long",
                target_notional=2000.0,
                seat_role="alpha",
                expected_price_edge_frac=0.01,
            )
        ]
    )
    metrics = compute_precheck(book, ev, cash=4000.0, cycle=13, current_book=current)
    leg = metrics.legs[0]
    assert leg.change == "role_changed"
    assert leg.material_effect == "entry"
    assert leg.turnover_notional == 0.0
    assert metrics.turnover_usd == 0.0
    assert metrics.turnover_legs_changed == 0
    assert metrics.turnover_aggressive_legs_changed == 1
    assert metrics.change_costs[0].action == "role_change"
    assert metrics.change_costs[0].executable_turnover_usd == 0.0


def test_hedge_to_alpha_with_notional_cut_prices_delta_but_audits_full_alpha_seat():
    ev = [
        _ev(
            "BTC/USDT:USDT",
            60_000.0,
            1.0,
            funding_bps=2.0,
            curve={"2k": 1.0},
        )
    ]
    current = [
        {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "target_notional": 3000.0,
            "seat_role": "hedge",
        }
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="long",
                target_notional=2000.0,
                seat_role="alpha",
                expected_price_edge_frac=0.0,
            )
        ],
        turnover_legs_changed=1,
    )
    metrics = compute_precheck(book, ev, cash=4000.0, cycle=13, current_book=current)
    leg = metrics.legs[0]
    cost = metrics.change_costs[0]
    assert leg.material_effect == "entry"
    assert metrics.turnover_aggressive_legs_changed == 1
    assert metrics.turnover_usd == 1000.0
    assert cost.action == "role_change"
    assert cost.executable_turnover_usd == 1000.0
    assert cost.friction_usd == pytest.approx(0.6)
    # Full retained $2k alpha seat pays adverse long carry: -$0.40/interval for three intervals.
    assert cost.expected_edge_through_horizon_pre_friction_usd == pytest.approx(-1.2)
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_alpha_to_hedge_role_change_stays_explicit_without_alpha_action():
    ev = [_ev("BTC/USDT:USDT", 60_000.0, 1.0, curve={"2k": 1.0})]
    current = [
        {
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "target_notional": 2000.0,
            "seat_role": "alpha",
        }
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="short",
                target_notional=2000.0,
                seat_role="hedge",
            )
        ]
    )
    metrics = compute_precheck(book, ev, cash=4000.0, cycle=14, current_book=current)
    assert metrics.legs[0].change == "role_changed"
    assert metrics.legs[0].material_effect == "none"
    assert metrics.turnover_aggressive_legs_changed == 0
    assert metrics.change_costs[0].action == "role_change"
    assert metrics.change_costs[0].payback_intervals == 0.0
    assert next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_risk_reducing_btc_hedge_is_insurance_not_a_carry_entry():
    ev = [
        {**_ev("A/USDT:USDT", 1.0, 9.0), "beta_clamped": 1.5},
        _ev("B/USDT:USDT", 1.0, 1.0),
        _ev("BTC/USDT:USDT", 60_000.0, 1.0, funding_bps=-1.0, curve={"2k": 0.5, "5k": 0.7}),
    ]
    current = [
        {"symbol": "A/USDT:USDT", "side": "long", "target_notional": 8000.0},
        {"symbol": "B/USDT:USDT", "side": "short", "target_notional": 8000.0},
    ]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=8000.0),
            BookLeg(symbol="B/USDT:USDT", side="short", target_notional=8000.0),
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="short",
                target_notional=3000.0,
                is_new=True,
                seat_role="hedge",
            ),
        ],
        turnover_legs_changed=1,
    )
    m = compute_precheck(book, ev, cash=20_000.0, cycle=10, current_book=current)
    assert m.hedge_risk_reducing
    assert next(b for b in m.bounds if b.bound_id == "B12").ok
    hedge_cost = next(row for row in m.change_costs if row.symbol.startswith("BTC/"))
    assert hedge_cost.b12_insurance_exempt
    assert hedge_cost.friction_priced
    assert hedge_cost.friction_usd > 0.0
    assert m.total_action_friction_usd == pytest.approx(hedge_cost.friction_usd)
    assert next(lm for lm in m.legs if lm.symbol.startswith("A/")).beta == 1.5


def test_b12_hedge_exemption_compares_with_the_carried_hedge_not_no_hedge():
    ev = [
        _ev("A/USDT:USDT", 1.0, 1.5),
        _ev("B/USDT:USDT", 1.0, 1.0),
        _ev(
            "BTC/USDT:USDT",
            60_000.0,
            1.0,
            funding_bps=0.0,
            curve={"2k": 0.5},
        ),
    ]
    current = [
        {"symbol": "A/USDT:USDT", "side": "long", "target_notional": 8000.0},
        {"symbol": "B/USDT:USDT", "side": "short", "target_notional": 8000.0},
        {
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "target_notional": 4000.0,
            "seat_role": "hedge",
        },
    ]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=8000.0),
            BookLeg(symbol="B/USDT:USDT", side="short", target_notional=8000.0),
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="short",
                target_notional=3200.0,
                seat_role="hedge",
            ),
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=10, current_book=current)

    assert metrics.hedge_risk_reducing is True  # versus the $4k-beta alpha book
    assert metrics.hedge_counterfactual_beta_net_usd == pytest.approx(0.0)
    assert metrics.beta_net_usd == pytest.approx(800.0)
    assert metrics.hedge_change_risk_reducing is False
    hedge = next(leg for leg in metrics.legs if leg.seat_role == "hedge")
    assert hedge.changed_slice_friction_usd is not None
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_risk_reducing_btc_alpha_label_does_not_receive_hedge_exemption():
    ev = [
        _ev("A/USDT:USDT", 1.0, 1.5),
        _ev("B/USDT:USDT", 1.0, 1.0),
        _ev("BTC/USDT:USDT", 60_000.0, 1.0, funding_bps=0.0, curve={"2k": 0.5}),
    ]
    current = [
        {"symbol": "A/USDT:USDT", "side": "long", "target_notional": 8000.0},
        {"symbol": "B/USDT:USDT", "side": "short", "target_notional": 8000.0},
    ]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=8000.0),
            BookLeg(symbol="B/USDT:USDT", side="short", target_notional=8000.0),
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="short",
                target_notional=2000.0,
                is_new=True,
                seat_role="alpha",
            ),
        ],
        turnover_legs_changed=1,
    )
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=10, current_book=current)
    assert not metrics.hedge_risk_reducing
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_directional_btc_leg_is_not_exempt_from_b12():
    ev = [_ev("BTC/USDT:USDT", 60_000.0, 1.0, funding_bps=0.0, curve={"5k": 0.7})]
    book = Book(
        legs=[BookLeg(symbol="BTC/USDT:USDT", side="long", target_notional=5000.0, is_new=True)],
        turnover_legs_changed=1,
    )
    m = compute_precheck(book, ev, cash=20_000.0, cycle=11, current_book=[])
    assert not m.hedge_risk_reducing
    assert not next(b for b in m.bounds if b.bound_id == "B12").ok


def test_precheck_exposes_alpha_volatility_concentration_without_vetoing():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0), _ev("B/USDT:USDT", 10.0, 1.0)]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=4_000.0),
            BookLeg(symbol="B/USDT:USDT", side="short", target_notional=4_000.0),
        ]
    )
    risk_model = {
        "residual_vol_annualized": {"A/USDT:USDT": 0.50, "B/USDT:USDT": 0.10},
        "covariance_annualized": {
            "A/USDT:USDT": {"A/USDT:USDT": 0.25, "B/USDT:USDT": 0.0},
            "B/USDT:USDT": {"A/USDT:USDT": 0.0, "B/USDT:USDT": 0.01},
        },
        "high_correlation_pairs": [],
    }
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=12, risk_model=risk_model)

    assert metrics.risk_model_available is True
    assert metrics.max_alpha_standalone_risk_symbol == "A/USDT:USDT"
    assert metrics.max_alpha_standalone_risk_share == pytest.approx(5.0 / 6.0)
    assert metrics.portfolio_residual_vol_annualized_usd == pytest.approx(2039.61)
    # Risk concentration is evidence for the sole Adversary, not a deterministic B13 veto.
    assert {bound.bound_id for bound in metrics.bounds} == {f"B{index}" for index in range(1, 13)}
    seat = next(leg for leg in metrics.legs if leg.symbol == "A/USDT:USDT")
    assert seat.standalone_risk_share == pytest.approx(5.0 / 6.0)
    assert seat.variance_contribution_frac == pytest.approx(16.0 / 16.64)


def test_precheck_never_turns_negative_covariance_variance_into_zero_risk():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0), _ev("B/USDT:USDT", 10.0, 1.0)]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=4_000.0),
            BookLeg(symbol="B/USDT:USDT", side="short", target_notional=4_000.0),
        ]
    )
    non_psd = {
        "available": True,
        "residual_vol_annualized": {"A/USDT:USDT": 0.1, "B/USDT:USDT": 0.1},
        "covariance_annualized": {
            "A/USDT:USDT": {"A/USDT:USDT": 0.01, "B/USDT:USDT": 0.02},
            "B/USDT:USDT": {"A/USDT:USDT": 0.02, "B/USDT:USDT": 0.01},
        },
    }
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=13, risk_model=non_psd)
    assert metrics.risk_model_available is False
    assert "negative variance" in metrics.risk_model_unavailable_reason


def test_precheck_separates_alpha_from_typed_risk_reducing_hedge():
    ev = [
        {**_ev("A/USDT:USDT", 10.0, 1.5), "beta_clamped": 1.5},
        {**_ev("B/USDT:USDT", 10.0, 1.0), "beta_clamped": 1.0},
        {**_ev("BTC/USDT:USDT", 60_000.0, 1.0), "beta_clamped": 1.0},
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=4_000.0,
                expected_price_edge_frac=0.01,
                edge_horizon_hours=24,
            ),
            BookLeg(
                symbol="B/USDT:USDT",
                side="short",
                target_notional=4_000.0,
                expected_price_edge_frac=0.02,
                edge_horizon_hours=24,
            ),
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="short",
                target_notional=2_000.0,
                seat_role="hedge",
            ),
        ]
    )
    risk_model = {
        "residual_vol_annualized": {"A/USDT:USDT": 0.2, "B/USDT:USDT": 0.2},
        "covariance_annualized": {
            "A/USDT:USDT": {"A/USDT:USDT": 0.04, "B/USDT:USDT": 0.0},
            "B/USDT:USDT": {"A/USDT:USDT": 0.0, "B/USDT:USDT": 0.04},
        },
        "high_correlation_pairs": [],
    }
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=14, risk_model=risk_model)

    assert metrics.gross == 10_000.0
    assert metrics.alpha_gross == 8_000.0
    assert metrics.hedge_gross == 2_000.0
    assert metrics.alpha_beta_net_usd_before_hedge == 2_000.0
    assert metrics.hedge_beta_usd == -2_000.0
    assert metrics.hedge_beta_reduction_frac == 1.0
    assert metrics.hedge_risk_reducing is True
    assert metrics.portfolio_expected_price_edge_usd_per_8h == pytest.approx(40.0)
    hedge = next(leg for leg in metrics.legs if leg.seat_role == "hedge")
    assert hedge.expected_total_edge_usd_per_8h == hedge.expected_carry_usd_per_8h


def test_precheck_exposes_same_side_high_correlation_cluster_without_b13():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0), _ev("B/USDT:USDT", 10.0, 1.0)]
    book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=4_000.0),
            BookLeg(symbol="B/USDT:USDT", side="long", target_notional=4_000.0),
        ]
    )
    risk_model = {
        "residual_vol_annualized": {"A/USDT:USDT": 0.2, "B/USDT:USDT": 0.1},
        "covariance_annualized": {
            "A/USDT:USDT": {"A/USDT:USDT": 0.04, "B/USDT:USDT": 0.016},
            "B/USDT:USDT": {"A/USDT:USDT": 0.016, "B/USDT:USDT": 0.01},
        },
        "high_correlation_pairs": [
            {"left": "A/USDT:USDT", "right": "B/USDT:USDT", "correlation": 0.8}
        ],
    }
    metrics = compute_precheck(book, ev, cash=20_000.0, cycle=15, risk_model=risk_model)

    assert metrics.max_same_side_high_correlation_cluster_risk_share == 1.0
    assert metrics.same_side_high_correlation_clusters == [
        {
            "side": "long",
            "symbols": ["A/USDT:USDT", "B/USDT:USDT"],
            "standalone_risk_share": 1.0,
            "variance_contribution_frac": pytest.approx(1.0),
        }
    ]
    assert {bound.bound_id for bound in metrics.bounds} == {f"B{index}" for index in range(1, 13)}


def test_precheck_uses_signed_position_correlation_for_cluster_risk():
    ev = [_ev("A/USDT:USDT", 10.0, 1.0), _ev("B/USDT:USDT", 10.0, 1.0)]
    risk_model = {
        "residual_vol_annualized": {"A/USDT:USDT": 0.2, "B/USDT:USDT": 0.2},
        "covariance_annualized": {
            "A/USDT:USDT": {"A/USDT:USDT": 0.04, "B/USDT:USDT": -0.032},
            "B/USDT:USDT": {"A/USDT:USDT": -0.032, "B/USDT:USDT": 0.04},
        },
        "high_correlation_pairs": [
            {"left": "A/USDT:USDT", "right": "B/USDT:USDT", "correlation": -0.8}
        ],
    }
    same_side = compute_precheck(
        Book(
            legs=[
                BookLeg(symbol="A/USDT:USDT", side="long", target_notional=4_000.0),
                BookLeg(symbol="B/USDT:USDT", side="long", target_notional=4_000.0),
            ]
        ),
        ev,
        cash=20_000.0,
        cycle=16,
        risk_model=risk_model,
    )
    assert same_side.same_side_high_correlation_clusters == []
    assert same_side.position_co_risk_clusters == []
    assert same_side.held_high_correlation_pairs[0]["position_pnl_correlation"] == -0.8

    opposite_side = compute_precheck(
        Book(
            legs=[
                BookLeg(symbol="A/USDT:USDT", side="long", target_notional=4_000.0),
                BookLeg(symbol="B/USDT:USDT", side="short", target_notional=4_000.0),
            ]
        ),
        ev,
        cash=20_000.0,
        cycle=17,
        risk_model=risk_model,
    )
    assert opposite_side.same_side_high_correlation_clusters == []
    assert opposite_side.max_position_co_risk_cluster_risk_share == 1.0
    assert opposite_side.position_co_risk_clusters[0]["side"] == "mixed"
    assert opposite_side.held_high_correlation_pairs[0]["position_pnl_correlation"] == 0.8


@pytest.mark.parametrize(
    "generation_fields",
    [
        {},
        {"hedge_risk_reducing": True},
        {
            "hedge_risk_reducing": False,
            "input_meta_sha256": "a" * 64,
            "risk_model_available": False,
            "turnover_aggressive_legs_changed": 0,
            "turnover_risk_reductions": 0,
        },
    ],
)
def test_legacy_precheck_hash_replays_the_exact_historical_shape(generation_fields):
    # Representative earliest-v1 shape, including a recursively sparse LegMetric. Later v1
    # generations added selected fields incrementally. Defaults introduced by today's models must
    # never enter the hash of any of those immutable artifacts.
    legacy = {
        "cycle": 1,
        "cash": 20_000.0,
        "gross": 1_000.0,
        "deploy_frac": 0.05,
        "longs_usd": 1_000.0,
        "shorts_usd": 0.0,
        "dollar_residual_frac": 1.0,
        "beta_net_usd": 1_000.0,
        "beta_residual": 0.05,
        "max_leg_symbol": "A/USDT:USDT",
        "max_leg_frac_gross": 1.0,
        "hedge_notional": 0.0,
        "hedge_frac_cash": 0.0,
        "max_leg_beta_usd_symbol": "A/USDT:USDT",
        "max_leg_beta_usd": 1_000.0,
        "legs": [
            {
                "symbol": "A/USDT:USDT",
                "side": "long",
                "notional": 1_000.0,
                "beta": 1.0,
                "beta_usd": 1_000.0,
                "frac_gross": 1.0,
                "change": "new",
            }
        ],
        "legs_added": ["A/USDT:USDT"],
        "legs_dropped": [],
        "legs_flipped": [],
        "legs_resized": [],
        "turnover_legs_changed": 1,
        "turnover_usd": 1_000.0,
        "unpriced_symbols": [],
        "duplicate_symbols": [],
        "worst_changed_leg_payback_cycles": 0.0,
        "bounds": [],
        **generation_fields,
    }
    legacy_hash = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, default=str).encode()
    ).hexdigest()
    legacy["sha256"] = legacy_hash

    loaded = PrecheckMetrics.model_validate(legacy)

    assert loaded.schema_version == 1
    assert precheck_sha256(loaded) == legacy_hash
    assert set(loaded.model_dump(mode="json", exclude_unset=True)) == set(legacy)


def test_schema2_hash_ignores_schema3_liquidity_denominator_defaults():
    metrics = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="long",
                    target_notional=1_000.0,
                    is_new=True,
                    hold_breaking_reason="schema replay",
                )
            ],
            turnover_legs_changed=1,
        ),
        [_ev("A/USDT:USDT", 10.0, 1.0, curve={"2k": 1.0})],
        cash=2_000.0,
        cycle=2,
        current_book=[],
    )
    schema2 = metrics.model_dump(mode="json")
    schema2["schema_version"] = 2
    schema2.pop("sha256")
    for row in schema2["change_costs"]:
        row.pop("decision_turnover_usd")
        row.pop("future_exit_executable_notional_usd")
        row.pop("liquidity_mid")
    expected_hash = hashlib.sha256(
        json.dumps(schema2, sort_keys=True, default=str).encode()
    ).hexdigest()
    schema2["sha256"] = expected_hash

    loaded = PrecheckMetrics.model_validate(schema2)

    assert loaded.schema_version == 2
    assert precheck_sha256(loaded) == expected_hash
    persisted_cost = loaded.model_dump(mode="json", exclude_unset=True)["change_costs"][0]
    assert "decision_turnover_usd" not in persisted_cost
    assert "future_exit_executable_notional_usd" not in persisted_cost
    assert "liquidity_mid" not in persisted_cost


def test_full_overhedge_drop_is_b12_insurance_exempt_only_when_beta_improves():
    evidence = [
        _ev("A/USDT:USDT", 1.0, 1.0),
        _ev("B/USDT:USDT", 1.0, 1.0),
        _ev(
            "BTC/USDT:USDT",
            1.0,
            1.0,
            funding_bps=10.0,
            curve={"2k": 1.0},
        ),
    ]
    balanced_book = Book(
        legs=[
            BookLeg(symbol="A/USDT:USDT", side="long", target_notional=5_000.0),
            BookLeg(symbol="B/USDT:USDT", side="short", target_notional=5_000.0),
        ],
        stated_deploy_frac=0.5,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
        turnover_legs_changed=1,
    )
    overhedged_current = [
        {"symbol": "A/USDT:USDT", "side": "long", "target_notional": 5_000.0},
        {"symbol": "B/USDT:USDT", "side": "short", "target_notional": 5_000.0},
        {
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "target_notional": 1_000.0,
            "seat_role": "hedge",
        },
    ]
    improving = compute_precheck(
        balanced_book,
        evidence,
        cash=20_000.0,
        cycle=18,
        current_book=overhedged_current,
    )
    improving_drop = next(row for row in improving.change_costs if row.symbol == "BTC/USDT:USDT")
    assert improving_drop.action == "drop"
    assert improving_drop.friction_priced
    assert improving_drop.b12_insurance_exempt
    assert next(bound for bound in improving.bounds if bound.bound_id == "B12").ok

    directional_book = balanced_book.model_copy(
        update={
            "legs": [
                balanced_book.legs[0].model_copy(update={"target_notional": 6_000.0}),
                balanced_book.legs[1],
            ],
            "stated_deploy_frac": 0.55,
            "stated_dollar_residual_frac": 1_000.0 / 11_000.0,
            "stated_beta_residual": 0.05,
        }
    )
    exactly_hedged_current = [
        {"symbol": "A/USDT:USDT", "side": "long", "target_notional": 6_000.0},
        {"symbol": "B/USDT:USDT", "side": "short", "target_notional": 5_000.0},
        {
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "target_notional": 1_000.0,
            "seat_role": "hedge",
        },
    ]
    worsening = compute_precheck(
        directional_book,
        evidence,
        cash=20_000.0,
        cycle=19,
        current_book=exactly_hedged_current,
    )
    worsening_drop = next(row for row in worsening.change_costs if row.symbol == "BTC/USDT:USDT")
    assert not worsening_drop.b12_insurance_exempt
    assert not next(bound for bound in worsening.bounds if bound.bound_id == "B12").ok


def test_b12_uses_the_same_depth_haircut_and_execution_reserves_as_reconcile():
    curve = {"5k": 2.0, "10k": 8.0, "20k": 15.0}
    evidence = [
        {
            **_ev(
                symbol,
                1.0,
                0.0,
                buy_curve=curve,
                sell_curve=curve,
            ),
            "depth_usd_bid": 20_000.0,
            "depth_usd_ask": 20_000.0,
        }
        for symbol in ("A/USDT:USDT", "B/USDT:USDT")
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=5_000.0,
                is_new=True,
                hold_breaking_reason="test",
                expected_price_edge_frac=0.05,
            ),
            BookLeg(
                symbol="B/USDT:USDT",
                side="short",
                target_notional=5_000.0,
                is_new=True,
                hold_breaking_reason="test",
                expected_price_edge_frac=0.05,
            ),
        ],
        turnover_legs_changed=2,
    )
    realism = ExecutionRealism(
        latency_ms=500.0,
        displayed_depth_fraction=0.5,
        adverse_selection_bps=1.0,
        legging_bps_per_second=0.25,
        allow_partial_fills=True,
    )

    metrics = compute_precheck(
        book,
        evidence,
        cash=20_000.0,
        cycle=20,
        current_book=[],
        execution_realism=realism,
    )

    assert metrics.schema_version == 5
    assert metrics.execution_policy_applied is True
    assert metrics.execution_displayed_depth_fraction == 0.5
    assert metrics.pretrade_legging_reserve_bps == pytest.approx(0.125)
    assert metrics.partial_fill_risk_symbols == []
    for change in metrics.change_costs:
        assert change.executable_turnover_usd == pytest.approx(5_000.0)
        assert change.conservative_curve_lookup_turnover_usd == pytest.approx(10_000.0)
        assert change.conservative_future_exit_curve_lookup_usd == pytest.approx(10_000.0)
        assert change.adverse_selection_reserve_bps == 1.0
        assert change.legging_reserve_bps == pytest.approx(0.125)
        assert change.estimated_min_fill_fraction == 1.0
        # two one-way executions at (8 + 5 fee + 1 adverse + .125 legging) bps
        assert change.friction_usd == pytest.approx(14.125)


def test_precheck_exposes_expected_partial_fill_and_fails_full_clip_economics_closed():
    curve = {"2k": 1.0, "5k": 4.0}
    evidence = [
        {
            **_ev(
                "A/USDT:USDT",
                1.0,
                0.0,
                buy_curve=curve,
                sell_curve=curve,
            ),
            "depth_usd_bid": 6_000.0,
            "depth_usd_ask": 6_000.0,
        }
    ]
    book = Book(
        legs=[
            BookLeg(
                symbol="A/USDT:USDT",
                side="long",
                target_notional=5_000.0,
                is_new=True,
                hold_breaking_reason="test",
                expected_price_edge_frac=0.05,
            )
        ],
        turnover_legs_changed=1,
    )

    metrics = compute_precheck(
        book,
        evidence,
        cash=10_000.0,
        cycle=21,
        current_book=[],
        execution_realism=ExecutionRealism(displayed_depth_fraction=0.5),
    )

    change = metrics.change_costs[0]
    assert change.conservative_curve_lookup_turnover_usd == 10_000.0
    assert change.estimated_min_fill_fraction == pytest.approx(0.6)
    assert change.partial_fill_expected is True
    assert metrics.partial_fill_risk_symbols == ["A/USDT:USDT"]
    assert change.friction_priced is False
    assert metrics.total_action_friction_fully_priced is False
    assert not next(bound for bound in metrics.bounds if bound.bound_id == "B12").ok


def test_schema3_hash_ignores_schema4_execution_policy_defaults():
    metrics = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="long",
                    target_notional=1_000.0,
                    is_new=True,
                    hold_breaking_reason="schema replay",
                )
            ],
            turnover_legs_changed=1,
        ),
        [_ev("A/USDT:USDT", 10.0, 1.0, curve={"2k": 1.0})],
        cash=2_000.0,
        cycle=2,
        current_book=[],
    )
    schema3 = metrics.model_dump(mode="json")
    schema3["schema_version"] = 3
    schema3.pop("sha256")
    for field in (
        "execution_policy_applied",
        "execution_latency_ms",
        "execution_displayed_depth_fraction",
        "execution_adverse_selection_bps",
        "execution_legging_bps_per_second",
        "execution_allow_partial_fills",
        "pretrade_legging_reserve_bps",
        "partial_fill_risk_symbols",
    ):
        schema3.pop(field)
    for row in schema3["change_costs"]:
        for field in (
            "conservative_curve_lookup_turnover_usd",
            "conservative_future_exit_curve_lookup_usd",
            "estimated_min_fill_fraction",
            "partial_fill_expected",
            "adverse_selection_reserve_bps",
            "legging_reserve_bps",
        ):
            row.pop(field)
    expected_hash = hashlib.sha256(
        json.dumps(schema3, sort_keys=True, default=str).encode()
    ).hexdigest()
    schema3["sha256"] = expected_hash

    loaded = PrecheckMetrics.model_validate(schema3)

    assert precheck_sha256(loaded) == expected_hash
    persisted = loaded.model_dump(mode="json", exclude_unset=True)
    assert "execution_policy_applied" not in persisted
    assert "conservative_curve_lookup_turnover_usd" not in persisted["change_costs"][0]


def test_schema4_hash_ignores_schema5_objective_hard_ban_default():
    metrics = compute_precheck(
        Book(
            legs=[
                BookLeg(
                    symbol="A/USDT:USDT",
                    side="long",
                    target_notional=1_000.0,
                    is_new=True,
                    hold_breaking_reason="schema replay",
                )
            ],
            turnover_legs_changed=1,
        ),
        [_ev("A/USDT:USDT", 10.0, 1.0, curve={"2k": 1.0})],
        cash=2_000.0,
        cycle=2,
        current_book=[],
    )
    schema4 = metrics.model_dump(mode="json")
    schema4["schema_version"] = 4
    schema4.pop("sha256")
    schema4.pop("hard_ban_violations")
    expected_hash = hashlib.sha256(
        json.dumps(schema4, sort_keys=True, default=str).encode()
    ).hexdigest()
    schema4["sha256"] = expected_hash

    loaded = PrecheckMetrics.model_validate(schema4)

    assert precheck_sha256(loaded) == expected_hash
    assert "hard_ban_violations" not in loaded.model_dump(
        mode="json", exclude_unset=True
    )
