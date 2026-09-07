from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from futures_fund.account import CostInputs, PaperAccount, Position
from futures_fund.agent_runner import StubAgentRunner
from futures_fund.costs import vwap_fill
from futures_fund.desk_contracts import Book, BookLeg, SpecialistRead
from futures_fund.desk_cycle import (
    _achieved_execution_safety,
    _execution_inputs,
    _execution_target_audit,
    _verify_execution_liquidity,
    _verify_fresh_execution_economics,
    reconcile_book,
    run_adversary,
    run_pm,
    run_specialists,
)
from futures_fund.evidence import EvidencePack
from futures_fund.precheck import compute_precheck
from futures_fund.slippage import ExecutionRealism, estimate_slippage, haircut_depth
from tests.conftest import make_verdict

NOW = datetime(2026, 7, 7, tzinfo=UTC)
EV = [EvidencePack(symbol="SOL/USDT:USDT", mark=100.0, as_of_ts=NOW)]


def _execution_spec(step_size=0.001, min_notional=5.0):
    return SimpleNamespace(
        step_size=step_size, tick_size=0.01, min_notional=min_notional, max_qty=None
    )


def _fresh_entry_economics_fixture(
    *,
    best_bid: float,
    best_ask: float,
    expected_price_edge_frac: float,
    edge_horizon_hours: int = 24,
):
    symbols = ("L1", "L2", "L3", "S1", "S2", "S3")
    decision_marks = dict.fromkeys(symbols, 100.0)
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="long" if symbol.startswith("L") else "short",
                target_notional=3_000.0,
                expected_price_edge_frac=expected_price_edge_frac,
                edge_horizon_hours=edge_horizon_hours,
                is_new=True,
                hold_breaking_reason="agent-authorized entry",
            )
            for symbol in symbols
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
        turnover_legs_changed=len(symbols),
    )
    cheap_evidence = [
        {
            "symbol": symbol,
            "mark": 100.0,
            "beta_clamped": 1.0,
            "conservative_funding_8h_bps": 0.0,
            "est_slippage_bps_2k": 1.0,
            "liquidity_mid": 100.0,
            "depth_usd_bid": 1_000_000.0,
            "depth_usd_ask": 1_000_000.0,
            "slippage_curve_bps": {"2k": 1.0, "5k": 1.0, "10k": 1.0, "20k": 1.0},
            "slippage_curve_buy_bps": {
                "2k": 1.0,
                "5k": 1.0,
                "10k": 1.0,
                "20k": 1.0,
            },
            "slippage_curve_sell_bps": {
                "2k": 1.0,
                "5k": 1.0,
                "10k": 1.0,
                "20k": 1.0,
            },
        }
        for symbol in symbols
    ]
    policy = ExecutionRealism(
        latency_ms=0.0,
        displayed_depth_fraction=1.0,
        adverse_selection_bps=1.0,
        legging_bps_per_second=0.0,
        allow_partial_fills=False,
    )
    precheck = compute_precheck(
        book,
        cheap_evidence,
        cash=20_000.0,
        cycle=1,
        execution_realism=policy,
    )

    class _FreshBook:
        def symbol_spec(self, _symbol):
            return _execution_spec(step_size=0.01, min_notional=5.0)

        def depth(self, _symbol):
            return {
                "bids": [(best_bid, 10_000.0)],
                "asks": [(best_ask, 10_000.0)],
            }

    execution_marks, costs, execution_audit, _ = _execution_inputs(
        _FreshBook(),
        set(symbols),
        decision_marks,
        execution_realism=policy,
    )
    account = PaperAccount(cash=20_000.0)
    target_audit = _execution_target_audit(
        account,
        book,
        decision_marks,
        execution_marks,
        execution_audit,
    )
    _verify_execution_liquidity(target_audit, execution_audit)
    return account, book, precheck, target_audit, execution_audit, costs


def _decision_before_execution(
    execution_audit: dict[str, dict], *, hours: float = 0.0
) -> datetime:
    first_execution = min(
        datetime.fromisoformat(str(row["execution_ts"]))
        for row in execution_audit.values()
    )
    return first_execution - timedelta(hours=hours)


def _group_execution_ts(execution_audit: dict[str, dict]) -> datetime:
    return max(
        datetime.fromisoformat(str(row["execution_ts"]))
        for row in execution_audit.values()
    )


def _cheap_evidence(
    symbols: tuple[str, ...], *, betas: dict[str, float] | None = None
) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "mark": 100.0,
            "beta_clamped": (betas or {}).get(symbol, 1.0),
            "conservative_funding_8h_bps": 0.0,
            "est_slippage_bps_2k": 1.0,
            "liquidity_mid": 100.0,
            "depth_usd_bid": 1_000_000.0,
            "depth_usd_ask": 1_000_000.0,
            "slippage_curve_bps": {
                "2k": 1.0,
                "5k": 1.0,
                "10k": 1.0,
                "20k": 1.0,
            },
            "slippage_curve_buy_bps": {
                "2k": 1.0,
                "5k": 1.0,
                "10k": 1.0,
                "20k": 1.0,
            },
            "slippage_curve_sell_bps": {
                "2k": 1.0,
                "5k": 1.0,
                "10k": 1.0,
                "20k": 1.0,
            },
        }
        for symbol in symbols
    ]


def _fresh_resize_economics_fixture(
    *,
    increase_symbol: str,
    fresh_mid: float,
    half_spread: float,
    expected_price_edge_frac: float,
):
    symbols = ("L1", "L2", "S1", "S2")
    sides = {"L1": "long", "L2": "long", "S1": "short", "S2": "short"}
    current_notionals = {"L1": 4_000.0, "L2": 4_500.0, "S1": 4_000.0, "S2": 4_500.0}
    target_notionals = dict(current_notionals)
    target_notionals[increase_symbol] += 500.0
    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbol: Position(
                symbol=symbol,
                direction=sides[symbol],
                qty=notional / 100.0,
                entry_price=100.0,
                opened_ts=NOW,
            )
            for symbol, notional in current_notionals.items()
        },
        last_funding_ts=NOW,
    )
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side=sides[symbol],
                target_notional=target_notionals[symbol],
                expected_price_edge_frac=expected_price_edge_frac,
                edge_horizon_hours=24,
            )
            for symbol in symbols
        ],
        stated_deploy_frac=sum(target_notionals.values()) / 20_000.0,
        stated_dollar_residual_frac=500.0 / sum(target_notionals.values()),
        stated_beta_residual=(
            500.0 / 20_000.0 if increase_symbol.startswith("L") else -500.0 / 20_000.0
        ),
        turnover_legs_changed=1,
    )
    current_book = [
        {
            "symbol": symbol,
            "side": sides[symbol],
            "target_notional": notional,
            "seat_role": "alpha",
        }
        for symbol, notional in current_notionals.items()
    ]
    policy = ExecutionRealism(
        latency_ms=0.0,
        displayed_depth_fraction=1.0,
        adverse_selection_bps=0.0,
        legging_bps_per_second=0.0,
        allow_partial_fills=False,
    )
    evidence = _cheap_evidence(symbols)
    precheck = compute_precheck(
        book,
        evidence,
        cash=20_000.0,
        cycle=2,
        current_book=current_book,
        execution_realism=policy,
    )
    fresh_mids = {"L1": 103.0, "L2": 97.0, "S1": 96.0, "S2": 102.0}
    fresh_mids[increase_symbol] = fresh_mid

    class _FreshBook:
        def symbol_spec(self, _symbol):
            return _execution_spec(step_size=0.01, min_notional=5.0)

        def depth(self, symbol):
            midpoint = fresh_mids[symbol]
            spread = half_spread if symbol == increase_symbol else 0.01
            return {
                "bids": [(midpoint - spread, 10_000.0)],
                "asks": [(midpoint + spread, 10_000.0)],
            }

    execution_marks, costs, execution_audit, _ = _execution_inputs(
        _FreshBook(),
        set(symbols),
        dict.fromkeys(symbols, 100.0),
        execution_realism=policy,
    )
    target_audit = _execution_target_audit(
        account,
        book,
        dict.fromkeys(symbols, 100.0),
        execution_marks,
        execution_audit,
    )
    _verify_execution_liquidity(target_audit, execution_audit)
    return book, precheck, target_audit, execution_audit, costs


