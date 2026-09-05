# tests/test_account_integration.py — create (or append if it exists from Task 12 ordering)
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from futures_fund.account import ClosedLeg, CostInputs, PaperAccount, Position, load_account
from futures_fund.desk_contracts import CycleReport
from futures_fund.pnl_attribution import build_cycle_pnl


def test_historical_funding_settlement_uses_raw_published_rate_not_signal_cap():
    symbol = "BTC/USDT:USDT"
    start = datetime(2026, 9, 1, 0, 7, tzinfo=UTC)
    end = datetime(2026, 9, 1, 8, 7, tzinfo=UTC)
    account = PaperAccount(
        cash=20_000.0,
        last_funding_ts=start,
        positions={
            symbol: Position(
                symbol=symbol,
                direction="long",
                qty=1.0,
                entry_price=100.0,
                opened_ts=start,
            )
        },
    )

    # 1% is intentionally above the strategy's BTC signal cap. A real published settlement must
    # still debit the full 1%: 1 contract * $100 settlement mark * 1% = $1.
    account.settle_funding_events(
        {
            symbol: [{
                "timestamp": datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
                "rate": 0.01,
                "mark": 100.0,
            }]
        },
        now=end,
        observed_intervals={symbol: 8},
    )

    assert account.cash == 19_999.0
    assert account.funding_paid == 1.0
    assert account.positions[symbol].accrued_funding == -1.0


def test_held_account_without_funding_clock_fails_closed_without_advancing_it():
    symbol = "ETH/USDT:USDT"
    now = datetime(2026, 9, 1, 8, 7, tzinfo=UTC)

    def account_without_clock():
        return PaperAccount(
            cash=20_000.0,
            positions={
                symbol: Position(
                    symbol=symbol,
                    direction="short",
                    qty=2.0,
                    entry_price=100.0,
                    opened_ts=now - timedelta(days=1),
                )
            },
        )

    historical = account_without_clock()
    before = historical.model_dump(mode="json")
    with pytest.raises(ValueError, match="explicit audited migration is required"):
        historical.settle_funding_events({symbol: []}, now=now)
    assert historical.model_dump(mode="json") == before

    legacy = account_without_clock()
    before = legacy.model_dump(mode="json")
    with pytest.raises(ValueError, match="explicit audited migration is required"):
        legacy.settle_funding(
            now - timedelta(hours=8),
            now,
            {symbol: 0.0001},
            {symbol: 8},
            {symbol: 100.0},
        )
    assert legacy.model_dump(mode="json") == before


def test_funding_metadata_for_every_held_symbol_is_mandatory_and_atomic():
    symbol = "ETH/USDT:USDT"
    start = datetime(2026, 9, 1, 0, 7, tzinfo=UTC)
    end = start + timedelta(hours=8)
    account = PaperAccount(
        cash=20_000.0,
        last_funding_ts=start,
        positions={
            symbol: Position(
                symbol=symbol,
                direction="long",
                qty=1.0,
                entry_price=100.0,
                opened_ts=start,
            )
        },
    )
    before = account.model_dump(mode="json")

    with pytest.raises(ValueError, match="funding intervals missing held symbols"):
        account.settle_funding(start, end, {symbol: 0.001}, {}, {symbol: 100.0})
    assert account.model_dump(mode="json") == before

    with pytest.raises(ValueError, match="observed funding intervals missing held symbols"):
        account.settle_funding_events({symbol: []}, now=end, observed_intervals={})
    assert account.model_dump(mode="json") == before


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: Position(
                symbol="A",
                direction="long",
                qty=float("nan"),
                entry_price=1.0,
                opened_ts=datetime(2026, 9, 1, tzinfo=UTC),
            ),
            "finite number",
        ),
        (
            lambda: Position(
                symbol="A",
                direction="long",
                qty=1.0,
                entry_price=-1.0,
                opened_ts=datetime(2026, 9, 1, tzinfo=UTC),
            ),
            "greater than 0",
        ),
        (
            lambda: ClosedLeg(symbol="A", direction="long", fees=-0.01),
            "greater than or equal to 0",
        ),
        (lambda: PaperAccount(cash=-1.0), "greater than or equal to 0"),
        (lambda: PaperAccount(cash=1.0, slippage_paid=float("inf")), "finite number"),
    ],
)
def test_account_models_reject_nonfinite_or_economically_impossible_values(factory, match):
    with pytest.raises(ValidationError, match=match):
        factory()


