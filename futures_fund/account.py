"""Phase 9 — paper-trading P&L ledger (Position + PaperAccount).

REUSES the cost primitives — it NEVER re-implements fee/funding/slippage math:
  * costs.trade_fee / costs.count_funding_events
  * funding_intervals.clamp_funding_rate / funding_intervals.realized_funding
  * slippage.estimate_slippage

Funding sign convention (load-bearing): this ledger settles funding via
`funding_intervals.realized_funding`, which is BALANCE-credit perspective (a SHORT with a positive
rate RECEIVES funding -> a POSITIVE cash credit). Do NOT use `costs.project_funding` here (that is
the opposite, cost/paid perspective).

Funding clock (load-bearing): the account carries its OWN `last_funding_ts`, advanced by
`settle_funding`. The equity series is NOT a safe `prev_ts` source — `equity_log.record_equity` keys
only on `cycle`, so weekly cycle 1 and daily cycle 1 collide and the daily point overwrites the
weekly one in a single run.

Closed-leg carrier (load-bearing): the realized outcome of a FULLY closed leg survives in the
account-level aggregates but NOT on any Position (it is popped). To patch each closed leg's realized
costs onto the Decision that OPENED it ("at close"), `_reconcile_opposite` snapshots a `ClosedLeg`
(carrying its open cycle+cadence and realized fees/slippage/funding/price-pnl) into `closed_legs`
before popping. The close-time journal patch keys each on its OWN open cycle+cadence — never the
current cycle — and `drain_closed_legs` empties the buffer so a leg is patched exactly once.
"""
from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.costs import count_funding_events, trade_fee
from futures_fund.funding_intervals import clamp_funding_rate, realized_funding
from futures_fund.models import Direction
from futures_fund.slippage import estimate_slippage


class Position(BaseModel):
    symbol: str
    direction: Direction
    qty: float                                   # absolute contract qty (>= 0)
    entry_price: float                           # avg entry (VWAP of accumulated fills)
    opened_ts: datetime
    opened_cycle: int | None = None              # the cycle this leg was OPENED in (journal key)
    opened_cadence: str | None = None            # weekly|daily opened in (journal discriminator)
    accrued_funding: float = 0.0                 # signed, + = received, - = paid (this leg's life)
    accrued_fees: float = 0.0                    # >= 0 taker/maker fees charged to this leg
    accrued_slippage: float = 0.0                # >= 0 depth slippage charged to this leg
    realized_pnl: float = 0.0                    # signed price P&L realized on this leg so far


class ClosedLeg(BaseModel):
    """A leg that has been FULLY closed (popped from `positions`) this run, retaining its realized
    fees/slippage/funding/price-pnl AND the (cycle, cadence) it was OPENED in.

    The realized outcome of a closed leg survives ONLY in the account-level aggregates otherwise —
    the Position is gone — so it could never reach the journal "at close". This record is the
    carrier the close-time journal patch keys on its OPEN cycle/cadence (NOT the current cycle).
    Drained by `drain_closed_legs` once the patch has consumed it so a leg is patched exactly once.
    """
    symbol: str
    direction: Direction
    opened_cycle: int | None = None
    opened_cadence: str | None = None
    fees: float = 0.0
    slippage: float = 0.0
    realized_funding: float = 0.0
    realized_pnl: float = 0.0


class CostInputs(BaseModel):
    """Per-symbol frictions the paper executor needs but the executed proposal does not carry.

    `depth_asks`/`depth_bids` are the two crossing sides of the live book; `apply_fills` selects
    the ASK side for a BUY (delta>0) and the BID side for a SELL (delta<0). When both are empty
    `estimate_slippage` uses the ADV + half-spread fallback (which is NEVER flat 2bps). `depth` is
    retained for back-compat (a pre-selected single side); it wins when set.

    Over-depth clips are charged the visible-book VWAP on the portion that fits PLUS the √-impact
    remainder on the portion that exceeds the book (`estimate_slippage`), so a clip larger than the
    book is NOT under-priced. The fill reference price and these depth sides must come from the same
    execution snapshot. Pre-trade evidence is an earlier, volatility-marked forecast and can differ
    from the later execution charge; market drift between the two timestamps is not slippage."""
    adv_usd: float = 0.0
    half_spread_bps: float = 1.0
    depth: list[tuple[float, float]] | None = None
    depth_bids: list[tuple[float, float]] = Field(default_factory=list)
    depth_asks: list[tuple[float, float]] = Field(default_factory=list)
    maker: bool = False                          # paper opens are market -> taker