def _fresh_hedge_economics_fixture(*, fresh_btc_mark: float):
    btc = "BTC/USDT:USDT"
    symbols = ("L1", "L2", "L3", "S1", "S2", btc)
    target_notionals = {
        "L1": 3_000.0,
        "L2": 3_000.0,
        "L3": 3_000.0,
        "S1": 3_500.0,
        "S2": 3_500.0,
        btc: 2_000.0,
    }
    betas = {
        "L1": 0.39444444444444443,
        "L2": 0.39444444444444443,
        "L3": 0.39444444444444443,
        "S1": 0.15,
        "S2": 0.15,
        btc: 1.0,
    }
    sides = {symbol: ("long" if symbol.startswith("L") else "short") for symbol in symbols}
    current_notionals = {**target_notionals, btc: 1_000.0}
    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbol: Position(
                symbol=symbol,
                direction=sides[symbol],
                qty=notional / 100.0,
                entry_price=100.0,
                opened_ts=NOW,
                seat_role="hedge" if symbol == btc else "alpha",
            )
            for symbol, notional in current_notionals.items()
        },
        last_funding_ts=NOW,
    )
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side=sides[symbol],
                target_notional=notional,
                seat_role="hedge" if symbol == btc else "alpha",
            )
            for symbol, notional in target_notionals.items()
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.025,
        turnover_legs_changed=1,
    )
    current_book = [
        {
            "symbol": symbol,
            "side": sides[symbol],
            "target_notional": notional,
            "seat_role": "hedge" if symbol == btc else "alpha",
        }
        for symbol, notional in current_notionals.items()
    ]
    policy = ExecutionRealism(
        latency_ms=0.0,
        displayed_depth_fraction=1.0,
        adverse_selection_bps=0.0,
        legging_bps_per_second=0.0,
        allow_partial_fills=False,
    )
    precheck = compute_precheck(
        book,
        _cheap_evidence(symbols, betas=betas),
        cash=20_000.0,
        cycle=2,
        current_book=current_book,
        execution_realism=policy,
    )
    fresh_mids = {
        "L1": 100.0,
        "L2": 100.0,
        "L3": 100.0,
        "S1": 500.0 / 7.0,
        "S2": 500.0 / 7.0,
        btc: fresh_btc_mark,
    }

    class _FreshBook:
        def symbol_spec(self, _symbol):
            return _execution_spec(step_size=0.01, min_notional=5.0)

        def depth(self, symbol):
            midpoint = fresh_mids[symbol]
            return {
                "bids": [(midpoint - 0.01, 10_000.0)],
                "asks": [(midpoint + 0.01, 10_000.0)],
            }

    execution_marks, costs, execution_audit, _ = _execution_inputs(
        _FreshBook(),
        set(symbols),
        dict.fromkeys(symbols, 100.0),
        execution_realism=policy,
    )
    target_audit = _execution_target_audit(
        account,
        book,
        dict.fromkeys(symbols, 100.0),
        execution_marks,
        execution_audit,
    )
    _verify_execution_liquidity(target_audit, execution_audit)
    return book, precheck, target_audit, execution_audit, costs, betas


def _read(sym, lean):
    return [SpecialistRead(symbol=sym, lean=lean, conviction=0.6, rationale="r", evidence=[])]


def test_run_specialists_collects_all_roles():
    runner = StubAgentRunner(canned={"sentiment": _read("SOL/USDT:USDT", "long"),
                                     "technical": _read("SOL/USDT:USDT", "long"),
                                     "futures": _read("SOL/USDT:USDT", "short")})
    out = run_specialists(runner, EV)
    assert set(out) == {"sentiment", "technical", "futures"}
    assert out["futures"][0].lean == "short"


def test_run_specialists_drops_a_failed_role():
    class _Partial(StubAgentRunner):
        def run(self, role, prompt, schema):
            if role == "technical":
                raise RuntimeError("timeout")
            return super().run(role, prompt, schema)
    runner = _Partial(canned={"sentiment": _read("SOL/USDT:USDT", "long"),
                              "futures": _read("SOL/USDT:USDT", "short")})
    out = run_specialists(runner, EV)
    assert out["technical"] == []          # dropped, not crashed
    assert out["sentiment"] and out["futures"]


def test_run_pm_returns_the_pm_book():
    book = Book(
        legs=[
            BookLeg(symbol="SOL/USDT:USDT", side="long", target_notional=9000.0, rationale="r"),
            BookLeg(symbol="XRP/USDT:USDT", side="short", target_notional=9000.0, rationale="r"),
        ],
        stated_deploy_frac=0.9, stated_dollar_residual_frac=0.0, stated_beta_residual=0.0)
    runner = StubAgentRunner(canned={"pm": book})
    out = run_pm(runner, {"sentiment": []}, EV, cash=20000.0)
    assert isinstance(out, Book) and len(out.legs) == 2


def _book(notional):
    return Book(
        legs=[
            BookLeg(symbol="SOL/USDT:USDT", side="long", target_notional=notional, rationale="r"),
            BookLeg(symbol="XRP/USDT:USDT", side="short", target_notional=notional, rationale="r"),
        ],
        stated_deploy_frac=0.9, stated_dollar_residual_frac=0.0, stated_beta_residual=0.0)


def test_adversary_accept_keeps_book():
    runner = StubAgentRunner(canned={"adversary": make_verdict(True)})
    verdict, final = run_adversary(runner, _book(9000.0), {}, EV, cash=20000.0)
    assert verdict.accept and final.legs[0].target_notional == 9000.0


def test_adversary_reject_triggers_one_pm_revision():
    revised = _book(9500.0)
    runner = StubAgentRunner(canned={
        "adversary": make_verdict(
            False, objections=["deployment is too low"], demanded_changes=["deploy more"]
        ),
        "pm_revise": revised})
    verdict, final = run_adversary(runner, _book(9000.0), {}, EV, cash=20000.0)
    assert verdict.accept is False
    assert final.legs[0].target_notional == 9500.0     # the single revision was applied


def test_reconcile_fills_and_reports_neutrality():
    acct = PaperAccount(cash=20000.0)
    book = _book(9000.0)   # 9000 long SOL, 9000 short XRP
    marks = {"SOL/USDT:USDT": 100.0, "XRP/USDT:USDT": 1.0}
    costs = {s: CostInputs(adv_usd=1e9, half_spread_bps=1.0) for s in marks}
    betas = {"SOL/USDT:USDT": 1.2, "XRP/USDT:USDT": 1.0}
    rep = reconcile_book(acct, book, marks=marks, costs=costs, betas=betas,
                         now=NOW, cycle=1, cadence="rebal",
                         enforce_achieved_safety=False)
    assert rep.n_legs == 2
    assert rep.achieved_deploy_frac > 0.0
    # 9000 long vs 9000 short -> ~dollar neutral
    assert abs(rep.achieved_dollar_residual_frac) < 0.01
    assert acct.positions                                   # fills happened


