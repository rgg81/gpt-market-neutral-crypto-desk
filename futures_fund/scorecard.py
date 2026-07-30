"""Attribution + recurrence detection for the self-learning loop.

Pure functions and pydantic models — NO I/O. The deterministic 'measure' half of
the reflector: score each agent's decisions against realized forward returns, then
(Task 2) surface RECURRENT misbehaviours for the Reflector agent to act on.
Records, never decides."""
from __future__ import annotations

from pydantic import BaseModel, Field

from futures_fund.desk_contracts import Book, SpecialistRead

K_DEFAULT = 3
WINDOW_DEFAULT = 6
HI_CONV = 0.6
LAX_ALPHA_FRAC = -0.005  # accepted book whose realized alpha < -0.5% of gross
                          # => adversary too lax


def forward_returns(
    marks_prev: dict[str, float], marks_now: dict[str, float]
) -> dict[str, float]:
    """Per-symbol return marks_prev -> marks_now, for symbols present (and
    non-zero) in BOTH."""
    out: dict[str, float] = {}
    for sym, mp in marks_prev.items():
        mn = marks_now.get(sym)
        if mp and mn:
            out[sym] = mn / mp - 1.0
    return out


class SpecialistScore(BaseModel):
    role: str
    n_scored: int = 0
    hit_rate: float = 0.0
    conv_weighted_edge: float = 0.0
    hi_n: int = 0
    hi_conv_hit_rate: float = 0.0


def score_specialist(
    role: str, reads: list[SpecialistRead], rets: dict[str, float]
) -> SpecialistScore:
    hits = n = hi_hits = hi_n = 0
    edge = 0.0
    for r in reads:
        if r.lean == "flat":
            continue
        ret = rets.get(r.symbol)
        if ret is None:
            continue
        n += 1
        signed = 1.0 if r.lean == "long" else -1.0
        hit = (signed * ret) > 0
        hits += 1 if hit else 0
        edge += signed * ret * r.conviction
        if r.conviction > HI_CONV:
            hi_n += 1
            hi_hits += 1 if hit else 0
    return SpecialistScore(
        role=role,
        n_scored=n,
        hit_rate=(hits / n) if n else 0.0,
        conv_weighted_edge=(edge / n) if n else 0.0,
        hi_n=hi_n,
        hi_conv_hit_rate=(hi_hits / hi_n) if hi_n else 0.0,
    )


class BookScore(BaseModel):
    n_legs: int = 0
    gross_notional: float = 0.0
    gross_pnl: float = 0.0
    return_frac: float = 0.0
    beta_dollar: float = 0.0
    alpha_net_beta: float = 0.0
    alpha_frac: float = 0.0


def score_book(
    book: Book, rets: dict[str, float], betas: dict[str, float], btc_ret: float
) -> BookScore:
    gross = pnl = beta_d = 0.0
    n = 0
    for lg in book.legs:
        ret = rets.get(lg.symbol)
        if ret is None:
            continue
        signed = lg.target_notional if lg.side == "long" else -lg.target_notional
        pnl += signed * ret
        beta_d += signed * betas.get(lg.symbol, 1.0)
        gross += lg.target_notional
        n += 1
    alpha = pnl - beta_d * btc_ret
    return BookScore(
        n_legs=n,
        gross_notional=gross,
        gross_pnl=pnl,
        return_frac=(pnl / gross) if gross else 0.0,
        beta_dollar=beta_d,
        alpha_net_beta=alpha,
        alpha_frac=(alpha / gross) if gross else 0.0,
    )


_TAGS: dict[str, tuple[str, ...]] = {
    "concentration": ("concentrat", "single-name", "single name", "dominates"),
    "hallucination": (
        "hallucinat",
        "unverifiab",
        "invented",
        "fabricat",
        "unsourced",
        "cannot verify",
        "couldn't verify",
        "could not verify",
    ),
    "under_deployment": (
        "under-deploy",
        "underdeploy",
        "under deployed",
        "sits under",
        "deploy to ~",
    ),
    "tilt": ("tilt", "directional"),
    "beta": ("beta",),
    "crowded": ("crowded", "low-conviction", "low conviction"),
}


def classify_objections(objections: list[str]) -> list[str]:
    """Deterministic keyword -> reason-tag map over adversary objection
    strings."""
    tags: list[str] = []
    for text in objections:
        low = text.lower()
        for tag, kws in _TAGS.items():
            if tag not in tags and any(kw in low for kw in kws):
                tags.append(tag)
    if objections and not tags:
        tags.append("other")
    return tags


