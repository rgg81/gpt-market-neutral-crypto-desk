import pytest

from futures_fund.desk_contracts import Book, BookLeg, CandidateReview, SpecialistRead
from futures_fund.scorecard import (
    BookScore,
    Recurrence,  # noqa: F401
    ScoreRecord,
    SpecialistScore,
    classify_objections,
    detect_recurrences,
    forward_returns,
    score_book,
    score_candidate_opportunities,
    score_specialist,
)


def _read(sym, lean, conv):
    return SpecialistRead(symbol=sym, lean=lean, conviction=conv, rationale="x", evidence=[])


def test_forward_returns_intersection_only():
    r = forward_returns({"A": 100.0, "B": 50.0, "Z": 10.0}, {"A": 110.0, "B": 45.0})
    assert set(r) == {"A", "B"}  # Z dropped (not in marks_now)
    assert r["A"] == pytest.approx(0.10)
    assert r["B"] == pytest.approx(-0.10)


def test_score_specialist_hit_rate_and_edge():
    reads = [_read("A", "long", 1.0), _read("B", "short", 0.5), _read("C", "flat", 0.9)]
    rets = {"A": 0.10, "B": 0.10, "C": 0.20}  # A long+up=hit, B short+up=miss, C flat=excluded
    s = score_specialist("technical", reads, rets)
    assert isinstance(s, SpecialistScore)
    assert s.role == "technical"
    assert s.n_scored == 2  # flat excluded
    assert s.n_available == 3
    assert s.abstention_rate == pytest.approx(1 / 3)
    assert s.hit_rate == 0.5  # 1 of 2
    # edge = mean(+1*0.10*1.0 , -1*0.10*0.5) = mean(0.10, -0.05) = 0.025
    assert abs(s.conv_weighted_edge - 0.025) < 1e-9


def test_score_specialist_hi_conv_subset():
    reads = [_read("A", "long", 0.9), _read("B", "long", 0.3)]
    rets = {"A": -0.05, "B": 0.05}  # hi-conv A misses; lo-conv B ignored for hi metric
    s = score_specialist("sentiment", reads, rets)
    assert s.hi_n == 1
    assert s.hi_conv_hit_rate == 0.0


def test_score_specialist_no_scorable_is_zero_not_crash():
    s = score_specialist("futures", [_read("A", "flat", 0.0)], {"A": 0.1})
    assert s.n_scored == 0 and s.hit_rate == 0.0 and s.hi_n == 0


def test_score_book_alpha_net_of_beta():
    book = Book(
        legs=[
            BookLeg(symbol="A", side="long", target_notional=1000.0),
            BookLeg(symbol="BTC/USDT:USDT", side="short", target_notional=1000.0),
        ]
    )
    rets = {"A": 0.02, "BTC/USDT:USDT": 0.01}
    betas = {"A": 1.5, "BTC/USDT:USDT": 1.0}
    bs = score_book(book, rets, betas, btc_ret=0.01)
    assert isinstance(bs, BookScore)
    # pnl = 1000*0.02 + (-1000)*0.01 = 20 - 10 = 10
    assert abs(bs.gross_pnl - 10.0) < 1e-9
    assert bs.gross_notional == 2000.0
    # beta_dollar = 1000*1.5 + (-1000)*1.0 = 500 ; alpha = 10 - 500*0.01 = 5
    assert abs(bs.beta_dollar - 500.0) < 1e-9
    assert abs(bs.alpha_net_beta - 5.0) < 1e-9
    assert abs(bs.alpha_frac - (5.0 / 2000.0)) < 1e-9
    assert bs.strategy_net_frac is None  # direct/legacy price-only scores cannot tune the PM


def test_score_book_strategy_net_includes_carry_and_entry_friction():
    book = Book(legs=[BookLeg(symbol="A", side="short", target_notional=2000.0)])
    bs = score_book(
        book,
        {"A": 0.01},
        {"A": 1.0},
        btc_ret=0.0,
        projected_funding_pnl=1.5,
        entry_friction=2.5,
    )
    # short loses $20 on price, earns $1.50 projected funding, and paid $2.50 to enter
    assert bs.alpha_net_beta == pytest.approx(-20.0)
    assert bs.strategy_net_edge == pytest.approx(-21.0)
    assert bs.strategy_net_frac == pytest.approx(-21.0 / 2000.0)
    assert bs.strategy_net_is_forecast is True
    assert bs.realized_edge_ex_funding == pytest.approx(-22.5)
    assert bs.realized_edge_ex_funding_frac == pytest.approx(-22.5 / 2000.0)