def test_execution_inputs_do_not_charge_decision_to_execution_drift_as_slippage():
    """A delayed fill must use the fresh book midpoint, not the old decision mark.

    The agents saw 100.00, then the whole market moved to roughly 110.00 before reconcile.
    Only crossing the fresh 109.90/110.10 book is slippage; the intervening 10% move happened
    before the paper position existed and must not be charged as an execution cost.
    """

    class _DriftedBook:
        def symbol_spec(self, symbol):
            return _execution_spec()

        def depth(self, symbol):
            assert symbol == "SOL/USDT:USDT"
            return {
                "bids": [(109.90, 1_000_000.0)],
                "asks": [(110.10, 1_000_000.0)],
            }

        def mark_price(self, symbol):
            raise AssertionError("a complete two-sided book must supply its own midpoint")

    marks, costs, audit, execution_ts = _execution_inputs(
        _DriftedBook(),
        {"SOL/USDT:USDT"},
        {"SOL/USDT:USDT": 100.0},
    )

    assert execution_ts.tzinfo is not None
    assert marks["SOL/USDT:USDT"] == pytest.approx(110.0)
    assert audit["SOL/USDT:USDT"]["price_source"] == "book_mid"
    assert audit["SOL/USDT:USDT"]["decision_to_execution_bps"] == pytest.approx(1000.0)

    account = PaperAccount(cash=20_000.0)
    account.apply_fills(
        [{"symbol": "SOL/USDT:USDT", "direction": "long", "target_notional": 5_000.0}],
        marks,
        costs,
    )

    # Fresh half-spread is ~9.09bps plus the conservative 1bp adverse-selection reserve: about
    # $5.05 on $5k, not ~$505 from comparing 110.10 to the stale decision mark.
    assert account.slippage_paid == pytest.approx(
        5_000.0 * ((110.10 - 110.0) / 110.0 + 1.0 / 1e4)
    )
    assert account.slippage_paid < 6.0
    assert account.positions["SOL/USDT:USDT"].entry_price == pytest.approx(110.0)
    assert audit["SOL/USDT:USDT"]["submission_at"]
    assert audit["SOL/USDT:USDT"]["observed_at"]
    assert audit["SOL/USDT:USDT"]["execution_ts"]


def test_six_leg_cheap_precheck_halts_on_wide_fresh_books_before_mutation():
    account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=90.0,
        best_ask=110.0,
        expected_price_edge_frac=0.05,
    )
    before = account.model_dump(mode="json")
    assert next(bound for bound in precheck.bounds if bound.bound_id == "B10").ok
    assert next(bound for bound in precheck.bounds if bound.bound_id == "B12").ok

    with pytest.raises(RuntimeError, match="fresh B10 slippage"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            execution,
            costs,
            dict.fromkeys(targets, 1.0),
            decision_ts=_decision_before_execution(execution),
            execution_ts=_group_execution_ts(execution),
            cadence_tf_minutes=1_440,
        )

    assert account.model_dump(mode="json") == before


def test_fresh_execution_cost_audit_separates_market_drift_from_slippage():
    _account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=109.9,
        best_ask=110.1,
        expected_price_edge_frac=0.20,
    )

    summary = _verify_fresh_execution_economics(
        book,
        precheck,
        targets,
        execution,
        costs,
        dict.fromkeys(targets, 1.0),
        decision_ts=_decision_before_execution(execution),
        execution_ts=_group_execution_ts(execution),
        cadence_tf_minutes=1_440,
    )

    long_row = execution["L1"]
    assert summary["fresh_execution_economics_passed"] is True
    assert long_row["fresh_market_drift_bps"] == pytest.approx(1_000.0)
    assert long_row["fresh_market_drift_excluded_from_slippage"] is True
    assert long_row["fresh_execution_book_walk_slippage_bps"] == pytest.approx(
        (110.1 / 110.0 - 1.0) * 1e4
    )
    assert long_row["fresh_execution_total_slippage_bps"] == pytest.approx(
        (110.1 / 110.0 - 1.0) * 1e4 + 1.0
    )
    assert long_row["fresh_favorable_drift_consumed_price_edge_frac"] == pytest.approx(
        0.10
    )
    assert long_row["fresh_remaining_expected_price_edge_frac"] == pytest.approx(0.10)
    assert long_row["fresh_execution_total_slippage_bps"] < 11.0


def test_narrow_fresh_l2_still_halts_when_favorable_drift_consumes_forecast_edge():
    _account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=109.9,
        best_ask=110.1,
        expected_price_edge_frac=0.05,
    )

    with pytest.raises(RuntimeError, match="fresh B12 payback"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            execution,
            costs,
            dict.fromkeys(targets, 1.0),
            decision_ts=_decision_before_execution(execution),
            execution_ts=_group_execution_ts(execution),
            cadence_tf_minutes=1_440,
        )


def test_fresh_l2_below_b10_can_still_invalidate_b12_payback():
    _account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=99.5,
        best_ask=100.5,
        expected_price_edge_frac=0.002,
    )
    assert next(bound for bound in precheck.bounds if bound.bound_id == "B12").ok

    with pytest.raises(RuntimeError, match="fresh B12 payback"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            execution,
            costs,
            dict.fromkeys(targets, 1.0),
            decision_ts=_decision_before_execution(execution),
            execution_ts=_group_execution_ts(execution),
            cadence_tf_minutes=1_440,
        )


def test_fresh_b12_anchors_remaining_long_edge_to_decision_notional():
    book, precheck, targets, execution, costs = _fresh_resize_economics_fixture(
        increase_symbol="L1",
        fresh_mid=108.5,
        half_spread=0.72,
        expected_price_edge_frac=0.10,
    )

    # The $500 decision clip forecast $50. The favorable 8.5% pre-entry move consumed $42.50,
    # leaving $7.50 rather than multiplying the residual 1.5% by the inflated fresh $542.50 clip.
    # Fresh round-trip friction is $7.7425, so authorization is stale despite B10 still passing.
    with pytest.raises(RuntimeError, match="fresh B12 payback"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            execution,
            costs,
            dict.fromkeys(targets, 1.0),
            decision_ts=_decision_before_execution(execution),
            execution_ts=_group_execution_ts(execution),
            cadence_tf_minutes=1_440,
        )


@pytest.mark.parametrize(
    ("symbol", "fresh_mid"),
    [("L1", 108.5), ("S1", 91.5)],
)
def test_fresh_b12_persists_decision_anchored_edge_for_both_sides(symbol, fresh_mid):
    book, precheck, targets, execution, costs = _fresh_resize_economics_fixture(
        increase_symbol=symbol,
        fresh_mid=fresh_mid,
        half_spread=0.65,
        expected_price_edge_frac=0.101,
    )

    _verify_fresh_execution_economics(
        book,
        precheck,
        targets,
        execution,
        costs,
        dict.fromkeys(targets, 1.0),
        decision_ts=_decision_before_execution(execution),
        execution_ts=_group_execution_ts(execution),
        cadence_tf_minutes=1_440,
    )

    row = execution[symbol]
    assert row["fresh_decision_anchored_economic_clip_usd"] == pytest.approx(500.0)
    assert row["fresh_original_forecast_price_edge_usd"] == pytest.approx(50.5)
    assert row["fresh_favorable_drift_consumed_price_edge_usd"] == pytest.approx(42.5)
    assert row["fresh_remaining_forecast_price_edge_usd"] == pytest.approx(8.0)
    assert row["fresh_execution_midpoint_notional_usd"] != pytest.approx(500.0)


def test_fresh_btc_insurance_exemption_halts_when_repricing_makes_hedge_worse():
    book, precheck, targets, execution, costs, betas = _fresh_hedge_economics_fixture(
        fresh_btc_mark=200.0
    )
    btc_row = next(row for row in precheck.change_costs if row.symbol == "BTC/USDT:USDT")
    assert btc_row.b12_insurance_exempt is True

    with pytest.raises(RuntimeError, match="no longer risk-reducing"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            execution,
            costs,
            betas,
            decision_ts=_decision_before_execution(execution),
            execution_ts=_group_execution_ts(execution),
            cadence_tf_minutes=1_440,
        )


