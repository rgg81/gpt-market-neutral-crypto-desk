from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from futures_fund.adversary_binding import (
    AdversaryBindingError,
    verify_precheck_artifact,
    verify_verdict_binding,
)
from futures_fund.desk_contracts import (
    AdversaryVerdict,
    Book,
    BookLeg,
    BoundVerdict,
    CitationCheck,
    MetricsEcho,
    SpecialistRead,
)
from futures_fund.precheck import PrecheckMetrics, compute_precheck
from scripts.desk_reconcile import _parse_specialist_reads, _verify_decision_chain, main

SYMBOLS = ("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT", "D/USDT:USDT")
EVIDENCE = [
    {
        "symbol": symbol,
        "mark": 1.0,
        "beta_btc": 1.0,
        "est_slippage_bps_2k": 1.0,
        "expected_funding_8h_bps": 0.0,
    }
    for symbol in SYMBOLS
]
CURRENT_BOOK = [
    {"symbol": symbol, "side": "long" if i < 2 else "short", "target_notional": 4500.0}
    for i, symbol in enumerate(SYMBOLS)
]
META = {"cash": 20_000.0, "btc_symbol": "BTC/USDT:USDT"}


def _book(*, first_notional: float = 4500.0) -> Book:
    return Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="long" if i < 2 else "short",
                target_notional=first_notional if i == 0 else 4500.0,
            )
            for i, symbol in enumerate(SYMBOLS)
        ],
        stated_deploy_frac=(first_notional + 13_500.0) / 20_000.0,
        stated_dollar_residual_frac=abs(first_notional - 4500.0) / (
            first_notional + 13_500.0
        ),
        stated_beta_residual=(first_notional - 4500.0) / 20_000.0,
        turnover_legs_changed=0,
    )


def _precheck(book: Book, cycle: int = 7) -> PrecheckMetrics:
    return compute_precheck(
        book, EVIDENCE, cash=20_000.0, cycle=cycle, current_book=CURRENT_BOOK
    )


def _verdict(
    precheck: PrecheckMetrics,
    *,
    accept: bool = True,
    override_rationale: str = "",
) -> AdversaryVerdict:
    return AdversaryVerdict(
        accept=accept,
        cycle=precheck.cycle,
        precheck_sha256=precheck.sha256,
        metrics_echo=MetricsEcho(
            gross=precheck.gross,
            deploy_frac=precheck.deploy_frac,
            dollar_residual_frac=precheck.dollar_residual_frac,
            beta_residual=precheck.beta_residual,
            max_leg_frac_gross=precheck.max_leg_frac_gross,
            turnover_legs_changed=precheck.turnover_legs_changed,
        ),
        bounds_confirmed=[
            BoundVerdict(bound_id=bound.bound_id, ok=bound.ok) for bound in precheck.bounds
        ],
        override_rationale=override_rationale,
        objections=[] if accept else ["the original thesis is not supported"],
        demanded_changes=[] if accept else ["replace the unsupported original leg"],
    )


def _write(path: Path, model) -> None:
    path.write_text(model.model_dump_json(indent=2))


def test_valid_verdict_is_bound_to_exact_precheck():
    precheck = _precheck(_book())
    verify_precheck_artifact(precheck, precheck, cycle=7)
    verify_verdict_binding(_verdict(precheck), precheck, cycle=7)


def test_specialist_reads_require_complete_duplicate_free_coverage():
    raw = [
        {
            "symbol": symbol,
            "lean": "flat",
            "conviction": 0.0,
            "rationale": "no signal",
            "evidence": [],
        }
        for symbol in SYMBOLS
    ]
    assert len(_parse_specialist_reads(raw, list(SYMBOLS))) == len(SYMBOLS)

    with pytest.raises(ValueError, match="missing"):
        _parse_specialist_reads(raw[:-1], list(SYMBOLS))
    with pytest.raises(ValueError, match="duplicates=1"):
        _parse_specialist_reads([*raw[:-1], raw[0]], list(SYMBOLS))


def test_verdict_rejects_wrong_hash_and_metric_echo():
    precheck = _precheck(_book())
    wrong_hash = _verdict(precheck).model_copy(update={"precheck_sha256": "f" * 64})
    with pytest.raises(AdversaryBindingError, match="precheck_sha256"):
        verify_verdict_binding(wrong_hash, precheck, cycle=7)

    verdict = _verdict(precheck)
    wrong_echo = verdict.metrics_echo.model_copy(update={"gross": precheck.gross + 1000.0})
    verdict = verdict.model_copy(update={"metrics_echo": wrong_echo})
    with pytest.raises(AdversaryBindingError, match="metrics_echo.gross"):
        verify_verdict_binding(verdict, precheck, cycle=7)


def _sentiment_read(symbol: str = "A/USDT:USDT") -> SpecialistRead:
    return SpecialistRead(
        symbol=symbol,
        lean="long",
        conviction=0.3,
        rationale="fresh permanent announcement",
        evidence=[
            "2026-07-30 | Project | permanent announcement | "
            f"https://example.com/{symbol.split('/')[0].lower()}-announcement"
        ],
    )