def _signed_qty(pos: Position | None) -> float:
    """Current signed qty: + for a long, - for a short, 0 if flat."""
    if pos is None:
        return 0.0
    return pos.qty if pos.direction == "long" else -pos.qty


class PaperAccount(BaseModel):
    cash: float
    positions: dict[str, Position] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    last_funding_ts: datetime | None = None       # the funding clock (NOT the equity series)
    # cumulative cost totals across the account's life
    fees_paid: float = 0.0                        # >= 0
    slippage_paid: float = 0.0                    # >= 0
    funding_received: float = 0.0                 # >= 0 (sum of positive settlements)
    funding_paid: float = 0.0                     # >= 0 (sum of |negative settlements|)
    # legs fully closed (popped) but not yet patched onto the journal — the "at close" carrier.
    closed_legs: list[ClosedLeg] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")

    def drain_closed_legs(self) -> list[ClosedLeg]:
        """Return the legs closed since the last drain and clear the buffer.

        The close-time journal patch consumes these (keying each on its OPEN cycle/cadence), so they
        must be drained AFTER the patch so a fully-closed leg is patched exactly once and never
        re-patched on a later cycle. Persisted between runs so a leg closed in one run is still
        patched even if the process restarts before the patch lands."""
        drained = list(self.closed_legs)
        self.closed_legs = []
        return drained

    @classmethod
    def from_dict(cls, data: dict) -> PaperAccount:
        return cls.model_validate(data)

    def mark_to_market(self, marks: dict[str, float]) -> dict[str, float]:
        """Unrealized PnL per held symbol (skips symbols with no mark).

        long: qty*(mark-entry) ; short: qty*(entry-mark)."""
        upnl: dict[str, float] = {}
        for sym, pos in self.positions.items():
            mark = marks.get(sym)
            if mark is None:
                continue
            if pos.direction == "long":
                upnl[sym] = pos.qty * (mark - pos.entry_price)
            else:
                upnl[sym] = pos.qty * (pos.entry_price - mark)
        return upnl

    def equity(self, marks: dict[str, float]) -> float:
        """cash + sum unrealized PnL (skips symbols missing a mark)."""
        return self.cash + sum(self.mark_to_market(marks).values())

    def settle_funding(
        self,
        prev_ts: datetime,
        now: datetime,
        funding_by_symbol: dict[str, float],
        intervals: dict[str, int],
        marks: dict[str, float],
    ) -> None:
        """Settle funding for every held position over (prev_ts, now], then ADVANCE the funding
        clock to `now`.

        Per symbol: n = count_funding_events(prev_ts, now, interval); clamp the rate; credit
        realized_funding(0, mark, qty, clamped_rate, direction) * n to cash (BALANCE-credit
        perspective: a SHORT with a positive rate RECEIVES). Accumulate signed per-position
        accrued_funding and split the total into funding_received (+) / funding_paid (|-|).
        `last_funding_ts` always moves to `now` (even with 0 events) so the next cycle's window
        starts here — the account, not the cycle-collided equity series, is the funding clock."""
        for sym, pos in self.positions.items():
            mark = marks.get(sym)
            if mark is None:
                continue
            interval = int(intervals.get(sym, 8))
            n = count_funding_events(prev_ts, now, interval)
            if n <= 0:
                continue
            rate = clamp_funding_rate(sym, funding_by_symbol.get(sym, 0.0))
            per_event = realized_funding(0.0, mark, pos.qty, rate, pos.direction)
            settled = per_event * n
            pos.accrued_funding += settled
            self.cash += settled
            if settled >= 0.0:
                self.funding_received += settled
            else:
                self.funding_paid += -settled
        self.last_funding_ts = now

    def apply_fills(
        self,
        executed_trades: list[dict],
        marks: dict[str, float],
        costs: dict[str, CostInputs],
        *,
        opened_ts: datetime | None = None,
        opened_cycle: int | None = None,
        opened_cadence: str | None = None,
    ) -> None:
        """RECONCILE the WHOLE held book to the new FULL intended book.

        `executed_trades` is the FULL consolidated (neutral, hedge-correct) target book — NOT a
        sparse execution delta — so the held positions always track the intended book. Each touched
        symbol is reconciled to its NET signed target across all of its legs, and any HELD symbol
        ABSENT from the new book is FLATTENED (target 0): a name dropped at reselection/rebalance
        must be closed, not left lingering (else the held book silently breaks neutrality).

        The optimizer legitimately emits the SAME symbol on BOTH sides (e.g. a factor SHORT and a
        hedge LONG): those legs NET to a single per-symbol position. We therefore CONSOLIDATE the
        executed legs by symbol into one net signed target_notional —
        `net_signed[sym] = Sum over that symbol's legs of (+target_notional if long else -target)`
        — and reconcile each symbol exactly ONCE (direction = sign of the net; |net|/mark qty).
        Processing the legs sequentially instead would let the second leg FLIP out the first
        (a BTC short $2116 then a BTC long $2129 -> held +$2129, losing the offsetting short), so
        the held book is silently NOT market-neutral even when the leg-level book is.

        Each leg's `target_notional` is the optimizer's TARGET; this fills only
        `delta = target_signed_qty - current_signed_qty`, so re-sending the identical book is an
        exact no-op (delta 0, 0 frictions). A positive delta opens/increases the SAME side (blending
        entry VWAP); a negative delta reduces/closes/flips (Task 4). Fill at the mark; charge a
        taker/maker fee + depth slippage on the |NET delta notional| actually traded (once per
        symbol, NOT per leg). qty is derived from notional/mark because the executed proposal
        carries no fill price/qty.

        Convergent across weeks (weekly re-emits the full book -> delta 0 on unchanged legs) and
        correct for daily (each rebalance_trades leg carries that symbol's NEW target_notional)."""
        ts = opened_ts or datetime.now(tz=UTC)
        # Consolidate by symbol into a single NET signed target_notional, preserving first-seen
        # order (so a single-leg book is processed exactly as before — an exact no-op on a re-send).
        net_signed_notional: dict[str, float] = {}
        for trade in executed_trades:
            sym = trade["symbol"]
            direction: Direction = trade["direction"]
            target_notional = abs(float(trade["target_notional"]))
            leg_sign = 1.0 if direction == "long" else -1.0
            net_signed_notional[sym] = (
                net_signed_notional.get(sym, 0.0) + leg_sign * target_notional)
        # CLOSE any currently-HELD symbol ABSENT from the new (full intended) book — a name dropped
        # at reselection/rebalance must be FLATTENED (realizing its PnL + a close fee/slippage), not
        # left lingering. Without this the dropped leg silently survives and breaks neutrality. We
        # synthesize a target-0 leg for it so it routes the SAME consolidated reconcile/close path
        # as an explicit zero-target leg (no special-casing). Snapshot the keys first — the
        # reconcile loop below mutates `positions`.
        dropped = [sym for sym in self.positions if sym not in net_signed_notional]
        for sym in dropped:
            net_signed_notional[sym] = 0.0
        for sym, net_notional in net_signed_notional.items():
            existing = self.positions.get(sym)
            mark = marks.get(sym)
            if mark is None or mark <= 0:
                # A held symbol with NO mark cannot be truthfully closed — closing at entry_price
                # fabricates realized_pnl=0 and erases the position's true market move from equity
                # (2026-07 review: EDGE/POWER, $10K of book, had their PnL zeroed this way). HALT
                # instead: the caller must supply a mark for every held symbol (desk_evidence now
                # unions held symbols into the universe, so this only fires on a real data gap).
                if existing is not None and abs(net_notional) <= 1e-9:
                    raise ValueError(
                        f"apply_fills: held position {sym} has no mark — cannot close truthfully "
                        "(would fabricate realized_pnl=0 at entry price). Supply a mark for every "
                        "held symbol (HALT per charter rather than falsify the ledger).")
                # A symbol with no mark AND a non-zero target can't be sized -> skip (as before).
                continue
            direction = "long" if net_notional >= 0.0 else "short"
            ci = costs.get(sym) or CostInputs()
            sign = 1.0 if direction == "long" else -1.0
            target_signed_qty = sign * (abs(net_notional) / mark)
            current_signed_qty = _signed_qty(existing)
            delta_signed_qty = target_signed_qty - current_signed_qty
            if math.isclose(
                target_signed_qty,
                current_signed_qty,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                continue  # already at target -> no-op (re-sent unchanged book)
            delta_notional = abs(delta_signed_qty) * mark
            fee = trade_fee(delta_notional, maker=ci.maker)
            if ci.depth is not None:
                side = ci.depth                                 # pre-selected (back-compat)
            elif delta_signed_qty > 0:
                side = ci.depth_asks or None                    # BUY crosses the ASKS
            else:
                side = ci.depth_bids or None                    # SELL crosses the BIDS
            slip = estimate_slippage(
                sym, abs(delta_signed_qty), mark, depth=side, adv_usd=ci.adv_usd,
                half_spread_bps=ci.half_spread_bps)

            if existing is not None and (
                target_signed_qty * current_signed_qty < 0
                or abs(target_signed_qty) < abs(current_signed_qty)
            ):
                # NOT a pure same-side increase -> reduce/close/FLIP (Task 4). A FLIP
                # (opposite signs) makes the delta overshoot past zero, so the old
                # `delta_signed_qty * sign < 0` predicate came out POSITIVE and let a
                # short leg silently grow a long position; route every non-increase here.
                self._reconcile_opposite(
                    existing, sym, direction, target_signed_qty, mark, fee, slip, ts,
                    opened_cycle=opened_cycle, opened_cadence=opened_cadence)
                continue

            # same-side open/increase: fill |delta| at the mark, blend entry VWAP.
            fill_qty = abs(delta_signed_qty)
            self._charge_frictions(sym, fee, slip, existing)
            if existing is None:
                self.positions[sym] = Position(
                    symbol=sym, direction=direction, qty=fill_qty, entry_price=mark,
                    opened_ts=ts, opened_cycle=opened_cycle, opened_cadence=opened_cadence,
                    accrued_fees=fee, accrued_slippage=slip)
            else:
                total_qty = existing.qty + fill_qty
                existing.entry_price = (
                    existing.entry_price * existing.qty + mark * fill_qty) / total_qty
                existing.qty = total_qty

    def _charge_frictions(
        self, sym: str, fee: float, slip: float, pos: Position | None
    ) -> None:
        self.cash -= fee + slip
        self.fees_paid += fee
        self.slippage_paid += slip
        if pos is not None:
            pos.accrued_fees += fee
            pos.accrued_slippage += slip

    def _reconcile_opposite(
        self, existing: Position, sym: str, direction: Direction,
        target_signed_qty: float, mark: float, fee: float, slip: float, ts: datetime,
        *, opened_cycle: int | None = None, opened_cadence: str | None = None,
    ) -> None:
        """Drive the held qty TOWARD `target_signed_qty` when the delta opposes the held side:
        reduce -> (close) -> (flip). Realize P&L on the closed portion, charge the
        (already-computed) frictions, and open the residual the other way on a flip. Frictions were
        sized on the FULL |delta notional| by `apply_fills`, so they are charged once here.

        On a FULL close the leg is popped from `positions`, so its realized fees/slippage/funding/
        price-pnl would otherwise be lost to the per-leg journal patch — we snapshot it into
        `closed_legs` (keyed on its OPEN cycle/cadence) BEFORE popping so the close-time patch can
        land it on the Decision that opened it.

        On a FLIP, the frictions are split pro-rata between the closing OLD leg and the opening
        NEW leg (by their notional shares of the traded delta): billing the whole flip to the old
        leg charged the NEW decision's entry cost to the PRIOR cycle's decision, corrupting the
        per-decision learning journal (equity-neutral, attribution-wrong — 2026-07 review)."""
        current_signed_qty = _signed_qty(existing)
        # qty being closed on the held side = min(|delta|, held qty), capped at a full close.
        delta_signed = target_signed_qty - current_signed_qty
        closed_qty = min(abs(delta_signed), existing.qty)
        new_open_qty = abs(target_signed_qty) if (
            target_signed_qty * current_signed_qty < 0) else 0.0
        total_traded = closed_qty + new_open_qty
        old_share = (closed_qty / total_traded) if total_traded > 0 else 1.0
        old_fee, old_slip = fee * old_share, slip * old_share
        new_fee, new_slip = fee - old_fee, slip - old_slip
        self._charge_frictions(sym, old_fee, old_slip, existing)
        if existing.direction == "long":
            realized = closed_qty * (mark - existing.entry_price)
        else:
            realized = closed_qty * (existing.entry_price - mark)
        self.realized_pnl += realized
        existing.realized_pnl += realized
        self.cash += realized

        residual_held = existing.qty - closed_qty
        if residual_held > 1e-12:
            existing.qty = residual_held
            return
        # fully closed this side -> snapshot its realized outcome (for the journal patch), then pop.
        self.closed_legs.append(ClosedLeg(
            symbol=existing.symbol, direction=existing.direction,
            opened_cycle=existing.opened_cycle, opened_cadence=existing.opened_cadence,
            fees=existing.accrued_fees, slippage=existing.accrued_slippage,
            realized_funding=existing.accrued_funding, realized_pnl=existing.realized_pnl))
        self.positions.pop(sym, None)
        residual_new_qty = abs(target_signed_qty)
        if residual_new_qty > 1e-12:                # FLIP: open to reach the opposite-side target
            new_pos = Position(
                symbol=sym, direction=direction, qty=residual_new_qty, entry_price=mark,
                opened_ts=ts, opened_cycle=opened_cycle, opened_cadence=opened_cadence,
                accrued_fees=0.0, accrued_slippage=0.0)
            self.positions[sym] = new_pos
            # the NEW leg's share of the flip frictions bills the NEW decision (see docstring)
            self._charge_frictions(sym, new_fee, new_slip, new_pos)
        elif new_fee or new_slip:
            # target went exactly to zero but a new-open share was computed — charge it to the
            # account (no new position to carry it); keeps cash identical to the old behavior.
            self._charge_frictions(sym, new_fee, new_slip, None)


def _account_path(state_dir) -> Path:
    return Path(state_dir) / "account.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp + os.replace — a crash mid-write leaves the PRIOR account.json intact (same discipline
    as cycle_io.save_output / equity_log.record_equity)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_account(state_dir, default_cash: float) -> PaperAccount:
    """Load the single account.json at the state root, or init a fresh account at `default_cash`
    (zero positions, no funding clock) on a clean state dir — the restart-from-scratch path."""
    p = _account_path(state_dir)
    if p.exists():
        return PaperAccount.from_dict(json.loads(p.read_text()))
    return PaperAccount(cash=default_cash)


def save_account(state_dir, account: PaperAccount) -> None:
    _atomic_write_text(_account_path(state_dir), json.dumps(account.to_dict(), indent=2))