def test_fresh_btc_insurance_exemption_persists_both_passing_counterfactuals():
    book, precheck, targets, execution, costs, betas = _fresh_hedge_economics_fixture(
        fresh_btc_mark=100.0
    )

    _verify_fresh_execution_economics(
        book,
        precheck,
        targets,
        execution,
        costs,
        betas,
        decision_ts=_decision_before_execution(execution),
        execution_ts=_group_execution_ts(execution),
        cadence_tf_minutes=1_440,
    )

    review = execution["BTC/USDT:USDT"]["fresh_b12_hedge_exemption_review"]
    assert review["alpha_only_beta_net_usd"] == pytest.approx(2_800.0)
    assert review["proposed_hedge_beta_usd"] == pytest.approx(-2_000.0)
    assert review["carried_hedge_beta_usd"] == pytest.approx(-1_000.0)
    assert review["final_beta_net_usd"] == pytest.approx(800.0)
    assert review["carried_counterfactual_beta_net_usd"] == pytest.approx(1_800.0)
    assert review["risk_reducing_vs_alpha_only"] is True
    assert review["risk_reducing_vs_carried_hedge"] is True
    assert review["fresh_insurance_exemption_valid"] is True


def test_basket_decision_ttl_halts_exactly_at_cadence_even_for_noop_book():
    symbols = ("SOL/USDT:USDT", "XRP/USDT:USDT")
    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbols[0]: Position(
                symbol=symbols[0],
                direction="long",
                qty=90.0,
                entry_price=100.0,
                opened_ts=NOW,
            ),
            symbols[1]: Position(
                symbol=symbols[1],
                direction="short",
                qty=90.0,
                entry_price=100.0,
                opened_ts=NOW,
            ),
        },
        last_funding_ts=NOW,
    )
    book = Book(
        legs=[
            BookLeg(symbol=symbol, side=side, target_notional=9_000.0)
            for symbol, side in zip(symbols, ("long", "short"), strict=True)
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )
    precheck = compute_precheck(
        book,
        _cheap_evidence(symbols),
        cash=20_000.0,
        cycle=2,
        current_book=[
            {
                "symbol": symbol,
                "side": side,
                "target_notional": 9_000.0,
                "seat_role": "alpha",
            }
            for symbol, side in zip(symbols, ("long", "short"), strict=True)
        ],
    )
    targets = _execution_target_audit(
        account,
        book,
        dict.fromkeys(symbols, 100.0),
        dict.fromkeys(symbols, 100.0),
    )
    assert precheck.change_costs == []
    assert all(row["delta_qty_signed"] == 0.0 for row in targets.values())

    with pytest.raises(RuntimeError, match="basket decision cadence TTL expired"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            {},
            {},
            dict.fromkeys(targets, 1.0),
            decision_ts=NOW - timedelta(hours=24),
            execution_ts=NOW,
            cadence_tf_minutes=1_440,
        )


def test_long_horizon_forecast_cannot_outlive_full_cycle_cadence():
    _account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=99.9,
        best_ask=100.1,
        expected_price_edge_frac=0.02,
        edge_horizon_hours=72,
    )
    group_ts = _group_execution_ts(execution)

    with pytest.raises(RuntimeError, match="basket decision cadence TTL expired"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            execution,
            costs,
            dict.fromkeys(targets, 1.0),
            decision_ts=group_ts - timedelta(hours=24),
            execution_ts=group_ts,
            cadence_tf_minutes=1_440,
        )


def test_alpha_forecast_expiry_halts_without_cadence_masking_it():
    _account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=99.9,
        best_ask=100.1,
        expected_price_edge_frac=0.02,
    )
    group_ts = _group_execution_ts(execution)
    first_ts = _decision_before_execution(execution)

    with pytest.raises(RuntimeError, match="alpha forecast expired"):
        _verify_fresh_execution_economics(
            book,
            precheck,
            targets,
            execution,
            costs,
            dict.fromkeys(targets, 1.0),
            decision_ts=first_ts - timedelta(hours=24),
            execution_ts=group_ts,
            cadence_tf_minutes=2_880,
        )


def test_partial_decision_age_retires_forecast_horizon_and_value_in_b12():
    _account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=99.9,
        best_ask=100.1,
        expected_price_edge_frac=0.02,
    )
    first_ts = _decision_before_execution(execution)
    decision_ts = first_ts - timedelta(hours=12)

    summary = _verify_fresh_execution_economics(
        book,
        precheck,
        targets,
        execution,
        costs,
        dict.fromkeys(targets, 1.0),
        decision_ts=decision_ts,
        execution_ts=_group_execution_ts(execution),
        cadence_tf_minutes=1_440,
    )

    row = execution["L1"]
    assert summary["fresh_basket_decision_age_hours"] == pytest.approx(12.0, abs=1e-3)
    assert row["fresh_original_forecast_horizon_hours"] == 24.0
    assert row["fresh_remaining_forecast_horizon_hours"] == pytest.approx(12.0)
    assert row["fresh_forecast_time_remaining_frac"] == pytest.approx(0.5)
    assert row["fresh_original_forecast_price_edge_usd"] == pytest.approx(60.0)
    assert row["fresh_time_adjusted_forecast_price_edge_usd"] == pytest.approx(30.0)
    assert row["fresh_forecast_time_decay_usd"] == pytest.approx(30.0)
    assert row["fresh_b12_expected_edge_through_horizon_usd"] == pytest.approx(30.0)


def test_time_and_favorable_price_use_conservative_caps_without_double_consumption():
    _account, book, precheck, targets, execution, costs = _fresh_entry_economics_fixture(
        best_bid=104.9,
        best_ask=105.1,
        expected_price_edge_frac=0.10,
    )
    first_ts = _decision_before_execution(execution)

    _verify_fresh_execution_economics(
        book,
        precheck,
        targets,
        execution,
        costs,
        dict.fromkeys(targets, 1.0),
        decision_ts=first_ts - timedelta(hours=12),
        execution_ts=_group_execution_ts(execution),
        cadence_tf_minutes=1_440,
    )

    row = execution["L1"]
    assert row["fresh_original_forecast_price_edge_usd"] == pytest.approx(300.0)
    assert row["fresh_time_adjusted_forecast_price_edge_usd"] == pytest.approx(150.0)
    assert row["fresh_favorable_drift_consumed_price_edge_usd"] == pytest.approx(150.0)
    assert row["fresh_price_target_remaining_forecast_price_edge_usd"] == pytest.approx(
        150.0
    )
    assert row["fresh_remaining_forecast_price_edge_usd"] == pytest.approx(150.0)


def test_execution_inputs_allow_fresh_mark_valuation_only_for_a_noop_symbol():
    class _NoBook:
        def symbol_spec(self, symbol):
            return _execution_spec()

        def depth(self, symbol):
            return {"bids": [], "asks": []}

        def mark_price(self, symbol):
            return 120.0

    marks, costs, audit, _ = _execution_inputs(
        _NoBook(),
        {"SOL/USDT:USDT"},
        {"SOL/USDT:USDT": 100.0},
        required_depth_symbols=set(),
    )

    assert marks["SOL/USDT:USDT"] == pytest.approx(120.0)
    assert costs["SOL/USDT:USDT"].depth_bids == []
    assert costs["SOL/USDT:USDT"].depth_asks == []
    assert audit["SOL/USDT:USDT"]["price_source"] == "mark_price"
    assert costs["SOL/USDT:USDT"].adv_usd == 0.0


def test_execution_inputs_fail_closed_without_l2_for_a_changed_symbol():
    class _NoBook:
        def symbol_spec(self, symbol):
            return _execution_spec()

        def depth(self, symbol):
            return {"bids": [], "asks": []}

        def mark_price(self, symbol):
            return 120.0

    with pytest.raises(RuntimeError, match="two-sided execution depth unavailable"):
        _execution_inputs(
            _NoBook(),
            {"SOL/USDT:USDT"},
            {"SOL/USDT:USDT": 100.0},
            required_depth_symbols={"SOL/USDT:USDT"},
        )