def test_account_models_forbid_unknown_fields_and_inconsistent_keys():
    now = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Position.model_validate(
            {
                "symbol": "A",
                "direction": "long",
                "qty": 1.0,
                "entry_price": 1.0,
                "opened_ts": now,
                "unknown": "not state",
            }
        )
    position = Position(
        symbol="A", direction="long", qty=1.0, entry_price=1.0, opened_ts=now
    )
    with pytest.raises(ValidationError, match="differs from Position.symbol"):
        PaperAccount(cash=20_000.0, positions={"B": position})
    with pytest.raises(ValidationError, match="non-held symbols"):
        PaperAccount(cash=20_000.0, funding_intervals_observed={"A": 8})
    with pytest.raises(ValidationError, match="invalid held funding intervals"):
        PaperAccount(
            cash=20_000.0,
            positions={"A": position},
            funding_intervals_observed={"A": 3},
        )


def test_invalid_persisted_account_halts_instead_of_cold_starting(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "account.json").write_text('{"cash": -1, "positions": {}}')
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        load_account(state, default_cash=20_000.0)

    (state / "account.json").write_text(
        """{
          "cash": 20000,
          "positions": {
            "A": {
              "symbol": "A", "direction": "long", "qty": 1,
              "entry_price": 100, "opened_ts": "2026-09-01T00:00:00Z"
            }
          }
        }"""
    )
    with pytest.raises(ValueError, match="held account has no funding clock"):
        load_account(state, default_cash=20_000.0)


def test_post_construction_nonfinite_mutation_cannot_be_serialized():
    account = PaperAccount(cash=20_000.0)
    account.cash = float("nan")
    with pytest.raises(ValidationError, match="finite number"):
        account.to_dict()


def test_cycle_report_rejects_nonfinite_values_and_unknown_fields():
    base = {
        "cycle": 1,
        "achieved_deploy_frac": 0.9,
        "achieved_dollar_residual_frac": 0.0,
        "achieved_beta_residual": 0.0,
        "equity": 20_000.0,
        "n_legs": 4,
    }
    with pytest.raises(ValidationError, match="finite number"):
        CycleReport.model_validate({**base, "equity": float("nan")})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CycleReport.model_validate({**base, "unknown": True})


def test_position_opened_this_cycle_earns_zero_funding_this_cycle():
    """Settle-BEFORE-fill: a leg opened in the same cycle as settle_funding earns no funding for
    that cycle's window (it did not exist for the pre-existence span)."""
    acct = PaperAccount(cash=20_000.0)
    costs = {"ETH/USDT:USDT": CostInputs(adv_usd=5_000_000.0, half_spread_bps=0.0)}
    prev = datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
    now = datetime(2026, 6, 11, 0, 0, tzinfo=UTC)  # 24h -> 3 settlements IF the leg existed
    # ORDER MATTERS: settle first (no positions yet -> 0), THEN open.
    acct.settle_funding(prev, now, {"ETH/USDT:USDT": 0.0005}, {"ETH/USDT:USDT": 8}, {})
    acct.apply_fills(
        [{"symbol": "ETH/USDT:USDT", "direction": "short", "target_notional": 4000.0}],
        {"ETH/USDT:USDT": 2000.0}, costs, opened_ts=now)
    assert acct.funding_received == 0.0            # opened AFTER settle -> no funding this cycle
    assert acct.positions["ETH/USDT:USDT"].accrued_funding == 0.0


