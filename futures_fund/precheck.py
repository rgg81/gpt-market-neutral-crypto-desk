"""Deterministic PRE-TRADE precheck on a PROPOSED book (charter: code FEEDS data, never vetoes).

Computes the exact numbers the PM must state honestly and the Adversary must audit — gross,
deploy, dollar/beta residuals, per-leg concentration, hedge share, per-leg beta-dollars, and
turnover vs the currently-held book — plus a table of numeric bounds (B1-B12). The result is
handed to BOTH agents; the Adversary's verdict must echo it (see `AdversaryVerdict`), so a
content-free accept can no longer pass validation. This module makes NO decision: `bounds` are
data for the Adversary, and the Adversary may override any failing bound with a written
rationale. It exists because cycle 4 shipped a 5.98x-cash book whose PM stated deploy=1.069 and
whose adversary verdict was a 66-byte bare accept — nothing deterministic had computed a single
number on the proposed book before fills.
"""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.desk_contracts import Book

# Bound limits (ratified from the 2026-07-10 forensic review; backtested 5/5 on cycles 1-5:
# every defective book fires, the two honest c5 constructions pass).
DEPLOY_MIN = 0.75            # B1 low
DEPLOY_MAX = 1.15            # B1 high (gross / cash)
DOLLAR_RESIDUAL_MAX = 0.10   # B2 |longs-shorts|/gross
BETA_RESIDUAL_MAX = 0.15     # B3 |net beta-$| / cash
MAX_LEG_FRAC_GROSS = 0.35    # B4 single-leg concentration
HEDGE_FRAC_CASH_MAX = 0.50   # B5 BTC hedge leg vs cash
LEG_BETA_USD_FRAC_MAX = 0.60  # B6 per-leg |notional x beta| vs cash (the c4 root-cause bound)
STATED_TOL = 0.05            # B7 |stated - computed| tolerance on each stated_* metric
MAX_LEGS_CHANGED = 2         # B9 turnover cap
EST_SLIPPAGE_BPS_MAX = 75.0  # B10 per-leg est. slippage (needs evidence est_slippage_bps_2k)
RESIZE_BAND_FRAC = 0.07      # a notional change <=7% is drift, not a leg change
MAX_PAYBACK_CYCLES = 10.0    # B12 a changed leg must repay its round-trip friction within this
TAKER_FEE_BPS = 5.0          # matches FeeSettings.taker_bps


def _slip_bps_for(ev_row: dict, notional: float) -> float:
    """SIZE-AWARE one-way slippage (bps) for a clip of `notional` in this name.

    Reads the evidence `slippage_curve_bps` at the smallest priced clip >= the notional (the curve
    is convex, so a smaller-clip quote UNDERSTATES a bigger trade — cycle 11's $5K WLD leg cost
    4.1x its $2K probe). Falls back to the legacy flat 2k probe when no curve is present."""
    curve = ev_row.get("slippage_curve_bps") or {}
    if curve:
        priced = sorted((int(k.rstrip("k")) * 1000, v) for k, v in curve.items())
        for size, bps in priced:
            if notional <= size:
                return float(bps)
        return float(priced[-1][1])          # above the curve: use its widest (most costly) point
    return float(ev_row.get("est_slippage_bps_2k") or 0.0)


class LegMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    side: str
    notional: float
    beta: float
    beta_usd: float           # signed: +long, -short
    frac_gross: float
    change: str                # held | new | flipped | resized


class BoundCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    bound_id: str              # B1..B12
    description: str
    value: float
    limit: str
    ok: bool


class PrecheckMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cycle: int
    cash: float
    gross: float
    deploy_frac: float
    longs_usd: float
    shorts_usd: float
    dollar_residual_frac: float
    beta_net_usd: float
    beta_residual: float                     # beta_net_usd / cash
    max_leg_symbol: str = ""
    max_leg_frac_gross: float = 0.0
    hedge_notional: float = 0.0              # the BTC leg, if any
    hedge_frac_cash: float = 0.0
    max_leg_beta_usd_symbol: str = ""
    max_leg_beta_usd: float = 0.0            # max per-leg |notional x beta|
    legs: list[LegMetric] = Field(default_factory=list)
    legs_added: list[str] = Field(default_factory=list)
    legs_dropped: list[str] = Field(default_factory=list)
    legs_flipped: list[str] = Field(default_factory=list)
    legs_resized: list[str] = Field(default_factory=list)
    turnover_legs_changed: int = 0           # added + dropped + flipped (resides within band ok)
    turnover_usd: float = 0.0                # sum |proposed - held| notional deltas
    unpriced_symbols: list[str] = Field(default_factory=list)
    duplicate_symbols: list[str] = Field(default_factory=list)
    worst_changed_leg_payback_cycles: float = 0.0   # B12: size-aware friction / carry per cycle
    bounds: list[BoundCheck] = Field(default_factory=list)
    sha256: str = ""