@pytest.mark.parametrize(
    ("depth", "message"),
    [
        (
            {"bids": [(99.0, 1.0), (100.0, 2.0)], "asks": [(101.0, 1.0)]},
            "bids must be unique and strictly descending",
        ),
        (
            {"bids": [(99.0, 1.0)], "asks": [(101.0, 1.0), (100.0, 2.0)]},
            "asks must be unique and strictly ascending",
        ),
        (
            {"bids": [(100.0, 1.0), (100.0, 2.0)], "asks": [(101.0, 1.0)]},
            "bids must be unique and strictly descending",
        ),
        (
            {"bids": [(float("nan"), 1.0)], "asks": [(101.0, 1.0)]},
            "positive finite price/qty",
        ),
        (
            {"bids": [(99.0, float("inf"))], "asks": [(101.0, 1.0)]},
            "positive finite price/qty",
        ),
        (
            {"bids": [(99.0, 0.0)], "asks": [(101.0, 1.0)]},
            "positive finite price/qty",
        ),
        (
            {"bids": [(99.0, 1.0, 3.0)], "asks": [(101.0, 1.0)]},
            "level shape",
        ),
        (
            {"bids": [(101.0, 1.0)], "asks": [(100.0, 1.0)]},
            "top is locked or inverted",
        ),
        (
            {"bids": [(100.0, 1.0)], "asks": [(100.0, 1.0)]},
            "top is locked or inverted",
        ),
    ],
)
def test_execution_inputs_reject_malformed_or_unordered_l2(depth, message):
    class _MalformedBook:
        def symbol_spec(self, _symbol):
            return _execution_spec()

        def depth(self, _symbol):
            return depth

    with pytest.raises(RuntimeError, match=message):
        _execution_inputs(_MalformedBook(), {"A"}, {"A": 100.0})


def test_execution_audit_persists_exact_raw_and_effective_ladders_for_replay():
    raw_bids = [(99.5, 1.0), (99.0, 2.0), (98.0, 3.0)]
    raw_asks = [(100.5, 1.0), (101.0, 2.0), (102.0, 3.0)]

    class _ReplayBook:
        def symbol_spec(self, _symbol):
            return _execution_spec()

        def depth(self, _symbol):
            return {"bids": raw_bids, "asks": raw_asks}

    policy = ExecutionRealism(
        latency_ms=0.0,
        displayed_depth_fraction=0.5,
        adverse_selection_bps=2.0,
        legging_bps_per_second=0.0,
    )
    marks, costs, audit, _ = _execution_inputs(
        _ReplayBook(), {"A"}, {"A": 100.0}, execution_realism=policy
    )
    row = audit["A"]

    assert row["l2_validation_passed"] is True
    assert row["raw_l2_used_for_execution"] is True
    assert row["raw_bid_ladder"] == [[price, qty] for price, qty in raw_bids]
    assert row["raw_ask_ladder"] == [[price, qty] for price, qty in raw_asks]
    replay_asks = haircut_depth(
        [tuple(level) for level in row["raw_ask_ladder"]],
        row["displayed_depth_fraction"],
    )
    assert replay_asks == costs["A"].depth_asks
    assert row["effective_ask_ladder"] == [list(level) for level in replay_asks]

    qty = 1.25
    filled, replay_vwap = vwap_fill(replay_asks, qty)
    assert filled == pytest.approx(qty)
    assert replay_vwap == pytest.approx(100.8)
    charged = estimate_slippage(
        "A",
        qty,
        marks["A"],
        depth=costs["A"].depth_asks,
        adv_usd=costs["A"].adv_usd,
        half_spread_bps=costs["A"].half_spread_bps,
        adverse_selection_bps=costs["A"].adverse_selection_bps,
        legging_bps=costs["A"].legging_bps,
    )
    replayed = estimate_slippage(
        "A",
        qty,
        marks["A"],
        depth=[tuple(level) for level in row["effective_ask_ladder"]],
        adv_usd=0.0,
        half_spread_bps=row["half_spread_bps"],
        adverse_selection_bps=row["adverse_selection_bps"],
        legging_bps=row["legging_bps"],
    )
    assert replayed == pytest.approx(charged)
    assert replayed == pytest.approx(1.025)


def test_reconcile_anchors_held_quantity_to_the_decision_mark():
    """A PM hold is a true no-op even if price moves before the fresh execution snapshot."""
    symbol = "SOL/USDT:USDT"
    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbol: Position(
                symbol=symbol,
                direction="long",
                qty=50.0,
                entry_price=95.0,
                opened_ts=NOW,
            )
        },
        last_funding_ts=NOW,
    )
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="long",
                target_notional=5_000.0,
                rationale="hold unchanged",
            )
        ],
        stated_deploy_frac=0.25,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.25,
    )
    decision_marks = {symbol: 100.0}
    execution_marks = {symbol: 110.0}
    costs = {
        symbol: CostInputs(
            adv_usd=1e9,
            depth_bids=[(109.9, 1_000_000.0)],
            depth_asks=[(110.1, 1_000_000.0)],
        )
    }

    audit = _execution_target_audit(account, book, decision_marks, execution_marks)[symbol]
    report = reconcile_book(
        account,
        book,
        marks=execution_marks,
        decision_marks=decision_marks,
        costs=costs,
        betas={symbol: 1.0},
        now=NOW,
        cycle=2,
        cadence="rebal",
        enforce_achieved_safety=False,
    )

    assert audit["quantity_source"] == "decision_mark"
    assert audit["decision_target_qty_signed"] == pytest.approx(50.0)
    assert audit["delta_qty_signed"] == 0.0
    assert audit["planned_turnover_usd"] == 0.0
    assert account.positions[symbol].qty == pytest.approx(50.0)
    assert report.turnover_usd == 0.0
    assert report.fees_paid_cycle == 0.0
    assert report.slippage_paid_cycle == 0.0
    # The achieved exposure moves with price; the executor does not resize it behind the PM's back.
    assert account.positions[symbol].qty * execution_marks[symbol] == pytest.approx(5_500.0)
    assert report.achieved_beta_residual == pytest.approx(5_500.0 / report.equity)


def test_new_position_quantity_is_also_anchored_to_the_decision_mark():
    symbol = "SOL/USDT:USDT"
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=5_000.0, rationale="new")],
        stated_deploy_frac=0.25,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.25,
    )

    reconcile_book(
        account,
        book,
        marks={symbol: 110.0},
        decision_marks={symbol: 100.0},
        costs={
            symbol: CostInputs(
                adv_usd=1e9,
                depth_bids=[(109.9, 1_000_000.0)],
                depth_asks=[(110.1, 1_000_000.0)],
            )
        },
        betas={symbol: 1.0},
        now=NOW,
        cycle=1,
        cadence="rebal",
        enforce_achieved_safety=False,
    )

    assert account.positions[symbol].qty == pytest.approx(50.0)


def test_execution_quantizes_lot_size_and_partially_fills_conservative_depth():
    symbol = "SOL/USDT:USDT"

    class _LimitedBook:
        def symbol_spec(self, requested_symbol):
            assert requested_symbol == symbol
            return _execution_spec(step_size=1.0, min_notional=5.0)

        def depth(self, requested_symbol):
            assert requested_symbol == symbol
            return {"bids": [(99.0, 10.0)], "asks": [(101.0, 10.0)]}

    policy = ExecutionRealism(
        latency_ms=0.0,
        displayed_depth_fraction=0.5,
        adverse_selection_bps=2.0,
        legging_bps_per_second=0.0,
    )
    marks, costs, execution, execution_ts = _execution_inputs(
        _LimitedBook(),
        {symbol},
        {symbol: 100.0},
        execution_realism=policy,
    )
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=1_234.0, rationale="PM")],
        stated_deploy_frac=0.0617,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.0617,
    )
    target_audit = _execution_target_audit(
        account, book, {symbol: 100.0}, marks, execution
    )
    targets = _verify_execution_liquidity(target_audit, execution)

    assert target_audit[symbol]["decision_target_qty_signed"] == pytest.approx(12.34)
    assert target_audit[symbol]["quantized_order_delta_qty_signed"] == 12.0
    assert target_audit[symbol]["executed_delta_qty_signed"] == 5.0
    assert target_audit[symbol]["partial_fill"] is True
    assert target_audit[symbol]["fill_ratio"] == pytest.approx(5.0 / 12.0)

    report = reconcile_book(
        account,
        book,
        marks=marks,
        decision_marks={symbol: 100.0},
        costs=costs,
        betas={symbol: 1.0},
        now=NOW,
        execution_ts=execution_ts,
        execution_ts_by_symbol={symbol: execution_ts},
        target_signed_quantities=targets,
        cycle=1,
        cadence="rebal",
        enforce_achieved_safety=False,
    )
    assert account.positions[symbol].qty == 5.0
    assert report.turnover_usd == pytest.approx(500.0)
    # Five units walk the haircutted ask at 101 versus midpoint 100, plus 2bps reserve.
    assert report.slippage_paid_cycle == pytest.approx(5.0 + 0.1)
    assert report.achieved_deploy_frac == pytest.approx(500.0 / report.equity)


