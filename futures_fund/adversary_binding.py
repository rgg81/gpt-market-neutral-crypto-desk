"""Integrity checks binding an Adversary verdict to the precheck it actually reviewed.

These checks validate workflow provenance only. They never decide whether a proposed book is good
or bad: numeric bounds remain data, the Adversary may accept a failing bound with a written
override, and a rejection still receives the runbook's single PM revision.
"""
from __future__ import annotations

import hmac
import math
import re

from futures_fund.desk_contracts import AdversaryVerdict, Book, SpecialistRead
from futures_fund.precheck import PrecheckMetrics, precheck_sha256

ECHO_REL_TOL = 0.01
ECHO_ABS_TOL = 1e-6

_ECHO_FIELDS = (
    "gross",
    "deploy_frac",
    "dollar_residual_frac",
    "beta_residual",
    "max_leg_frac_gross",
    "turnover_legs_changed",
)


class AdversaryBindingError(ValueError):
    """The decision artifacts are stale, tampered, or not mutually bound."""


def _evidence_urls(read: SpecialistRead) -> set[str]:
    """Extract the exact HTTP(S) URLs the sentiment read says it opened."""
    urls: set[str] = set()
    for item in read.evidence:
        for raw in re.findall(r"https?://[^\s]+", item):
            urls.add(raw.rstrip(".,);]"))
    return urls


def verify_citation_audit(
    verdict: AdversaryVerdict,
    sentiment_reads: list[SpecialistRead],
    book: Book,
) -> None:
    """Bind the Adversary's citation audit to every non-flat sentiment call and the reviewed book.

    This is provenance validation, not a deterministic truth oracle: GPT still decides whether a
    source supports the claim. Code proves that it opened every cited URL, labeled materiality
    honestly, and cannot accept a selected leg whose sentiment support it marked false.
    """
    expected = {
        read.symbol: _evidence_urls(read)
        for read in sentiment_reads
        if read.lean != "flat"
    }
    checks = verdict.citation_checks
    actual_symbols = [check.symbol for check in checks]
    if len(actual_symbols) != len(set(actual_symbols)) or set(actual_symbols) != set(expected):
        missing = sorted(set(expected) - set(actual_symbols))
        extra = sorted(set(actual_symbols) - set(expected))
        raise AdversaryBindingError(
            "adversary citation_checks must cover every non-flat sentiment symbol exactly once: "
            f"missing={missing}, extra={extra}, "
            f"duplicates={len(actual_symbols)-len(set(actual_symbols))}"
        )

    selected_symbols = {leg.symbol for leg in book.legs}
    by_symbol = {check.symbol: check for check in checks}
    for symbol, cited_urls in expected.items():
        if not cited_urls:
            raise AdversaryBindingError(
                f"non-flat sentiment read for {symbol} has no citable HTTP(S) URL"
            )
        check = by_symbol[symbol]
        if len(check.checked_urls) != len(set(check.checked_urls)):
            raise AdversaryBindingError(f"citation_checks for {symbol} contains duplicate URLs")
        if set(check.checked_urls) != cited_urls:
            raise AdversaryBindingError(
                f"citation_checks for {symbol} do not match the sentiment read URLs"
            )
        material = symbol in selected_symbols
        if check.material_to_book != material:
            raise AdversaryBindingError(
                f"citation_checks material_to_book is false for reviewed book symbol {symbol}"
                if material
                else f"citation_checks material_to_book is true for unselected symbol {symbol}"
            )
        if verdict.accept and material and not check.supported:
            raise AdversaryBindingError(
                f"accepted book uses unsupported sentiment evidence for selected symbol {symbol}"
            )


def verify_precheck_artifact(
    artifact: PrecheckMetrics,
    expected: PrecheckMetrics,
    *,
    cycle: int,
    label: str = "precheck",
) -> None:
    """Prove a persisted precheck is intact and was computed for this exact book/input bundle."""
    if artifact.cycle != cycle:
        raise AdversaryBindingError(
            f"{label} is for cycle {artifact.cycle}, current cycle is {cycle}"
        )
    canonical = precheck_sha256(artifact)
    if not hmac.compare_digest(artifact.sha256, canonical):
        raise AdversaryBindingError(f"{label} sha256 does not match its contents")
    if not hmac.compare_digest(artifact.sha256, expected.sha256):
        raise AdversaryBindingError(f"{label} does not match the current book and evidence")


def verify_verdict_binding(
    verdict: AdversaryVerdict,
    precheck: PrecheckMetrics,
    *,
    cycle: int,
    sentiment_reads: list[SpecialistRead] | None = None,
    book: Book | None = None,
) -> None:
    """Prove the verdict reviewed `precheck`, including its hash, metrics, and all bound IDs."""
    if verdict.cycle != cycle or precheck.cycle != cycle:
        raise AdversaryBindingError(
            f"cycle mismatch: verdict={verdict.cycle}, precheck={precheck.cycle}, current={cycle}"
        )
    if not hmac.compare_digest(verdict.precheck_sha256, precheck.sha256):
        raise AdversaryBindingError("adversary precheck_sha256 does not match reviewed precheck")

    for field in _ECHO_FIELDS:
        echoed = getattr(verdict.metrics_echo, field)
        actual = getattr(precheck, field)
        if field == "turnover_legs_changed":
            matches = echoed == actual
        else:
            matches = math.isclose(
                float(echoed), float(actual), rel_tol=ECHO_REL_TOL, abs_tol=ECHO_ABS_TOL
            )
        if not matches:
            raise AdversaryBindingError(
                f"adversary metrics_echo.{field}={echoed} does not match precheck value {actual}"
            )

    checks = {bound.bound_id: bound for bound in precheck.bounds}
    rulings = {bound.bound_id: bound for bound in verdict.bounds_confirmed}
    if set(rulings) != set(checks):
        raise AdversaryBindingError("adversary bound IDs do not match the reviewed precheck")

    for bound_id, ruling in rulings.items():
        if ruling.ok != checks[bound_id].ok and not ruling.note.strip():
            raise AdversaryBindingError(
                f"{bound_id} ruling differs from precheck without an explanatory note"
            )

    failing = [bound.bound_id for bound in precheck.bounds if not bound.ok]
    if verdict.accept and failing and not verdict.override_rationale.strip():
        raise AdversaryBindingError(
            "accepting failing precheck bounds requires override_rationale: "
            + ", ".join(failing)
        )
    if sentiment_reads is not None or book is not None:
        if sentiment_reads is None or book is None:
            raise AdversaryBindingError(
                "citation audit binding requires both sentiment_reads and reviewed book"
            )
        verify_citation_audit(verdict, sentiment_reads, book)