def test_score_book_separates_alpha_return_from_whole_book_net_and_actual_funding():
    book = Book(
        legs=[
            BookLeg(symbol="A", side="long", target_notional=1_000.0),
            BookLeg(
                symbol="BTC/USDT:USDT",
                side="short",
                target_notional=500.0,
                seat_role="hedge",
            ),
        ]
    )
    score = score_book(
        book,
        {"A": 0.03, "BTC/USDT:USDT": 0.02},
        {"A": 1.0, "BTC/USDT:USDT": 1.0},
        btc_ret=0.02,
        entry_friction=3.0,
        actual_realized_funding_pnl=2.0,
        actual_funding_attribution_status="exact",
    )

    assert score.alpha_gross_notional == 1_000.0
    assert score.hedge_gross_notional == 500.0
    assert score.alpha_return_frac_on_alpha_gross == pytest.approx(0.01)
    assert score.actual_strategy_net_edge == pytest.approx(9.0)
    assert score.actual_strategy_net_frac_on_whole_book_gross == pytest.approx(9.0 / 1_500.0)
    assert score.actual_strategy_net_frac_on_alpha_gross is None


def test_cash_exit_is_scored_against_the_no_change_alpha_book():
    prior = Book(legs=[BookLeg(symbol="A", side="long", target_notional=1_000.0)])
    score = score_book(
        Book(),
        {"A": -0.04},
        {"A": 0.0},
        btc_ret=0.0,
        entry_friction=1.0,
        previous_book=prior,
    )

    assert score.decision_kind == "exit_to_cash"
    assert score.no_change_counterfactual_edge_ex_funding == pytest.approx(-40.0)
    assert score.incremental_edge_vs_no_change == pytest.approx(39.0)
    assert score.incremental_edge_vs_no_change_frac == pytest.approx(0.039)


def test_candidate_opportunity_score_preserves_pm_gate_causality_and_horizon():
    candidate = CandidateReview(
        symbol="A",
        side="short",
        status="rejected",
        exclusion_reason="entry_gate",
        expected_price_edge_frac=0.01,
        edge_horizon_hours=72,
        counterfactual_notional=2_000.0,
        rationale="PM-declared gate",
    )

    row = score_candidate_opportunities(
        [candidate],
        {"A": -0.05},
        {"A": 0.5},
        0.02,
        evaluation_horizon_hours=72.0,
        book_sha256="a" * 64,
        specialist_reads_sha256="b" * 64,
        entry_gate_policy_sha256="c" * 64,
    )[0]

    assert row.gate_causal_claim is True
    assert row.horizon_label_eligible is True
    assert row.realized_beta_adjusted_return_frac == pytest.approx(-0.06)
    assert row.realized_selected_edge_frac == pytest.approx(0.06)
    assert row.counterfactual_price_pnl == pytest.approx(120.0)
    assert row.forecast_error_frac == pytest.approx(0.05)
    assert len(row.candidate_sha256) == 64


def test_classify_objections_tags():
    tags = classify_objections(
        [
            "short side is over-concentrated in 2 names, single-name risk dominates",
            "the cited partnership is unverifiable",
        ]
    )
    assert "concentration" in tags and "hallucination" in tags


def test_classify_objections_empty():
    assert classify_objections([]) == []


def _rec(
    cycle,
    *,
    edge=0.1,
    alpha_frac=0.01,
    strategy_net_frac=0.01,
    accepted=True,
    tags=None,
    revised=False,
):
    return ScoreRecord(
        cycle=cycle,
        scored_at="t",
        n_symbols=12,
        specialist_return_label="btc_beta_adjusted",
        specialists={
            "sentiment": SpecialistScore(
                role="sentiment", n_scored=10, conv_weighted_edge=edge, hit_rate=0.5
            )
        },
        book=BookScore(
            n_legs=8,
            gross_notional=19000.0,
            alpha_frac=alpha_frac,
            strategy_net_frac=strategy_net_frac,
        ),
        adv_accepted=accepted,
        adv_revised=revised,
        adv_reason_tags=tags or [],
    )


def test_detect_fires_on_k_miscalibration():
    recs = [_rec(1, edge=-0.02), _rec(2, edge=-0.01), _rec(3, edge=-0.03)]
    out = detect_recurrences(recs, k=3, window=6)
    kinds = {(r.kind, r.role) for r in out}
    assert ("specialist_miscalibrated", "sentiment") in kinds
    assert any(r.count == 3 for r in out)


def test_detect_silent_below_k():
    recs = [_rec(1, edge=-0.02), _rec(2, edge=0.05), _rec(3, edge=-0.03)]
    out = detect_recurrences(recs, k=3, window=6)
    assert not any(r.kind == "specialist_miscalibrated" for r in out)


def test_detect_respects_window():
    # 3 bad but only in the OLD tail; window=3 sees only the last 3 (all good)
    recs = [
        _rec(1, edge=-0.1),
        _rec(2, edge=-0.1),
        _rec(3, edge=-0.1),
        _rec(4, edge=0.1),
        _rec(5, edge=0.1),
        _rec(6, edge=0.1),
    ]
    out = detect_recurrences(recs, k=3, window=3)
    assert not any(r.kind == "specialist_miscalibrated" for r in out)
    assert any(r.kind == "specialist_recovered" for r in out)


def test_detect_pm_rejected_same_reason():
    recs = [_rec(c, accepted=False, tags=["concentration"]) for c in (1, 2, 3)]
    out = detect_recurrences(recs, k=3, window=6)
    assert any(r.kind == "pm_rejected_same_reason" and r.role == "pm" for r in out)