def test_execution_uses_one_basket_participation_ratio_to_preserve_neutrality():
    class _AsymmetricBook:
        def symbol_spec(self, _symbol):
            return _execution_spec(step_size=0.01, min_notional=5.0)

        def depth(self, symbol):
            qty = 100.0 if symbol == "LONG" else 5.0
            return {"bids": [(99.0, qty)], "asks": [(101.0, 100.0)]}

    policy = ExecutionRealism(
        latency_ms=0.0,
        displayed_depth_fraction=1.0,
        adverse_selection_bps=0.0,
        legging_bps_per_second=0.0,
    )
    decision_marks = {"LONG": 100.0, "SHORT": 100.0}
    marks, costs, execution, execution_ts = _execution_inputs(
        _AsymmetricBook(),
        set(decision_marks),
        decision_marks,
        execution_realism=policy,
    )
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[
            BookLeg(symbol="LONG", side="long", target_notional=9_000.0),
            BookLeg(symbol="SHORT", side="short", target_notional=9_000.0),
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )

    audit = _execution_target_audit(account, book, decision_marks, marks, execution)
    targets = _verify_execution_liquidity(audit, execution)

    assert audit["LONG"]["basket_fill_ratio"] == pytest.approx(5.0 / 90.0)
    assert audit["SHORT"]["basket_fill_ratio"] == pytest.approx(5.0 / 90.0)
    assert audit["LONG"]["executed_delta_qty_signed"] == pytest.approx(5.0)
    assert audit["SHORT"]["executed_delta_qty_signed"] == pytest.approx(-5.0)
    report = reconcile_book(
        account,
        book,
        marks=marks,
        decision_marks=decision_marks,
        costs=costs,
        betas={"LONG": 1.0, "SHORT": 1.0},
        now=NOW,
        execution_ts=execution_ts,
        target_signed_quantities=targets,
        execution_completed_by_symbol={symbol: False for symbol in targets},
        cycle=1,
        cadence="rebal",
        enforce_achieved_safety=False,
    )

    assert account.positions["LONG"].qty == pytest.approx(5.0)
    assert account.positions["SHORT"].qty == pytest.approx(5.0)
    assert report.achieved_dollar_residual_frac == pytest.approx(0.0)
    assert report.achieved_beta_residual == pytest.approx(0.0)


def test_achieved_safety_rejects_unbalanced_partial_targets_before_mutation():
    account = PaperAccount(cash=20_000.0)
    before = account.model_dump(mode="json")
    book = Book(
        legs=[
            BookLeg(symbol="LONG", side="long", target_notional=9_000.0),
            BookLeg(symbol="SHORT", side="short", target_notional=9_000.0),
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )
    marks = {"LONG": 100.0, "SHORT": 100.0}
    costs = {
        symbol: CostInputs(
            adv_usd=1e9,
            depth_bids=[(99.0, 1_000.0)],
            depth_asks=[(101.0, 1_000.0)],
        )
        for symbol in marks
    }

    with pytest.raises(RuntimeError, match="B2_dollar_residual"):
        reconcile_book(
            account,
            book,
            marks=marks,
            decision_marks=marks,
            costs=costs,
            betas={"LONG": 1.0, "SHORT": 1.0},
            now=NOW,
            target_signed_quantities={"LONG": 90.0, "SHORT": -5.0},
            execution_completed_by_symbol={"LONG": False, "SHORT": False},
            cycle=1,
            cadence="rebal",
        )

    assert account.model_dump(mode="json") == before


def test_achieved_safety_rejects_symmetric_price_drift_above_b1_upper():
    account = PaperAccount(cash=20_000.0)
    before = account.model_dump(mode="json")
    book = Book(
        legs=[
            BookLeg(symbol="LONG", side="long", target_notional=9_000.0),
            BookLeg(symbol="SHORT", side="short", target_notional=9_000.0),
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )
    decision_marks = {"LONG": 100.0, "SHORT": 100.0}
    execution_marks = {"LONG": 200.0, "SHORT": 200.0}
    costs = {
        symbol: CostInputs(
            adv_usd=1e9,
            depth_bids=[(199.0, 1_000.0)],
            depth_asks=[(201.0, 1_000.0)],
        )
        for symbol in execution_marks
    }

    with pytest.raises(RuntimeError, match="B1_upper"):
        reconcile_book(
            account,
            book,
            marks=execution_marks,
            decision_marks=decision_marks,
            costs=costs,
            betas={"LONG": 1.0, "SHORT": 1.0},
            now=NOW,
            cycle=1,
            cadence="rebal",
        )

    assert account.model_dump(mode="json") == before


def test_shallow_common_basket_cannot_create_unapproved_b1_underdeployment():
    symbols = ("L1", "L2", "S1", "S2")

    class _OnePercentBook:
        def symbol_spec(self, _symbol):
            return _execution_spec(step_size=0.01, min_notional=5.0)

        def depth(self, _symbol):
            return {"bids": [(99.0, 0.45)], "asks": [(101.0, 0.45)]}

    decision_marks = dict.fromkeys(symbols, 100.0)
    marks, costs, execution, execution_ts = _execution_inputs(
        _OnePercentBook(),
        set(symbols),
        decision_marks,
        execution_realism=ExecutionRealism(
            latency_ms=0.0,
            displayed_depth_fraction=1.0,
            adverse_selection_bps=0.0,
            legging_bps_per_second=0.0,
        ),
    )
    book = Book(
        legs=[
            BookLeg(symbol="L1", side="long", target_notional=4_500.0),
            BookLeg(symbol="L2", side="long", target_notional=4_500.0),
            BookLeg(symbol="S1", side="short", target_notional=4_500.0),
            BookLeg(symbol="S2", side="short", target_notional=4_500.0),
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )
    account = PaperAccount(cash=20_000.0)
    before = account.model_dump(mode="json")
    audit = _execution_target_audit(account, book, decision_marks, marks, execution)
    targets = _verify_execution_liquidity(audit, execution)
    assert all(
        row["basket_fill_ratio"] == pytest.approx(0.01) for row in audit.values()
    )

    with pytest.raises(RuntimeError, match="B1_lower_unapproved"):
        reconcile_book(
            account,
            book,
            marks=marks,
            decision_marks=decision_marks,
            costs=costs,
            betas=dict.fromkeys(symbols, 1.0),
            now=NOW,
            execution_ts=execution_ts,
            target_signed_quantities=targets,
            execution_completed_by_symbol=dict.fromkeys(symbols, False),
            cycle=1,
            cadence="rebal",
        )

    assert account.model_dump(mode="json") == before


@pytest.mark.parametrize(
    ("symbol", "current_role"),
    [("SOL/USDT:USDT", "alpha"), ("BTC/USDT:USDT", "hedge")],
)
def test_partial_reduction_or_role_transfer_fails_before_lifecycle_mutation(
    symbol, current_role
):
    class _ShallowBid:
        def symbol_spec(self, _symbol):
            return _execution_spec(step_size=1.0, min_notional=5.0)

        def depth(self, _symbol):
            return {"bids": [(99.0, 3.0)], "asks": [(101.0, 100.0)]}

    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbol: Position(
                symbol=symbol,
                direction="long",
                qty=10.0,
                entry_price=90.0,
                opened_ts=NOW,
                seat_role=current_role,
                thesis_cycle=1,
                edge_calibration_basis="legacy",
            )
        },
    )
    before = account.model_dump(mode="json")
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="long",
                target_notional=200.0,
                seat_role="alpha",
                edge_calibration_basis="new target only",
            )
        ],
        stated_deploy_frac=0.01,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.01,
    )
    marks, _costs, execution, _ = _execution_inputs(
        _ShallowBid(),
        {symbol},
        {symbol: 100.0},
        execution_realism=ExecutionRealism(
            latency_ms=0.0,
            displayed_depth_fraction=1.0,
        ),
    )
    audit = _execution_target_audit(
        account, book, {symbol: 100.0}, marks, execution
    )

    assert audit[symbol]["executed_target_qty_signed"] == pytest.approx(7.0)
    assert audit[symbol]["lifecycle_requires_full_fill"] is True
    with pytest.raises(RuntimeError, match="split the held lifecycle"):
        _verify_execution_liquidity(audit, execution)
    assert account.model_dump(mode="json") == before

    with pytest.raises(ValueError, match="lacks full-fill proof"):
        account.apply_fills(
            [
                {
                    "symbol": symbol,
                    "direction": "long",
                    "target_notional": 200.0,
                    "seat_role": "alpha",
                    "thesis_cycle": 2,
                    "edge_calibration_basis": "new target only",
                }
            ],
            {symbol: 100.0},
            {symbol: CostInputs()},
            target_signed_quantities={symbol: 7.0},
        )
    assert account.model_dump(mode="json") == before