def test_weekly_cycle2_resend_does_not_double_qty():
    """The multi-week double-count guard at the book level: re-applying the IDENTICAL full weekly
    book on cycle 2 reconciles to delta 0 -> qty unchanged, frictions unchanged."""
    acct = PaperAccount(cash=20_000.0)
    costs = {
        "ETH/USDT:USDT": CostInputs(adv_usd=5_000_000.0, half_spread_bps=1.0),
        "BTC/USDT:USDT": CostInputs(adv_usd=5_000_000.0, half_spread_bps=1.0),
    }
    marks = {"ETH/USDT:USDT": 2000.0, "BTC/USDT:USDT": 60_000.0}
    book = [
        {"symbol": "ETH/USDT:USDT", "direction": "long", "target_notional": 4000.0},
        {"symbol": "BTC/USDT:USDT", "direction": "short", "target_notional": 6000.0},
    ]
    acct.apply_fills(book, marks, costs, opened_ts=datetime(2026, 6, 10, tzinfo=UTC))
    qty1 = {s: p.qty for s, p in acct.positions.items()}
    fees1, slip1 = acct.fees_paid, acct.slippage_paid
    # weekly cycle 2: the SAME full book again
    acct.apply_fills(book, marks, costs, opened_ts=datetime(2026, 6, 17, tzinfo=UTC))
    qty2 = {s: p.qty for s, p in acct.positions.items()}
    assert qty2 == qty1                            # NOT doubled to ~2x notional
    assert acct.fees_paid == fees1                 # 0 extra fee on the re-send
    assert acct.slippage_paid == slip1             # 0 extra slippage on the re-send


def test_taker_fee_uses_walked_quote_value_not_midpoint_notional():
    now = datetime(2026, 9, 1, tzinfo=UTC)
    buy = PaperAccount(cash=20_000.0)
    buy.apply_fills(
        [{"symbol": "A", "direction": "long", "target_notional": 200.0}],
        {"A": 100.0},
        {"A": CostInputs(depth_asks=[(110.0, 2.0)], half_spread_bps=0.0)},
        opened_ts=now,
    )
    assert buy.slippage_paid == pytest.approx(20.0)
    assert buy.fees_paid == pytest.approx(220.0 * 0.0005)

    sell = PaperAccount(cash=20_000.0)
    sell.apply_fills(
        [{"symbol": "A", "direction": "short", "target_notional": 200.0}],
        {"A": 100.0},
        {"A": CostInputs(depth_bids=[(90.0, 2.0)], half_spread_bps=0.0)},
        opened_ts=now,
    )
    assert sell.slippage_paid == pytest.approx(20.0)
    assert sell.fees_paid == pytest.approx(180.0 * 0.0005)


def test_zero_turnover_role_change_resets_lifecycle_without_changing_equity():
    symbol = "BTC/USDT:USDT"
    old_ts = datetime(2026, 6, 1, tzinfo=UTC)
    new_ts = datetime(2026, 6, 11, tzinfo=UTC)
    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbol: Position(
                symbol=symbol,
                direction="long",
                qty=0.1,
                entry_price=50_000.0,
                opened_ts=old_ts,
                opened_cycle=3,
                opened_cadence="rebal",
                seat_role="hedge",
                thesis_cycle=3,
                thesis_book_sha256="a" * 64,
                expected_price_edge_frac=0.0,
                edge_horizon_hours=24,
                accrued_funding=3.0,
                accrued_fees=2.0,
                accrued_slippage=1.0,
            )
        },
        fees_paid=2.0,
        slippage_paid=1.0,
        funding_received=3.0,
    )
    marks = {symbol: 60_000.0}
    equity_before = account.equity(marks)

    account.apply_fills(
        [{
            "symbol": symbol,
            "direction": "long",
            "target_notional": 6_000.0,
            "seat_role": "alpha",
            "thesis_cycle": 9,
            "thesis_book_sha256": "b" * 64,
            "expected_price_edge_frac": 0.01,
            "edge_horizon_hours": 72,
            "edge_calibration_basis": "manifest-bound 72h cohort calibration",
            "invalidation_condition": "relative 72h and 168h momentum oppose the long",
        }],
        marks,
        {symbol: CostInputs(adv_usd=1e9, half_spread_bps=0.0)},
        opened_ts=new_ts,
        opened_cycle=9,
        opened_cadence="rebal",
    )

    position = account.positions[symbol]
    assert account.equity(marks) == equity_before
    assert account.cash == 21_000.0
    assert account.fees_paid == 2.0
    assert account.slippage_paid == 1.0
    assert position.qty == 0.1
    assert position.entry_price == 60_000.0
    assert position.opened_ts == new_ts
    assert position.opened_cycle == 9
    assert position.seat_role == "alpha"
    assert position.accrued_funding == 0.0
    assert position.accrued_fees == 0.0
    assert position.accrued_slippage == 0.0
    assert position.thesis_book_sha256 == "b" * 64
    assert len(account.closed_legs) == 1
    closed = account.closed_legs[0]
    assert closed.seat_role == "hedge"
    assert closed.realized_pnl == 1_000.0
    assert closed.realized_funding == 3.0
    assert closed.fees == 2.0
    assert closed.slippage == 1.0