def test_citation_audit_covers_every_nonflat_read_and_exact_url():
    precheck = _precheck(_book())
    read = _sentiment_read()
    url = "https://example.com/a-announcement"
    verdict = _verdict(precheck).model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=True,
                    material_to_book=True,
                    checked_urls=[url],
                    note="Opened the permanent announcement; the dated claim is present.",
                )
            ]
        }
    )
    verify_verdict_binding(
        verdict, precheck, cycle=7, sentiment_reads=[read], book=_book()
    )

    with pytest.raises(AdversaryBindingError, match="cover every non-flat"):
        verify_verdict_binding(
            _verdict(precheck), precheck, cycle=7, sentiment_reads=[read], book=_book()
        )

    wrong_url = verdict.model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=True,
                    material_to_book=True,
                    checked_urls=["https://example.com/different-page"],
                    note="Opened a page.",
                )
            ]
        }
    )
    with pytest.raises(AdversaryBindingError, match="do not match"):
        verify_verdict_binding(
            wrong_url, precheck, cycle=7, sentiment_reads=[read], book=_book()
        )


def test_accepted_selected_leg_cannot_use_unsupported_sentiment():
    precheck = _precheck(_book())
    read = _sentiment_read()
    verdict = _verdict(precheck).model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=False,
                    material_to_book=True,
                    checked_urls=["https://example.com/a-announcement"],
                    note="The URL does not contain the claimed activation.",
                )
            ]
        }
    )
    with pytest.raises(AdversaryBindingError, match="uses unsupported"):
        verify_verdict_binding(
            verdict, precheck, cycle=7, sentiment_reads=[read], book=_book()
        )


def test_unsupported_unselected_sentiment_is_recorded_without_vetoing_book():
    precheck = _precheck(_book())
    read = _sentiment_read("UNI/USDT:USDT")
    verdict = _verdict(precheck).model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=False,
                    material_to_book=False,
                    checked_urls=["https://example.com/uni-announcement"],
                    note="The URL says proposed, while the read says activated.",
                )
            ]
        }
    )
    verify_verdict_binding(
        verdict, precheck, cycle=7, sentiment_reads=[read], book=_book()
    )


def test_precheck_must_match_current_book_and_its_own_hash():
    final = _precheck(_book())
    original = _precheck(_book(first_notional=4400.0))
    with pytest.raises(AdversaryBindingError, match="current book and evidence"):
        verify_precheck_artifact(original, final, cycle=7)

    tampered = final.model_copy(update={"gross": final.gross + 1.0})
    with pytest.raises(AdversaryBindingError, match="contents"):
        verify_precheck_artifact(tampered, final, cycle=7)


def test_accepting_a_precheck_failure_needs_explicit_override():
    failed = compute_precheck(Book(), [], cash=20_000.0, cycle=7)
    rulings = [
        BoundVerdict(
            bound_id=bound.bound_id,
            ok=True,
            note="Adversary override" if not bound.ok else "",
        )
        for bound in failed.bounds
    ]
    verdict = AdversaryVerdict(
        accept=True,
        cycle=7,
        precheck_sha256=failed.sha256,
        metrics_echo=MetricsEcho(
            gross=failed.gross,
            deploy_frac=failed.deploy_frac,
            dollar_residual_frac=failed.dollar_residual_frac,
            beta_residual=failed.beta_residual,
            max_leg_frac_gross=failed.max_leg_frac_gross,
            turnover_legs_changed=failed.turnover_legs_changed,
        ),
        bounds_confirmed=rulings,
    )
    with pytest.raises(AdversaryBindingError, match="override_rationale"):
        verify_verdict_binding(verdict, failed, cycle=7)


def test_accepted_decision_chain_binds_final_artifacts(tmp_path):
    book = _book()
    precheck = _precheck(book)
    _write(tmp_path / "precheck.json", precheck)

    _verify_decision_chain(
        tmp_path,
        cycle=7,
        meta=META,
        evidence=EVIDENCE,
        current_book=CURRENT_BOOK,
        book=book,
        verdict=_verdict(precheck),
    )


def test_rejected_decision_chain_binds_original_and_requires_revision_trail(tmp_path):
    original_book = _book(first_notional=4400.0)
    original_precheck = _precheck(original_book)
    final_book = _book()
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False)
    _write(tmp_path / "precheck.json", final_precheck)

    with pytest.raises(AdversaryBindingError, match="lacks pm_book_original"):
        _verify_decision_chain(
            tmp_path,
            cycle=7,
            meta=META,
            evidence=EVIDENCE,
            current_book=CURRENT_BOOK,
            book=final_book,
            verdict=verdict,
        )

    _write(tmp_path / "pm_book_original.json", original_book)
    _write(tmp_path / "precheck_original.json", original_precheck)
    _verify_decision_chain(
        tmp_path,
        cycle=7,
        meta=META,
        evidence=EVIDENCE,
        current_book=CURRENT_BOOK,
        book=final_book,
        verdict=verdict,
    )