class ScoreRecord(BaseModel):
    cycle: int
    scored_at: str = ""
    n_symbols: int = 0
    specialists: dict[str, SpecialistScore] = Field(default_factory=dict)
    book: BookScore = Field(default_factory=BookScore)
    adv_accepted: bool = True
    adv_revised: bool = False
    adv_reason_tags: list[str] = Field(default_factory=list)


class Recurrence(BaseModel):
    kind: str
    role: str
    count: int
    window: int
    evidence: list[str] = Field(default_factory=list)
    suggestion: str = ""


_SPECIALIST_ROLES = ("sentiment", "technical", "futures")


def detect_recurrences(
    records: list[ScoreRecord], *, k: int = K_DEFAULT,
    window: int = WINDOW_DEFAULT
) -> list[Recurrence]:
    """Emit a Recurrence for each pattern true in >=k of the last `window`
    scored records."""
    recent = records[-window:]
    w = len(recent)
    if w < k:
        return []
    out: list[Recurrence] = []

    for role in _SPECIALIST_ROLES:
        bad = [r for r in recent
               if role in r.specialists and r.specialists[role].n_scored > 0
               and r.specialists[role].conv_weighted_edge < 0]
        if len(bad) >= k:
            out.append(Recurrence(
                kind="specialist_miscalibrated", role=role, count=len(bad), window=w,
                evidence=[
                    f"c{r.cycle}: edge={r.specialists[role].conv_weighted_edge:+.4f} "
                    f"hit={r.specialists[role].hit_rate:.2f}" for r in bad
                ],
                suggestion=(
                    f"{role}'s conviction-weighted calls lost money in {len(bad)}/{w} "
                    "cycles; tighten conviction discipline / demand stronger evidence."
                ),
            ))
        oc = [r for r in recent
              if role in r.specialists and r.specialists[role].hi_n > 0
              and r.specialists[role].hi_conv_hit_rate < 0.5]
        if len(oc) >= k:
            out.append(Recurrence(
                kind="specialist_overconviction", role=role, count=len(oc), window=w,
                evidence=[
                    f"c{r.cycle}: hi_conv_hit={r.specialists[role].hi_conv_hit_rate:.2f} "
                    f"(n={r.specialists[role].hi_n})" for r in oc
                ],
                suggestion=(
                    f"{role}'s high-conviction (>0.6) calls were wrong more often than not "
                    f"in {len(oc)}/{w} cycles; raise the bar for high conviction."
                ),
            ))

    neg = [r for r in recent if r.book.n_legs > 0 and r.book.alpha_frac < 0]
    if len(neg) >= k:
        out.append(Recurrence(
            kind="pm_negative_alpha", role="pm", count=len(neg), window=w,
            evidence=[f"c{r.cycle}: alpha_frac={r.book.alpha_frac:+.4f}" for r in neg],
            suggestion=(
                f"the book's alpha-net-of-beta was negative in {len(neg)}/{w} cycles; "
                "reconsider name selection / sizing by conviction."
            ),
        ))

    tag_cycles: dict[str, list[int]] = {}
    for r in recent:
        if not r.adv_accepted:
            for t in r.adv_reason_tags:
                tag_cycles.setdefault(t, []).append(r.cycle)
    for tag, cyc in tag_cycles.items():
        if len(cyc) >= k:
            out.append(Recurrence(
                kind="pm_rejected_same_reason", role="pm", count=len(cyc), window=w,
                evidence=[f"rejected for '{tag}' in cycles {cyc}"],
                suggestion=(
                    f"the adversary rejected the PM for '{tag}' in {len(cyc)}/{w} cycles; "
                    "bake the fix into the PM's construction rules."
                ),
            ))

    lax = [r for r in recent if r.adv_accepted and not r.adv_revised
           and r.book.n_legs > 0 and r.book.alpha_frac < LAX_ALPHA_FRAC]
    if len(lax) >= k:
        out.append(Recurrence(
            kind="adversary_too_lax", role="adversary", count=len(lax), window=w,
            evidence=[
                f"c{r.cycle}: accepted, alpha_frac={r.book.alpha_frac:+.4f}" for r in lax
            ],
            suggestion=(
                f"the adversary accepted books that then lost (alpha < "
                f"{LAX_ALPHA_FRAC:+.3f}) in {len(lax)}/{w} cycles; sharpen its risk scrutiny."
            ),
        ))
    return out