def test_role_change_reduction_charges_exit_to_old_role_then_transfers_residual():
    symbol = "BTC/USDT:USDT"
    old_ts = datetime(2026, 6, 1, tzinfo=UTC)
    new_ts = datetime(2026, 6, 11, tzinfo=UTC)
    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbol: Position(
                symbol=symbol,
                direction="long",
                qty=0.1,
                entry_price=50_000.0,
                opened_ts=old_ts,
                opened_cycle=3,
                opened_cadence="rebal",
                seat_role="hedge",
                accrued_fees=2.0,
            )
        },
        fees_paid=2.0,
    )
    marks = {symbol: 50_000.0}
    equity_before = account.equity(marks)

    account.apply_fills(
        [{
            "symbol": symbol,
            "direction": "long",
            "target_notional": 4_000.0,
            "seat_role": "alpha",
            "thesis_cycle": 9,
            "thesis_book_sha256": "c" * 64,
            "expected_price_edge_frac": 0.01,
            "edge_horizon_hours": 72,
            "edge_calibration_basis": "manifest-bound 72h cohort calibration",
            "invalidation_condition": "relative trend breaks",
        }],
        marks,
        {symbol: CostInputs(
            depth_bids=[(50_000.0, 100.0)],
            depth_asks=[(50_000.0, 100.0)],
            half_spread_bps=0.0,
        )},
        opened_ts=new_ts,
        opened_cycle=9,
        opened_cadence="rebal",
    )

    expected_exit_fee = 1_000.0 * 5.0 / 10_000.0
    position = account.positions[symbol]
    assert account.equity(marks) == equity_before - expected_exit_fee
    assert account.fees_paid == 2.0 + expected_exit_fee
    assert position.qty == 0.08
    assert position.seat_role == "alpha"
    assert position.opened_ts == new_ts
    assert position.entry_price == 50_000.0
    assert position.accrued_fees == 0.0
    assert len(account.closed_legs) == 1
    closed = account.closed_legs[0]
    assert closed.seat_role == "hedge"
    assert closed.fees == 2.0 + expected_exit_fee


