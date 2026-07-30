import pytest

from futures_fund.desk_contracts import Book, BookLeg, SpecialistRead
from futures_fund.scorecard import (
    BookScore,
    Recurrence,  # noqa: F401
    ScoreRecord,
    SpecialistScore,
    classify_objections,
    detect_recurrences,
    forward_returns,
    score_book,
    score_specialist,
)


def _read(sym, lean, conv):
    return SpecialistRead(symbol=sym, lean=lean, conviction=conv, rationale="x", evidence=[])


def test_forward_returns_intersection_only():
    r = forward_returns({"A": 100.0, "B": 50.0, "Z": 10.0}, {"A": 110.0, "B": 45.0})
    assert set(r) == {"A", "B"}                 # Z dropped (not in marks_now)
    assert r["A"] == pytest.approx(0.10)
    assert r["B"] == pytest.approx(-0.10)


def test_score_specialist_hit_rate_and_edge():
    reads = [_read("A", "long", 1.0), _read("B", "short", 0.5), _read("C", "flat", 0.9)]
    rets = {"A": 0.10, "B": 0.10, "C": 0.20}  # A long+up=hit, B short+up=miss, C flat=excluded
    s = score_specialist("technical", reads, rets)
    assert isinstance(s, SpecialistScore)
    assert s.role == "technical"
    assert s.n_scored == 2                       # flat excluded
    assert s.hit_rate == 0.5                      # 1 of 2
    # edge = mean(+1*0.10*1.0 , -1*0.10*0.5) = mean(0.10, -0.05) = 0.025
    assert abs(s.conv_weighted_edge - 0.025) < 1e-9


def test_score_specialist_hi_conv_subset():
    reads = [_read("A", "long", 0.9), _read("B", "long", 0.3)]
    rets = {"A": -0.05, "B": 0.05}               # hi-conv A misses; lo-conv B ignored for hi metric
    s = score_specialist("sentiment", reads, rets)
    assert s.hi_n == 1
    assert s.hi_conv_hit_rate == 0.0


def test_score_specialist_no_scorable_is_zero_not_crash():
    s = score_specialist("futures", [_read("A", "flat", 0.0)], {"A": 0.1})
    assert s.n_scored == 0 and s.hit_rate == 0.0 and s.hi_n == 0


def test_score_book_alpha_net_of_beta():
    book = Book(legs=[
        BookLeg(symbol="A", side="long", target_notional=1000.0),
        BookLeg(symbol="BTC/USDT:USDT", side="short", target_notional=1000.0),
    ])
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


def test_classify_objections_tags():
    tags = classify_objections([
        "short side is over-concentrated in 2 names, single-name risk dominates",
        "the cited partnership is unverifiable",
    ])
    assert "concentration" in tags and "hallucination" in tags


def test_classify_objections_empty():
    assert classify_objections([]) == []


def _rec(cycle, *, edge=0.1, alpha_frac=0.01, accepted=True, tags=None, revised=False):
    return ScoreRecord(
        cycle=cycle, scored_at="t", n_symbols=12,
        specialists={"sentiment": SpecialistScore(role="sentiment", n_scored=10,
                                                  conv_weighted_edge=edge, hit_rate=0.5)},
        book=BookScore(n_legs=8, gross_notional=19000.0, alpha_frac=alpha_frac),
        adv_accepted=accepted, adv_revised=revised, adv_reason_tags=tags or [],
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
    recs = [_rec(1, edge=-0.1), _rec(2, edge=-0.1), _rec(3, edge=-0.1),
            _rec(4, edge=0.1), _rec(5, edge=0.1), _rec(6, edge=0.1)]
    assert detect_recurrences(recs, k=3, window=3) == []


def test_detect_pm_rejected_same_reason():
    recs = [_rec(c, accepted=False, tags=["concentration"]) for c in (1, 2, 3)]
    out = detect_recurrences(recs, k=3, window=6)
    assert any(r.kind == "pm_rejected_same_reason" and r.role == "pm" for r in out)


def test_detect_specialist_overconviction_fires():
    def _oc(cycle):
        return ScoreRecord(
            cycle=cycle, scored_at="t", n_symbols=12,
            specialists={"technical": SpecialistScore(role="technical", n_scored=8,
                                                      hi_n=4, hi_conv_hit_rate=0.25)},
            book=BookScore(n_legs=8, gross_notional=19000.0, alpha_frac=0.01),
            adv_accepted=True)
    out = detect_recurrences([_oc(1), _oc(2), _oc(3)], k=3, window=6)
    assert any(r.kind == "specialist_overconviction" and r.role == "technical" for r in out)


def test_detect_pm_negative_alpha_and_adversary_lax():
    recs = [_rec(c, alpha_frac=-0.02, accepted=True) for c in (1, 2, 3)]
    out = detect_recurrences(recs, k=3, window=6)
    kinds = {r.kind for r in out}
    assert "pm_negative_alpha" in kinds
    assert "adversary_too_lax" in kinds  # accepted + alpha_frac < LAX_ALPHA_FRAC