def test_execution_min_notional_failure_records_zero_fill_without_inventing_size():
    symbol = "SOL/USDT:USDT"

    class _Book:
        def symbol_spec(self, requested_symbol):
            return _execution_spec(step_size=0.1, min_notional=50.0)

        def depth(self, requested_symbol):
            return {"bids": [(99.0, 1_000.0)], "asks": [(101.0, 1_000.0)]}

    marks, _costs, execution, _ = _execution_inputs(
        _Book(),
        {symbol},
        {symbol: 100.0},
        execution_realism=ExecutionRealism(latency_ms=0.0),
    )
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=30.0, rationale="PM")],
        stated_deploy_frac=0.0015,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.0015,
    )
    target_audit = _execution_target_audit(
        account, book, {symbol: 100.0}, marks, execution
    )
    assert target_audit[symbol]["min_notional_pass"] is False
    assert target_audit[symbol]["executed_delta_qty_signed"] == 0.0
    with pytest.raises(RuntimeError, match="below exchange minimum notional"):
        _verify_execution_liquidity(target_audit, execution)


def test_execution_above_market_max_qty_is_split_into_valid_clips():
    symbol = "SOL/USDT:USDT"

    class _Book:
        def symbol_spec(self, requested_symbol):
            return SimpleNamespace(
                step_size=0.1,
                tick_size=0.01,
                min_notional=5.0,
                max_qty=0.5,
            )

        def depth(self, requested_symbol):
            return {"bids": [(99.0, 1_000.0)], "asks": [(101.0, 1_000.0)]}

    marks, _costs, execution, _ = _execution_inputs(
        _Book(),
        {symbol},
        {symbol: 100.0},
        execution_realism=ExecutionRealism(latency_ms=0.0),
    )
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=100.0, rationale="PM")],
        stated_deploy_frac=0.005,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.005,
    )
    target_audit = _execution_target_audit(
        account, book, {symbol: 100.0}, marks, execution
    )

    assert target_audit[symbol]["max_qty_pass"] is True
    assert target_audit[symbol]["market_order_clips_qty_signed"] == [0.5, 0.5]
    targets = _verify_execution_liquidity(target_audit, execution)
    assert targets[symbol] == 1.0
    assert target_audit[symbol]["executed_market_order_clip_count"] == 2


def test_execution_inputs_bind_distinct_filters_to_each_symbol():
    symbols = {"AKE/USDT:USDT", "ZEC/USDT:USDT"}

    class _Book:
        def symbol_spec(self, requested_symbol):
            if requested_symbol.startswith("AKE"):
                return SimpleNamespace(
                    step_size=1.0,
                    tick_size=0.000001,
                    min_notional=5.0,
                    min_qty=1.0,
                    max_qty=40_000_000.0,
                )
            return SimpleNamespace(
                step_size=0.001,
                tick_size=0.01,
                min_notional=5.0,
                min_qty=0.001,
                max_qty=2_000.0,
            )

        def depth(self, requested_symbol):
            mark = 0.015 if requested_symbol.startswith("AKE") else 1_180.0
            return {
                "bids": [(mark * 0.999, 100_000_000.0)],
                "asks": [(mark * 1.001, 100_000_000.0)],
            }

    _marks, _costs, execution, _ts = _execution_inputs(
        _Book(),
        symbols,
        {"AKE/USDT:USDT": 0.015, "ZEC/USDT:USDT": 1_180.0},
        execution_realism=ExecutionRealism(latency_ms=0.0),
    )
    assert execution["AKE/USDT:USDT"]["min_order_qty"] == 1.0
    assert execution["AKE/USDT:USDT"]["max_order_qty"] == 40_000_000.0
    assert execution["ZEC/USDT:USDT"]["min_order_qty"] == 0.001
    assert execution["ZEC/USDT:USDT"]["max_order_qty"] == 2_000.0


def test_execution_fails_when_max_qty_is_below_minimum_order_notional():
    symbol = "SOL/USDT:USDT"

    class _Book:
        def symbol_spec(self, requested_symbol):
            return SimpleNamespace(
                step_size=0.01,
                tick_size=0.01,
                min_notional=5.0,
                max_qty=0.04,
            )

        def depth(self, requested_symbol):
            return {"bids": [(99.0, 1_000.0)], "asks": [(101.0, 1_000.0)]}

    marks, _costs, execution, _ = _execution_inputs(
        _Book(),
        {symbol},
        {symbol: 100.0},
        execution_realism=ExecutionRealism(latency_ms=0.0),
    )
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=100.0, rationale="PM")],
        stated_deploy_frac=0.005,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.005,
    )
    target_audit = _execution_target_audit(
        account, book, {symbol: 100.0}, marks, execution
    )
    assert target_audit[symbol]["max_qty_pass"] is False
    with pytest.raises(RuntimeError, match="cannot be split within market maxQty"):
        _verify_execution_liquidity(target_audit, execution)


def test_execution_below_market_min_qty_fails_even_when_notional_passes():
    symbol = "BTC/USDT:USDT"

    class _Book:
        def symbol_spec(self, requested_symbol):
            return SimpleNamespace(
                step_size=0.001,
                tick_size=0.01,
                min_notional=5.0,
                min_qty=1.0,
                max_qty=1_000.0,
            )

        def depth(self, requested_symbol):
            return {"bids": [(99.0, 1_000.0)], "asks": [(101.0, 1_000.0)]}

    marks, _costs, execution, _ = _execution_inputs(
        _Book(),
        {symbol},
        {symbol: 100.0},
        execution_realism=ExecutionRealism(latency_ms=0.0),
    )
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=50.0, rationale="PM")],
        stated_deploy_frac=0.0025,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.0025,
    )
    target_audit = _execution_target_audit(
        account, book, {symbol: 100.0}, marks, execution
    )

    assert target_audit[symbol]["min_notional_pass"] is True
    assert target_audit[symbol]["min_qty_pass"] is False
    with pytest.raises(RuntimeError, match="below exchange market minQty"):
        _verify_execution_liquidity(target_audit, execution)