def test_held_book_is_dollar_and_beta_neutral_with_a_same_symbol_hedge_alpha_pair():
    """Book-level market-neutrality: a leg-level dollar+beta-neutral book whose legs include the
    SAME symbol on both sides (a BTC factor-short AND a BTC hedge-long) must, AFTER apply_fills,
    hold positions that are STILL dollar-neutral (|Sum long$ - Sum short$| ~ 0) and beta-neutral.

    Pre-fix, apply_fills overwrites BTC per-leg (the hedge-long flips out the factor-short), so the
    HELD book goes net long ~$2118 even though the leg-level book sums to a perfect $9k/$9k — the
    desk is silently NOT market-neutral in its actual positions."""
    # leg-level book: $9013 long / $9013 short, beta-neutral, with BTC on BOTH sides.
    #   BTC factor-short $2116 (beta 1.0) + BTC hedge-long $2129 (beta 1.0)  -> net BTC +$13 long
    #   ETH long  $6884 (beta 1.0)  | SOL short $6871 (beta 1.0) + a hedge short $26 (beta 1.0)
    marks = {"BTC/USDT:USDT": 60_000.0, "ETH/USDT:USDT": 3000.0, "SOL/USDT:USDT": 150.0}
    betas = {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0, "SOL/USDT:USDT": 1.0}
    legs = [
        {"symbol": "BTC/USDT:USDT", "direction": "short", "target_notional": 2116.0},  # factor
        {"symbol": "BTC/USDT:USDT", "direction": "long", "target_notional": 2129.0},   # hedge
        {"symbol": "ETH/USDT:USDT", "direction": "long", "target_notional": 6884.0},
        {"symbol": "SOL/USDT:USDT", "direction": "short", "target_notional": 6871.0},
        {"symbol": "SOL/USDT:USDT", "direction": "short", "target_notional": 26.0},    # hedge
    ]
    # leg-level book is balanced: long 2129+6884 = 9013 ; short 2116+6871+26 = 9013.
    long_legs = sum(lg["target_notional"] for lg in legs if lg["direction"] == "long")
    short_legs = sum(lg["target_notional"] for lg in legs if lg["direction"] == "short")
    assert abs(long_legs - short_legs) < 1e-6          # leg-level book IS dollar-neutral ($9013)

    acct = PaperAccount(cash=100_000.0)
    costs = {s: CostInputs(adv_usd=5_000_000.0, half_spread_bps=0.0) for s in marks}
    acct.apply_fills(legs, marks, costs, opened_ts=datetime(2026, 6, 10, tzinfo=UTC))

    # HELD positions must reconstruct the SAME neutrality the leg-level book had.
    held_long = sum(p.qty * marks[s] for s, p in acct.positions.items() if p.direction == "long")
    held_short = sum(p.qty * marks[s] for s, p in acct.positions.items() if p.direction == "short")
    assert abs(held_long - held_short) < 1.0           # dollar-neutral held book (~$26 net, < $1k)
    # beta residual = Sum(signed notional * beta) / equity ~ 0
    beta_resid = sum(
        (p.qty * marks[s] if p.direction == "long" else -p.qty * marks[s]) * betas[s]
        for s, p in acct.positions.items())
    assert abs(beta_resid) < 1.0                        # beta-neutral held book
    # BTC is a SINGLE consolidated net position (~+$13), not a full $2129 flip.
    btc = acct.positions["BTC/USDT:USDT"]
    assert btc.direction == "long" and abs(btc.qty * marks["BTC/USDT:USDT"] - 13.0) < 1e-6


def _held_long_short(acct, marks):
    """(Sum long $, Sum short $) over the HELD positions at `marks`."""
    long_usd = sum(p.qty * marks[s] for s, p in acct.positions.items() if p.direction == "long")
    short_usd = sum(p.qty * marks[s] for s, p in acct.positions.items() if p.direction == "short")
    return long_usd, short_usd


def _held_beta_resid(acct, marks, betas):
    """Beta residual = Sum(signed notional * beta) over the HELD positions."""
    return sum(
        (p.qty * marks[s] if p.direction == "long" else -p.qty * marks[s]) * betas[s]
        for s, p in acct.positions.items())