def test_reconcile_halts_before_state_mutation_on_unbound_verdict(tmp_path, capsys):
    memory = tmp_path / "memory"
    pending = memory / "pending" / "7"
    pending.mkdir(parents=True)
    now = datetime.now(UTC)
    (memory / "pending" / "current.json").write_text(json.dumps(
        {"cycle": 7, "dir": str(pending), "created": now.isoformat()}
    ))
    (pending / "meta.json").write_text(json.dumps(
        {"cycle": 7, "now": now.isoformat(), **META}
    ))
    (pending / "evidence.json").write_text(json.dumps(EVIDENCE))

    raw_reads = [
        {
            "symbol": symbol,
            "lean": "flat",
            "conviction": 0.0,
            "rationale": "no signal",
            "evidence": [],
        }
        for symbol in SYMBOLS
    ]
    for role in ("sentiment", "technical", "futures"):
        (pending / f"{role}_reads.json").write_text(json.dumps(raw_reads))

    book = _book()
    precheck = _precheck(book)
    _write(pending / "pm_book.json", book)
    _write(pending / "precheck.json", precheck)
    wrong_verdict = _verdict(precheck).model_copy(update={"precheck_sha256": "f" * 64})
    _write(pending / "adversary.json", wrong_verdict)

    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "--memory-dir", str(memory)]) == 1
    assert not state.exists()
    assert "decision-chain validation failed" in capsys.readouterr().out


def test_reconcile_main_uses_fresh_book_mid_and_persists_execution_audit(
    tmp_path, monkeypatch
):
    """Production reconciliation keeps decision provenance but fills from one fresh book."""
    symbol = "SOL/USDT:USDT"
    decision_ts = datetime.now(UTC) - timedelta(minutes=15)
    evidence = [{
        "symbol": symbol,
        "mark": 100.0,
        "beta_btc": 1.0,
        "est_slippage_bps_2k": 1.0,
        "expected_funding_8h_bps": 0.0,
        "funding_rate": 0.0,
        "funding_interval_h": 8.0,
    }]

    memory = tmp_path / "memory"
    pending = memory / "pending" / "7"
    pending.mkdir(parents=True)
    (memory / "pending" / "current.json").write_text(json.dumps(
        {"cycle": 7, "dir": str(pending), "created": decision_ts.isoformat()}
    ))
    (pending / "meta.json").write_text(json.dumps({
        "cycle": 7,
        "now": decision_ts.isoformat(),
        "cash": 20_000.0,
        "btc_symbol": "BTC/USDT:USDT",
    }))
    (pending / "evidence.json").write_text(json.dumps(evidence))

    raw_reads = [{
        "symbol": symbol,
        "lean": "flat",
        "conviction": 0.0,
        "rationale": "no signal",
        "evidence": [],
    }]
    for role in ("sentiment", "technical", "futures"):
        (pending / f"{role}_reads.json").write_text(json.dumps(raw_reads))

    book = Book(
        legs=[BookLeg(
            symbol=symbol,
            side="long",
            target_notional=5_000.0,
            is_new=True,
            hold_breaking_reason="test fixture",
        )],
        stated_deploy_frac=0.25,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=0.25,
        turnover_legs_changed=1,
    )
    precheck = compute_precheck(
        book, evidence, cash=20_000.0, cycle=7, current_book=[]
    )
    _write(pending / "pm_book.json", book)
    _write(pending / "precheck.json", precheck)
    _write(
        pending / "adversary.json",
        _verdict(
            precheck,
            override_rationale="Test fixture explicitly accepts its non-production bounds.",
        ),
    )

    class _FreshExecutionExchange:
        def depth(self, requested_symbol):
            assert requested_symbol == symbol
            return {
                "bids": [(109.90, 1_000_000.0)],
                "asks": [(110.10, 1_000_000.0)],
            }

        def mark_price(self, requested_symbol):
            raise AssertionError("complete depth must provide its own midpoint")

    monkeypatch.setattr(
        "scripts.desk_reconcile.FuturesExchange.from_settings",
        lambda settings: _FreshExecutionExchange(),
    )

    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "--memory-dir", str(memory)]) == 0

    account = json.loads((state / "account.json").read_text())
    position = account["positions"][symbol]
    assert position["entry_price"] == pytest.approx(110.0)
    assert position["accrued_slippage"] < 5.0

    cycle_dir = state / "rebal" / "cycle" / "7"
    execution = json.loads((cycle_dir / "execution.json").read_text())[symbol]
    assert execution["decision_mark"] == pytest.approx(100.0)
    assert execution["execution_mark"] == pytest.approx(110.0)
    assert execution["decision_to_execution_bps"] == pytest.approx(1000.0)
    assert execution["price_source"] == "book_mid"

    report = json.loads((cycle_dir / "report.json").read_text())
    assert report["decision_age_seconds"] >= 15 * 60
    assert datetime.fromisoformat(report["execution_ts"]) > decision_ts
