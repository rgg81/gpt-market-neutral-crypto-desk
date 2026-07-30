from datetime import UTC, datetime

import pytest

from futures_fund.account import CostInputs, PaperAccount, Position
from futures_fund.agent_runner import StubAgentRunner
from futures_fund.desk_contracts import Book, BookLeg, SpecialistRead
from futures_fund.desk_cycle import (
    _execution_inputs,
    _execution_target_audit,
    reconcile_book,
    run_adversary,
    run_pm,
    run_specialists,
)
from futures_fund.evidence import EvidencePack
from tests.conftest import make_verdict

NOW = datetime(2026, 7, 7, tzinfo=UTC)
EV = [EvidencePack(symbol="SOL/USDT:USDT", mark=100.0, as_of_ts=NOW)]


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
                         now=NOW, cycle=1, cadence="rebal")
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

    # Fresh half-spread is ~9.09bps: ~$4.55 on $5k, not ~$505 from comparing 110.10 to 100.
    assert account.slippage_paid == pytest.approx(5_000.0 * (110.10 - 110.0) / 110.0)
    assert account.slippage_paid < 5.0
    assert account.positions["SOL/USDT:USDT"].entry_price == pytest.approx(110.0)


def test_execution_inputs_use_a_fresh_mark_and_fallback_model_without_two_sided_depth():
    class _NoBook:
        def depth(self, symbol):
            return {"bids": [], "asks": []}

        def mark_price(self, symbol):
            return 120.0

    marks, costs, audit, _ = _execution_inputs(
        _NoBook(),
        {"SOL/USDT:USDT"},
        {"SOL/USDT:USDT": 100.0},
    )

    assert marks["SOL/USDT:USDT"] == pytest.approx(120.0)
    assert costs["SOL/USDT:USDT"].depth_bids == []
    assert costs["SOL/USDT:USDT"].depth_asks == []
    assert audit["SOL/USDT:USDT"]["price_source"] == "mark_price"


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
    )

    assert account.positions[symbol].qty == pytest.approx(50.0)