def test_full_neutral_book_held_then_dropped_symbol_closed_and_still_neutral():
    """HARD INVARIANT (the live market-neutrality bug). Feed the FULL intended (consolidated,
    dollar+beta-neutral, BTC-hedged) book into apply_fills; the HELD positions must reconstruct the
    SAME neutrality (|Sum long$ - Sum short$| < $5 AND beta-neutral) and each held per-symbol net
    must equal the intended per-symbol net. Then a SECOND cycle's FULL book that DROPS a symbol and
    re-hedges must CLOSE the dropped symbol (gone from positions) and STILL be dollar+beta-neutral,
    equal to the new intended book.

    Pre-fix FAILS: feeding the full book each cycle is not the live wiring (the live loop fed the
    sparse daily deltas), and even at the apply_fills level a symbol DROPPED from the new book is
    never flattened — it lingers, so the held book is net-imbalanced and keeps the dropped name."""
    marks = {"BTC/USDT:USDT": 60_000.0, "ETH/USDT:USDT": 3000.0,
             "SOL/USDT:USDT": 150.0, "XRP/USDT:USDT": 0.6}
    betas = {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0, "SOL/USDT:USDT": 1.0, "XRP/USDT:USDT": 1.0}
    costs = {s: CostInputs(adv_usd=5_000_000.0, half_spread_bps=0.0) for s in marks}

    # CYCLE 1 — full neutral book: $9000 long / $9000 short, BTC hedge included on BOTH sides.
    #   BTC factor-short $3000 + BTC hedge-long $3000 -> net BTC flat (0)
    #   ETH long $6000 | SOL short $3000 + XRP short $3000
    book1 = [
        {"symbol": "BTC/USDT:USDT", "direction": "short", "target_notional": 3000.0},  # factor
        {"symbol": "BTC/USDT:USDT", "direction": "long", "target_notional": 3000.0},   # hedge
        {"symbol": "ETH/USDT:USDT", "direction": "long", "target_notional": 6000.0},
        {"symbol": "SOL/USDT:USDT", "direction": "short", "target_notional": 3000.0},
        {"symbol": "XRP/USDT:USDT", "direction": "short", "target_notional": 3000.0},
    ]
    intended1 = _net_intended(book1)                   # {sym: signed $} the book intends to HOLD
    acct = PaperAccount(cash=100_000.0)
    acct.apply_fills(book1, marks, costs, opened_ts=datetime(2026, 6, 10, tzinfo=UTC))

    long1, short1 = _held_long_short(acct, marks)
    assert abs(long1 - short1) < 5.0                   # held book dollar-neutral
    assert abs(_held_beta_resid(acct, marks, betas)) < 5.0   # held book beta-neutral
    # held per-symbol net == intended per-symbol net
    for sym, signed in intended1.items():
        held = _signed_held(acct, sym, marks)
        assert abs(held - signed) < 1e-6, f"{sym}: held {held} != intended {signed}"

    # CYCLE 2 — full neutral book that DROPS XRP and RE-HEDGES: $9000 long / $9000 short.
    #   BTC factor-short $2000 + BTC hedge-long $2000 -> net BTC flat
    #   ETH long $7000 | SOL short $7000      (XRP absent -> must be CLOSED)
    book2 = [
        {"symbol": "BTC/USDT:USDT", "direction": "short", "target_notional": 2000.0},
        {"symbol": "BTC/USDT:USDT", "direction": "long", "target_notional": 2000.0},
        {"symbol": "ETH/USDT:USDT", "direction": "long", "target_notional": 7000.0},
        {"symbol": "SOL/USDT:USDT", "direction": "short", "target_notional": 7000.0},
    ]
    intended2 = _net_intended(book2)
    acct.apply_fills(book2, marks, costs, opened_ts=datetime(2026, 6, 17, tzinfo=UTC))

    assert "XRP/USDT:USDT" not in acct.positions       # DROPPED symbol was flattened
    long2, short2 = _held_long_short(acct, marks)
    assert abs(long2 - short2) < 5.0                   # STILL dollar-neutral after the drop
    assert abs(_held_beta_resid(acct, marks, betas)) < 5.0   # STILL beta-neutral
    for sym, signed in intended2.items():
        held = _signed_held(acct, sym, marks)
        assert abs(held - signed) < 1e-6, f"{sym}: held {held} != intended {signed}"