def test_detect_specialist_overconviction_fires():
    def _oc(cycle):
        return ScoreRecord(
            cycle=cycle,
            scored_at="t",
            n_symbols=12,
            specialist_return_label="btc_beta_adjusted",
            specialists={
                "technical": SpecialistScore(
                    role="technical", n_scored=8, hi_n=4, hi_conv_hit_rate=0.25
                )
            },
            book=BookScore(n_legs=8, gross_notional=19000.0, alpha_frac=0.01),
            adv_accepted=True,
        )

    out = detect_recurrences([_oc(1), _oc(2), _oc(3)], k=3, window=6)
    assert any(r.kind == "specialist_overconviction" and r.role == "technical" for r in out)


def test_detect_pm_negative_net_edge_and_adversary_lax():
    recs = [_rec(c, alpha_frac=-0.02, strategy_net_frac=-0.02, accepted=True) for c in (1, 2, 3)]
    out = detect_recurrences(recs, k=3, window=6)
    kinds = {r.kind for r in out}
    assert "pm_negative_net_edge" in kinds
    assert "adversary_too_lax" in kinds


def test_projected_funding_cannot_turn_a_realized_loss_into_a_learning_win():
    records = [
        ScoreRecord(
            cycle=cycle,
            book=BookScore(
                n_legs=4,
                gross_notional=20_000.0,
                alpha_frac=-0.01,
                projected_funding_pnl=400.0,
                strategy_net_frac=0.01,
                strategy_net_is_forecast=True,
            ),
            adv_accepted=True,
        )
        for cycle in (1, 2, 3)
    ]
    kinds = {item.kind for item in detect_recurrences(records, k=3, window=6)}
    assert "pm_negative_net_edge" in kinds
    assert "adversary_too_lax" in kinds


def test_manifest_bound_score_requires_a_complete_observation_identity():
    with pytest.raises(ValueError, match="later observation cycle"):
        ScoreRecord(cycle=7, outcome_provenance="manifest_bound")


def test_detect_pm_ignores_legacy_and_immaterial_price_noise():
    legacy = [
        ScoreRecord(
            cycle=c,
            book=BookScore(n_legs=4, gross_notional=20_000.0, alpha_frac=-0.02),
        )
        for c in (1, 2, 3)
    ]
    small = [_rec(c, strategy_net_frac=-0.001) for c in (4, 5, 6)]
    out = detect_recurrences([*legacy, *small], k=3, window=6)
    assert not any(r.kind == "pm_negative_net_edge" for r in out)


def test_detect_specialist_inactivity_without_forcing_a_call():
    records = []
    for cycle in range(1, 7):
        records.append(
            ScoreRecord(
                cycle=cycle,
                n_symbols=10,
                specialist_return_label="btc_beta_adjusted",
                specialists={
                    "futures": SpecialistScore(
                        role="futures", n_available=10, n_scored=0, abstention_rate=1.0
                    )
                },
            )
        )
    out = detect_recurrences(records, k=3, window=6)
    item = next(r for r in out if r.kind == "specialist_inactive")
    assert item.role == "futures" and item.count == 6
    assert "never evidence" in item.suggestion


def test_failed_beta_adjusted_specialist_has_no_inactivity_coverage():
    records = [
        ScoreRecord(
            cycle=cycle,
            n_symbols=10,
            specialist_return_label="btc_beta_adjusted",
            specialists={
                "futures": SpecialistScore(
                    role="futures",
                    n_available=0,
                    n_scored=0,
                    abstention_rate=0.0,
                )
            },
        )
        for cycle in range(1, 7)
    ]
    out = detect_recurrences(records, k=3, window=6)
    assert not any(item.kind == "specialist_inactive" for item in out)


def test_detect_two_sided_recovery_for_specialist_and_pm():
    prior = [
        ScoreRecord(
            cycle=cycle,
            n_symbols=10,
            specialist_return_label="btc_beta_adjusted",
            specialists={
                "technical": SpecialistScore(
                    role="technical",
                    n_available=10,
                    n_scored=2,
                    hit_rate=0.0,
                    conv_weighted_edge=-0.01,
                )
            },
            book=BookScore(n_legs=4, gross_notional=20_000.0, strategy_net_frac=-0.002),
        )
        for cycle in (1, 2, 3)
    ]
    recovered = [
        ScoreRecord(
            cycle=cycle,
            n_symbols=10,
            specialist_return_label="btc_beta_adjusted",
            specialists={
                "technical": SpecialistScore(
                    role="technical",
                    n_available=10,
                    n_scored=2,
                    hit_rate=0.5,
                    conv_weighted_edge=0.01,
                )
            },
            book=BookScore(n_legs=4, gross_notional=20_000.0, strategy_net_frac=0.002),
        )
        for cycle in (4, 5, 6)
    ]
    kinds = {(r.kind, r.role) for r in detect_recurrences([*prior, *recovered], k=3, window=6)}
    assert ("specialist_recovered", "technical") in kinds
    assert ("pm_recovered", "pm") in kinds