@pytest.mark.parametrize(
    ("min_notional", "min_qty", "message"),
    [
        (5.0, 1.0, "below exchange minimum notional"),
        (0.5, 5.0, "below exchange market minQty"),
    ],
)
def test_common_basket_partial_fill_rechecks_post_scale_exchange_minima(
    min_notional, min_qty, message
):
    symbols = ("L1", "L2", "S1", "S2")

    class _ShallowBook:
        def symbol_spec(self, _symbol):
            return SimpleNamespace(
                step_size=1.0,
                tick_size=0.01,
                min_notional=min_notional,
                min_qty=min_qty,
                max_qty=1_000_000.0,
            )

        def depth(self, _symbol):
            return {"bids": [(0.99, 1.0)], "asks": [(1.01, 1.0)]}

    account = PaperAccount(
        cash=20_000.0,
        positions={
            symbol: Position(
                symbol=symbol,
                direction="long" if symbol.startswith("L") else "short",
                qty=4_500.0,
                entry_price=1.0,
                opened_ts=NOW,
            )
            for symbol in symbols
        },
        last_funding_ts=NOW,
    )
    before = account.model_dump(mode="json")
    book = Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="long" if symbol.startswith("L") else "short",
                target_notional=(4_510.0 if symbol in {"L1", "S1"} else 4_500.0),
            )
            for symbol in symbols
        ],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )
    marks, _costs, execution, _execution_ts = _execution_inputs(
        _ShallowBook(),
        set(symbols),
        dict.fromkeys(symbols, 1.0),
        execution_realism=ExecutionRealism(
            latency_ms=0.0,
            displayed_depth_fraction=1.0,
            adverse_selection_bps=0.0,
            legging_bps_per_second=0.0,
        ),
    )
    target_audit = _execution_target_audit(
        account,
        book,
        dict.fromkeys(symbols, 1.0),
        marks,
        execution,
    )
    assert target_audit["L1"]["basket_fill_ratio"] == pytest.approx(0.1)
    assert target_audit["L1"]["executed_delta_qty_signed"] == pytest.approx(1.0)

    with pytest.raises(RuntimeError, match=message):
        _verify_execution_liquidity(target_audit, execution)

    assert account.model_dump(mode="json") == before


def test_no_partial_mode_still_executes_toward_zero_lot_quantized_delta():
    symbol = "SOL/USDT:USDT"

    class _Book:
        def symbol_spec(self, requested_symbol):
            return _execution_spec(step_size=0.1, min_notional=5.0)

        def depth(self, requested_symbol):
            return {"bids": [(99.0, 100.0)], "asks": [(101.0, 100.0)]}

    marks, _costs, execution, _ = _execution_inputs(
        _Book(),
        {symbol},
        {symbol: 100.0},
        execution_realism=ExecutionRealism(latency_ms=0.0, allow_partial_fills=False),
    )
    account = PaperAccount(cash=20_000.0)
    book = Book(
        legs=[BookLeg(symbol=symbol, side="long", target_notional=123.0, rationale="PM")],
        stated_deploy_frac=0.00615,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.00615,
    )
    target_audit = _execution_target_audit(
        account, book, {symbol: 100.0}, marks, execution
    )
    targets = _verify_execution_liquidity(target_audit, execution)

    assert target_audit[symbol]["delta_qty_signed"] == pytest.approx(1.23)
    assert target_audit[symbol]["quantized_order_delta_qty_signed"] == pytest.approx(1.2)
    assert target_audit[symbol]["executed_delta_qty_signed"] == pytest.approx(1.2)
    assert targets[symbol] == pytest.approx(1.2)


def _marked_account(
    notionals: dict[str, float], *, hedge_symbol: str | None = None
) -> tuple[PaperAccount, dict[str, float]]:
    marks = {symbol: 100.0 for symbol in notionals}
    positions = {
        symbol: Position(
            symbol=symbol,
            direction="long" if notional > 0.0 else "short",
            qty=abs(notional) / marks[symbol],
            entry_price=marks[symbol],
            opened_ts=NOW,
            seat_role="hedge" if symbol == hedge_symbol else "alpha",
        )
        for symbol, notional in notionals.items()
    }
    return PaperAccount(cash=20_000.0, positions=positions, last_funding_ts=NOW), marks


def test_achieved_safety_enforces_concentration_hedge_and_leg_beta_bounds():
    concentrated, marks = _marked_account(
        {"A": 7_000.0, "B": 2_000.0, "C": -4_500.0, "D": -4_500.0}
    )
    metrics = _achieved_execution_safety(
        concentrated, marks, {symbol: 0.0 for symbol in marks}, minimum_deploy_frac=0.0
    )
    assert "B4_concentration" in metrics["achieved_safety_violations"]

    oversized_hedge, marks = _marked_account(
        {
            "BTC/USDT:USDT": 10_100.0,
            "A": -3_400.0,
            "B": -3_350.0,
            "C": -3_350.0,
        },
        hedge_symbol="BTC/USDT:USDT",
    )
    metrics = _achieved_execution_safety(
        oversized_hedge, marks, {symbol: 0.0 for symbol in marks}, minimum_deploy_frac=0.0
    )
    assert "B5_btc_hedge" in metrics["achieved_safety_violations"]

    high_beta, marks = _marked_account(
        {"A": 6_000.0, "B": 3_000.0, "C": -4_500.0, "D": -4_500.0}
    )
    metrics = _achieved_execution_safety(
        high_beta,
        marks,
        {"A": 2.1, "B": 0.0, "C": 1.4, "D": 1.4},
        minimum_deploy_frac=0.0,
    )
    assert metrics["achieved_beta_residual"] == pytest.approx(0.0)
    assert "B6_leg_beta" in metrics["achieved_safety_violations"]


def test_achieved_safety_rejects_missing_or_nonfinite_held_marks_and_betas():
    account, marks = _marked_account({"A": 4_500.0, "B": -4_500.0})
    with pytest.raises(RuntimeError, match="lack achieved execution marks"):
        _achieved_execution_safety(account, {}, {"A": 1.0, "B": 1.0})
    with pytest.raises(RuntimeError, match="invalid achieved execution marks"):
        _achieved_execution_safety(account, {**marks, "A": float("nan")}, {})
    with pytest.raises(RuntimeError, match="invalid achieved betas"):
        _achieved_execution_safety(account, marks, {"A": float("nan")})


def test_execution_records_per_leg_observation_time_latency_and_legging_reserve():
    class _TwoBooks:
        def symbol_spec(self, requested_symbol):
            return _execution_spec()

        def depth(self, requested_symbol):
            return {"bids": [(99.0, 100.0)], "asks": [(101.0, 100.0)]}

    t0 = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    ticks = iter([
        t0,
        t0 + timedelta(milliseconds=500),
        t0 + timedelta(milliseconds=510),
        t0 + timedelta(seconds=1),
        t0 + timedelta(seconds=1, milliseconds=20),
    ])
    sleeps: list[float] = []
    policy = ExecutionRealism(
        latency_ms=500.0,
        displayed_depth_fraction=0.5,
        adverse_selection_bps=1.0,
        legging_bps_per_second=0.25,
    )

    _marks, costs, execution, group_ts = _execution_inputs(
        _TwoBooks(),
        {"A", "B"},
        {"A": 100.0, "B": 100.0},
        execution_realism=policy,
        sleep_fn=sleeps.append,
        now_fn=lambda: next(ticks),
    )

    assert sleeps == [0.5]
    assert execution["A"]["execution_sequence"] == 1
    assert execution["B"]["execution_sequence"] == 2
    assert execution["A"]["observed_at"] == (
        t0 + timedelta(milliseconds=510)
    ).isoformat()
    assert execution["B"]["execution_ts"] == group_ts.isoformat()
    assert execution["A"]["legging_bps"] == 0.0
    assert execution["B"]["legging_delay_seconds"] == pytest.approx(0.51)
    assert execution["B"]["legging_bps"] == pytest.approx(0.1275)
    assert costs["B"].legging_bps == pytest.approx(0.1275)