def test_refeeding_identical_full_book_trades_nothing():
    """No-churn: re-feeding the IDENTICAL full book reconciles to delta 0 on every leg -> no new
    fees/slippage, positions unchanged (the account stays put)."""
    marks = {"BTC/USDT:USDT": 60_000.0, "ETH/USDT:USDT": 3000.0, "SOL/USDT:USDT": 150.0}
    costs = {s: CostInputs(adv_usd=5_000_000.0, half_spread_bps=1.0) for s in marks}
    book = [
        {"symbol": "BTC/USDT:USDT", "direction": "short", "target_notional": 3000.0},
        {"symbol": "BTC/USDT:USDT", "direction": "long", "target_notional": 3000.0},
        {"symbol": "ETH/USDT:USDT", "direction": "long", "target_notional": 6000.0},
        {"symbol": "SOL/USDT:USDT", "direction": "short", "target_notional": 6000.0},
    ]
    acct = PaperAccount(cash=100_000.0)
    acct.apply_fills(book, marks, costs, opened_ts=datetime(2026, 6, 10, tzinfo=UTC))
    qty1 = {s: (p.direction, p.qty) for s, p in acct.positions.items()}
    fees1, slip1 = acct.fees_paid, acct.slippage_paid
    cash1 = acct.cash

    acct.apply_fills(book, marks, costs, opened_ts=datetime(2026, 6, 17, tzinfo=UTC))  # identical
    qty2 = {s: (p.direction, p.qty) for s, p in acct.positions.items()}
    assert qty2 == qty1                                # positions unchanged
    assert acct.fees_paid == fees1                     # 0 new fee
    assert acct.slippage_paid == slip1                 # 0 new slippage
    assert acct.cash == cash1                          # account stayed put


def _net_intended(book):
    """Net signed $ per symbol the leg-level book intends to HOLD (long +, short -)."""
    net = {}
    for leg in book:
        sign = 1.0 if leg["direction"] == "long" else -1.0
        net[leg["symbol"]] = net.get(leg["symbol"], 0.0) + sign * leg["target_notional"]
    return {s: v for s, v in net.items() if abs(v) > 1e-9}


def _signed_held(acct, sym, marks):
    """Signed held $ for a symbol (0 if flat/absent)."""
    pos = acct.positions.get(sym)
    if pos is None:
        return 0.0
    return (pos.qty if pos.direction == "long" else -pos.qty) * marks[sym]


def test_two_cycle_equity_moves_off_constant_with_funding_and_fees():
    acct = PaperAccount(cash=20_000.0)
    costs = {"ETH/USDT:USDT": CostInputs(adv_usd=5_000_000.0, half_spread_bps=1.0)}
    t0 = datetime(2026, 6, 10, 0, 0, tzinfo=UTC)

    # cycle 1: settle (no positions -> 0), then open a short carry leg
    marks1 = {"ETH/USDT:USDT": 2000.0}
    opening1 = acct.equity(marks1)
    acct.settle_funding(t0, t0, {"ETH/USDT:USDT": 0.0005}, {"ETH/USDT:USDT": 8}, marks1)
    acct.apply_fills(
        [{"symbol": "ETH/USDT:USDT", "direction": "short", "target_notional": 4000.0}],
        marks1, costs, opened_ts=t0)
    rec1 = build_cycle_pnl(acct, opening_equity=opening1, marks=marks1,
                           turnover_usd=4000.0, cycle=1, cadence="weekly", now=t0)
    assert rec1["closing_equity"] != 20_000.0      # frictions moved equity off the constant
    assert rec1["fees_paid"] > 0.0
    assert rec1["slippage_paid"] > 0.0
    assert rec1["funding_received"] == 0.0         # leg opened AFTER cycle-1 settle

    # cycle 2 (one sim-day later): settle funding (3 events at 8h) from the account clock, re-mark
    t1 = t0 + timedelta(days=1)
    marks2 = {"ETH/USDT:USDT": 2000.0}
    opening2 = acct.equity(marks2)
    acct.settle_funding(acct.last_funding_ts, t1, {"ETH/USDT:USDT": 0.0005},
                        {"ETH/USDT:USDT": 8}, marks2)
    rec2 = build_cycle_pnl(acct, opening_equity=opening2, marks=marks2,
                           turnover_usd=0.0, cycle=2, cadence="daily", now=t1)

    assert rec2["funding_received"] > 0.0          # short + positive rate = received over the day
    assert rec2["funding_net"] > 0.0
    equities = [rec1["closing_equity"], rec2["closing_equity"]]
    assert len(set(equities)) == 2                 # not a flat 20000
    assert all(e != 20_000.0 for e in equities)
    assert rec2["net_pnl"] == rec2["gross_pnl"] - rec2["fees_paid"] - rec2["slippage_paid"]