def precheck_sha256(metrics: PrecheckMetrics) -> str:
    """Return the canonical content hash for a precheck, excluding its hash field."""
    payload = metrics.model_dump(mode="json")
    payload.pop("sha256", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# Backward-compatible private alias for callers/tests written before the integrity verifier.
_sha = precheck_sha256


def compute_precheck(
    book: Book,
    evidence: list[dict],
    *,
    cash: float,
    cycle: int,
    current_book: list[dict] | None = None,
    btc_symbol: str = "BTC/USDT:USDT",
) -> PrecheckMetrics:
    """Compute every auditable number on the PROPOSED book. Pure function of its inputs."""
    marks = {e["symbol"]: float(e["mark"]) for e in evidence}
    betas = {e["symbol"]: float(e.get("beta_btc", 1.0)) for e in evidence}
    slip_est = {e["symbol"]: e.get("est_slippage_bps_2k") for e in evidence}
    ev_by_sym = {e["symbol"]: e for e in evidence}
    fund_bps = {e["symbol"]: float(e.get("expected_funding_8h_bps") or 0.0) for e in evidence}

    seen: set[str] = set()
    duplicates: list[str] = []
    for lg in book.legs:
        if lg.symbol in seen:
            duplicates.append(lg.symbol)
        seen.add(lg.symbol)
    unpriced = sorted({lg.symbol for lg in book.legs if lg.symbol not in marks})

    held = {c["symbol"]: c for c in (current_book or [])}
    longs = sum(lg.target_notional for lg in book.legs if lg.side == "long")
    shorts = sum(lg.target_notional for lg in book.legs if lg.side == "short")
    gross = longs + shorts

    legs: list[LegMetric] = []
    beta_net = 0.0
    max_leg_sym, max_leg_frac = "", 0.0
    max_beta_sym, max_beta_usd = "", 0.0
    hedge_notional = 0.0
    added, flipped, resized = [], [], []
    turnover_usd = 0.0
    for lg in book.legs:
        beta = betas.get(lg.symbol, 1.0)
        sign = 1.0 if lg.side == "long" else -1.0
        beta_usd = sign * lg.target_notional * beta
        beta_net += beta_usd
        frac = (lg.target_notional / gross) if gross > 0 else 0.0
        if frac > max_leg_frac:
            max_leg_sym, max_leg_frac = lg.symbol, frac
        if abs(beta_usd) > abs(max_beta_usd):
            max_beta_sym, max_beta_usd = lg.symbol, abs(beta_usd)
        if lg.symbol == btc_symbol:
            hedge_notional = lg.target_notional
        prior = held.get(lg.symbol)
        if prior is None:
            change = "new"
            added.append(lg.symbol)
            turnover_usd += lg.target_notional
        elif prior["side"] != lg.side:
            change = "flipped"
            flipped.append(lg.symbol)
            turnover_usd += float(prior["target_notional"]) + lg.target_notional
        else:
            delta = abs(lg.target_notional - float(prior["target_notional"]))
            turnover_usd += delta
            base = float(prior["target_notional"]) or 1.0
            if delta / base > RESIZE_BAND_FRAC:
                change = "resized"
                resized.append(lg.symbol)
            else:
                change = "held"
        legs.append(LegMetric(symbol=lg.symbol, side=lg.side, notional=lg.target_notional,
                              beta=beta, beta_usd=beta_usd, frac_gross=frac, change=change))
    proposed_syms = {lg.symbol for lg in book.legs}
    dropped = sorted(s for s in held if s not in proposed_syms)
    turnover_usd += sum(float(held[s]["target_notional"]) for s in dropped)

    deploy = (gross / cash) if cash > 0 else 0.0
    dollar_resid = (abs(longs - shorts) / gross) if gross > 0 else 0.0
    beta_resid = (beta_net / cash) if cash > 0 else 0.0
    hedge_frac = (hedge_notional / cash) if cash > 0 else 0.0
    n_changed = len(added) + len(dropped) + len(flipped)

    worst_slip = 0.0
    slip_known = False
    for lg in book.legs:
        est = slip_est.get(lg.symbol)
        if est is not None:
            slip_known = True
            worst_slip = max(worst_slip, float(est))

    # B12 — SIZE-AWARE break-even on every CHANGED leg (the cycle-11 lesson: the agents priced a
    # $5K WLD entry off a $2K slippage probe, so a 32-cycle-payback trade passed as 7.7). Price the
    # round-trip friction at the leg's REAL notional and require it to repay within 10 cycles from
    # the carry the seat earns. A new/flipped leg pays entry + (eventual) exit: 2x one-way.
    worst_payback = 0.0
    payback_checked = False
    for lm in legs:
        if lm.change not in ("new", "flipped"):
            continue
        row = ev_by_sym.get(lm.symbol)
        if not row:
            continue
        payback_checked = True
        slip_bps = _slip_bps_for(row, lm.notional)
        friction = 2.0 * (slip_bps + TAKER_FEE_BPS) / 1e4 * lm.notional
        # carry per cycle this seat EARNS: short earns +funding, long earns -funding
        sign = 1.0 if lm.side == "short" else -1.0
        carry = sign * fund_bps.get(lm.symbol, 0.0) / 1e4 * lm.notional
        payback = (friction / carry) if carry > 1e-9 else float("inf")
        worst_payback = max(worst_payback, payback)

    def _b(bid: str, desc: str, value: float, limit: str, ok: bool) -> BoundCheck:
        return BoundCheck(bound_id=bid, description=desc, value=round(value, 6),
                          limit=limit, ok=ok)

    bounds = [
        _b("B1", "deploy gross/cash within band", deploy,
           f"[{DEPLOY_MIN}, {DEPLOY_MAX}]", DEPLOY_MIN <= deploy <= DEPLOY_MAX),
        _b("B2", "dollar residual |L-S|/gross", dollar_resid,
           f"<= {DOLLAR_RESIDUAL_MAX}", dollar_resid <= DOLLAR_RESIDUAL_MAX),
        _b("B3", "beta residual |net beta-$|/cash", abs(beta_resid),
           f"<= {BETA_RESIDUAL_MAX}", abs(beta_resid) <= BETA_RESIDUAL_MAX),
        _b("B4", "max single-leg share of gross", max_leg_frac,
           f"<= {MAX_LEG_FRAC_GROSS}", max_leg_frac <= MAX_LEG_FRAC_GROSS),
        _b("B5", "BTC hedge notional / cash", hedge_frac,
           f"<= {HEDGE_FRAC_CASH_MAX}", hedge_frac <= HEDGE_FRAC_CASH_MAX),
        _b("B6", "max per-leg |notional x beta| / cash",
           (max_beta_usd / cash) if cash > 0 else 0.0,
           f"<= {LEG_BETA_USD_FRAC_MAX}",
           (max_beta_usd / cash if cash > 0 else 0.0) <= LEG_BETA_USD_FRAC_MAX),
        _b("B7", "stated_* metrics match computed",
           max(abs(book.stated_deploy_frac - deploy),
               abs(book.stated_dollar_residual_frac - dollar_resid),
               abs(book.stated_beta_residual - beta_resid)),
           f"<= {STATED_TOL}",
           abs(book.stated_deploy_frac - deploy) <= STATED_TOL
           and abs(book.stated_dollar_residual_frac - dollar_resid) <= STATED_TOL
           and abs(book.stated_beta_residual - beta_resid) <= STATED_TOL),
        _b("B8", "book.turnover_legs_changed field is truthful",
           float(book.turnover_legs_changed),
           f"== {n_changed} (computed)", book.turnover_legs_changed == n_changed),
        _b("B9", "legs changed (added+dropped+flipped)", float(n_changed),
           f"<= {MAX_LEGS_CHANGED}", n_changed <= MAX_LEGS_CHANGED),
        _b("B10", "worst per-leg est. slippage bps (if data present)", worst_slip,
           f"<= {EST_SLIPPAGE_BPS_MAX}",
           (worst_slip <= EST_SLIPPAGE_BPS_MAX) if slip_known else True),
        _b("B11", "no duplicate or unpriced legs",
           float(len(duplicates) + len(unpriced)), "== 0",
           not duplicates and not unpriced),
        _b("B12", "worst changed-leg payback (cycles, SIZE-AWARE friction vs its carry)",
           (worst_payback if worst_payback != float("inf") else 9999.0),
           f"<= {MAX_PAYBACK_CYCLES}",
           (worst_payback <= MAX_PAYBACK_CYCLES) if payback_checked else True),
    ]

    metrics = PrecheckMetrics(
        cycle=cycle, cash=round(cash, 2), gross=round(gross, 2),
        deploy_frac=round(deploy, 6), longs_usd=round(longs, 2), shorts_usd=round(shorts, 2),
        dollar_residual_frac=round(dollar_resid, 6), beta_net_usd=round(beta_net, 2),
        beta_residual=round(beta_resid, 6),
        max_leg_symbol=max_leg_sym, max_leg_frac_gross=round(max_leg_frac, 6),
        hedge_notional=round(hedge_notional, 2), hedge_frac_cash=round(hedge_frac, 6),
        max_leg_beta_usd_symbol=max_beta_sym, max_leg_beta_usd=round(max_beta_usd, 2),
        legs=legs, legs_added=sorted(added), legs_dropped=dropped,
        legs_flipped=sorted(flipped), legs_resized=sorted(resized),
        turnover_legs_changed=n_changed, turnover_usd=round(turnover_usd, 2),
        unpriced_symbols=unpriced, duplicate_symbols=sorted(set(duplicates)),
        worst_changed_leg_payback_cycles=(
            round(worst_payback, 2) if worst_payback != float("inf") else 9999.0),
        bounds=bounds)
    metrics.sha256 = precheck_sha256(metrics)
    return metrics
