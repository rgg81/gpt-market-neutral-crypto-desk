"""Phase 9 — paper-trading P&L ledger (Position + PaperAccount).

REUSES the cost primitives — it NEVER re-implements fee/funding/slippage math:
  * costs.trade_fee / costs.count_funding_events
  * funding_intervals.realized_funding (raw published rates for realized cash)
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from futures_fund.costs import count_funding_events, trade_fee
from futures_fund.durable_io import durable_write_text
from futures_fund.funding_intervals import realized_funding
from futures_fund.models import Direction
from futures_fund.slippage import estimate_slippage


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str = Field(min_length=1)
    direction: Direction
    qty: float = Field(gt=0.0)  # absolute held contract qty
    entry_price: float = Field(gt=0.0)  # avg entry (VWAP of accumulated fills)
    opened_ts: datetime
    opened_cycle: int | None = None  # the cycle this leg was OPENED in (journal key)
    opened_cadence: str | None = None  # weekly|daily opened in (journal discriminator)
    seat_role: Literal["alpha", "hedge"] = "alpha"
    thesis_cycle: int | None = None
    thesis_book_sha256: str | None = None
    expected_price_edge_frac: float | None = Field(default=None, ge=-1.0, le=1.0)
    edge_horizon_hours: int | None = Field(default=None, ge=1)
    edge_calibration_basis: str = ""
    invalidation_condition: str = ""
    accrued_funding: float = 0.0  # signed, + = received, - = paid (this leg's life)
    accrued_fees: float = Field(default=0.0, ge=0.0)
    accrued_slippage: float = Field(default=0.0, ge=0.0)
    realized_pnl: float = 0.0  # signed price P&L realized on this leg so far

    @model_validator(mode="after")
    def validate_opened_timestamp(self) -> Position:
        if self.opened_ts.tzinfo is None:
            raise ValueError("position opened_ts must be timezone-aware")
        return self


class ClosedLeg(BaseModel):
    """A fully closed leg or zero-turnover role lifecycle transfer.

    Retains realized fees/slippage/funding/price-pnl and the (cycle, cadence) it was opened in.

    The realized outcome of a closed leg survives ONLY in the account-level aggregates otherwise —
    the Position is gone — so it could never reach the journal "at close". This record is the
    carrier the close-time journal patch keys on its OPEN cycle/cadence (NOT the current cycle).
    Drained by `drain_closed_legs` once the patch has consumed it so a leg is patched exactly once.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str = Field(min_length=1)
    direction: Direction
    opened_cycle: int | None = None
    opened_cadence: str | None = None
    seat_role: Literal["alpha", "hedge"] = "alpha"
    thesis_cycle: int | None = None
    thesis_book_sha256: str | None = None
    expected_price_edge_frac: float | None = Field(default=None, ge=-1.0, le=1.0)
    edge_horizon_hours: int | None = Field(default=None, ge=1)
    edge_calibration_basis: str = ""
    invalidation_condition: str = ""
    fees: float = Field(default=0.0, ge=0.0)
    slippage: float = Field(default=0.0, ge=0.0)
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

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    adv_usd: float = Field(default=0.0, ge=0.0)
    half_spread_bps: float = Field(default=1.0, ge=0.0)
    depth: list[tuple[float, float]] | None = None
    depth_bids: list[tuple[float, float]] = Field(default_factory=list)
    depth_asks: list[tuple[float, float]] = Field(default_factory=list)
    maker: bool = False  # paper opens are market -> taker
    adverse_selection_bps: float = Field(default=0.0, ge=0.0)
    legging_bps: float = Field(default=0.0, ge=0.0)


def _signed_qty(pos: Position | None) -> float:
    """Current signed qty: + for a long, - for a short, 0 if flat."""
    if pos is None:
        return 0.0
    return pos.qty if pos.direction == "long" else -pos.qty


class PaperAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cash: float = Field(ge=0.0)
    positions: dict[str, Position] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    last_funding_ts: datetime | None = None  # the funding clock (NOT the equity series)
    # Interval last observed when this account's funding clock advanced. It lets the historical
    # collector fail closed across an observed adaptive-interval transition rather than assuming
    # today's schedule governed the whole outage.
    funding_intervals_observed: dict[str, int] = Field(default_factory=dict)
    # cumulative cost totals across the account's life
    fees_paid: float = Field(default=0.0, ge=0.0)
    slippage_paid: float = Field(default=0.0, ge=0.0)
    funding_received: float = Field(default=0.0, ge=0.0)
    funding_paid: float = Field(default=0.0, ge=0.0)
    # closed/role-transferred lifecycle sleeves not yet patched onto the attribution journal.
    closed_legs: list[ClosedLeg] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_account_state(self) -> PaperAccount:
        for key, position in self.positions.items():
            if key != position.symbol:
                raise ValueError(
                    f"position map key {key!r} differs from Position.symbol {position.symbol!r}"
                )
        extra_intervals = sorted(set(self.funding_intervals_observed) - set(self.positions))
        if extra_intervals:
            raise ValueError(
                f"funding interval metadata contains non-held symbols: {extra_intervals}"
            )
        invalid_intervals = {
            symbol: interval
            for symbol, interval in self.funding_intervals_observed.items()
            if int(interval) != interval or int(interval) not in {1, 2, 4, 8}
        }
        if invalid_intervals:
            raise ValueError(f"invalid held funding intervals: {invalid_intervals}")
        if self.last_funding_ts is not None and self.last_funding_ts.tzinfo is None:
            raise ValueError("account last_funding_ts must be timezone-aware")
        return self

    def _validated_for_persistence(self) -> PaperAccount:
        validated = type(self).model_validate(self.model_dump())
        if validated.positions and validated.last_funding_ts is None:
            raise ValueError(
                "persisted held account has no funding clock; "
                "explicit audited migration is required"
            )
        return validated

    def to_dict(self) -> dict:
        return self._validated_for_persistence().model_dump(mode="json")

    def drain_closed_legs(self) -> list[ClosedLeg]:
        """Return lifecycle sleeves closed since the last drain and clear the buffer.

        The close-time journal patch consumes these (keying each on its OPEN cycle/cadence), so they
        must be drained AFTER the patch so a fully-closed leg is patched exactly once and never
        re-patched on a later cycle. Persisted between runs so a leg closed in one run is still
        patched even if the process restarts before the patch lands."""
        drained = list(self.closed_legs)
        self.closed_legs = []
        return drained

    @staticmethod
    def _assign_thesis(position: Position, thesis: dict) -> None:
        if not thesis:
            return
        position.thesis_cycle = thesis.get("thesis_cycle")
        position.thesis_book_sha256 = thesis.get("thesis_book_sha256")
        position.expected_price_edge_frac = thesis.get("expected_price_edge_frac")
        position.edge_horizon_hours = thesis.get("edge_horizon_hours")
        position.edge_calibration_basis = str(thesis.get("edge_calibration_basis") or "")
        position.invalidation_condition = str(thesis.get("invalidation_condition") or "")

    def _roll_seat_lifecycle(
        self,
        position: Position,
        *,
        mark: float,
        ts: datetime,
        opened_cycle: int | None,
        opened_cadence: str | None,
        target_seat_role: Literal["alpha", "hedge"],
        thesis: dict,
    ) -> None:
        """Close/reopen attribution at one mark for a zero-turnover semantic role change.

        No exchange trade, fee, or slippage occurs. The old role's unrealized price PnL is moved
        into cash so total equity is unchanged, its complete lifecycle is journaled, and the same
        quantity starts a new role/thesis baseline at the transfer mark.
        """
        realized = (
            position.qty * (mark - position.entry_price)
            if position.direction == "long"
            else position.qty * (position.entry_price - mark)
        )
        self.realized_pnl += realized
        position.realized_pnl += realized
        self.cash += realized
        self.closed_legs.append(
            ClosedLeg(
                symbol=position.symbol,
                direction=position.direction,
                opened_cycle=position.opened_cycle,
                opened_cadence=position.opened_cadence,
                seat_role=position.seat_role,
                thesis_cycle=position.thesis_cycle,
                thesis_book_sha256=position.thesis_book_sha256,
                expected_price_edge_frac=position.expected_price_edge_frac,
                edge_horizon_hours=position.edge_horizon_hours,
                edge_calibration_basis=position.edge_calibration_basis,
                invalidation_condition=position.invalidation_condition,
                fees=position.accrued_fees,
                slippage=position.accrued_slippage,
                realized_funding=position.accrued_funding,
                realized_pnl=position.realized_pnl,
            )
        )
        position.entry_price = mark
        position.opened_ts = ts
        position.opened_cycle = opened_cycle
        position.opened_cadence = opened_cadence
        position.seat_role = target_seat_role
        position.accrued_funding = 0.0
        position.accrued_fees = 0.0
        position.accrued_slippage = 0.0
        position.realized_pnl = 0.0
        self._assign_thesis(position, thesis)

    @classmethod
    def from_dict(cls, data: dict) -> PaperAccount:
        account = cls.model_validate(data)
        return account._validated_for_persistence()

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

        Per symbol: n = count_funding_events(prev_ts, now, interval); credit the raw observed rate
        through realized_funding(0, mark, qty, rate, direction) * n to cash (BALANCE-credit
        perspective: a SHORT with a positive rate RECEIVES). Accumulate signed per-position
        accrued_funding and split the total into funding_received (+) / funding_paid (|-|).
        `last_funding_ts` always moves to `now` (even with 0 events) so the next cycle's window
        starts here — the account, not the cycle-collided equity series, is the funding clock."""
        if self.positions and self.last_funding_ts is None:
            raise ValueError(
                "held account has no funding clock; explicit audited migration is required"
            )
        start = prev_ts.replace(tzinfo=UTC) if prev_ts.tzinfo is None else prev_ts.astimezone(UTC)
        end = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        if end < start:
            raise ValueError("funding settlement timestamp precedes previous timestamp")
        if self.last_funding_ts is not None:
            clock = self.last_funding_ts.astimezone(UTC)
            if start != clock:
                raise ValueError("funding settlement start does not equal the account clock")
        held = set(self.positions)
        for label, supplied in (
            ("funding rates", funding_by_symbol),
            ("funding intervals", intervals),
            ("funding marks", marks),
        ):
            missing = sorted(held - set(supplied))
            if missing:
                raise ValueError(f"{label} missing held symbols: {missing}")

        staged: list[tuple[Position, float]] = []
        for sym, pos in self.positions.items():
            mark = float(marks[sym])
            rate = float(funding_by_symbol[sym])
            interval_raw = float(intervals[sym])
            interval = int(interval_raw)
            if (
                not math.isfinite(mark)
                or mark <= 0.0
                or not math.isfinite(rate)
                or not math.isfinite(interval_raw)
                or interval_raw != interval
                or interval not in {1, 2, 4, 8}
            ):
                raise ValueError(f"{sym}: invalid funding rate/mark/interval")
            n = count_funding_events(start, end, interval)
            if n <= 0:
                continue
            per_event = realized_funding(0.0, mark, pos.qty, rate, pos.direction)
            staged.append((pos, per_event * n))
        new_cash = self.cash + sum(settled for _position, settled in staged)
        if not math.isfinite(new_cash) or new_cash < 0.0:
            raise ValueError("funding settlement would make PAPER cash invalid")
        for pos, settled in staged:
            pos.accrued_funding += settled
            self.cash += settled
            if settled >= 0.0:
                self.funding_received += settled
            else:
                self.funding_paid += -settled
        self.last_funding_ts = end

    def settle_funding_events(
        self,
        events_by_symbol: dict[str, list[dict]],
        *,
        now: datetime,
        observed_intervals: dict[str, int] | None = None,
    ) -> None:
        """Settle exact historical ``rate`` + ``mark`` pairs, then advance the funding clock.

        Every event is validated and staged before cash or position state changes. The caller must
        separately prove boundary completeness (``collect_funding_events`` does so); this method
        proves the supplied data is safe to apply once and cannot cross the account clock.
        """
        end = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        if self.positions and self.last_funding_ts is None:
            raise ValueError(
                "held account has no funding clock; explicit audited migration is required"
            )
        previous = self.last_funding_ts or end
        previous = (
            previous.replace(tzinfo=UTC) if previous.tzinfo is None else previous.astimezone(UTC)
        )
        if end < previous:
            raise ValueError("funding settlement timestamp precedes the account funding clock")
        missing = sorted(set(self.positions) - set(events_by_symbol))
        if missing:
            raise ValueError(f"funding events missing held symbols: {missing}")
        missing_intervals = sorted(set(self.positions) - set(observed_intervals or {}))
        if missing_intervals:
            raise ValueError(
                f"observed funding intervals missing held symbols: {missing_intervals}"
            )
        invalid_intervals = {
            symbol: (observed_intervals or {})[symbol]
            for symbol in self.positions
            if float((observed_intervals or {})[symbol])
            not in {1.0, 2.0, 4.0, 8.0}
        }
        if invalid_intervals:
            raise ValueError(f"invalid observed funding intervals: {invalid_intervals}")

        staged: list[tuple[Position, float]] = []
        for symbol, position in self.positions.items():
            seen: set[datetime] = set()
            for raw in events_by_symbol[symbol]:
                timestamp = raw.get("timestamp")
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp)
                if not isinstance(timestamp, datetime):
                    raise ValueError(f"{symbol}: funding event lacks a timestamp")
                timestamp = (
                    timestamp.replace(tzinfo=UTC)
                    if timestamp.tzinfo is None
                    else timestamp.astimezone(UTC)
                )
                if timestamp in seen:
                    raise ValueError(f"{symbol}: duplicate funding event {timestamp.isoformat()}")
                seen.add(timestamp)
                if not previous < timestamp <= end:
                    raise ValueError(
                        f"{symbol}: funding event {timestamp.isoformat()} outside "
                        f"({previous.isoformat()}, {end.isoformat()}]"
                    )
                rate = float(raw.get("rate"))
                mark = float(raw.get("mark"))
                if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0.0:
                    raise ValueError(f"{symbol}: invalid historical funding rate/mark")
                settled = realized_funding(
                    0.0,
                    mark,
                    position.qty,
                    rate,
                    position.direction,
                )
                staged.append((position, settled))

        new_cash = self.cash + sum(settled for _position, settled in staged)
        if not math.isfinite(new_cash) or new_cash < 0.0:
            raise ValueError("funding settlement would make PAPER cash invalid")
        for position, settled in staged:
            position.accrued_funding += settled
            self.cash += settled
            if settled >= 0.0:
                self.funding_received += settled
            else:
                self.funding_paid += -settled
        self.last_funding_ts = end
        self.funding_intervals_observed = {
            symbol: int((observed_intervals or {})[symbol]) for symbol in self.positions
        }

    def apply_fills(
        self,
        executed_trades: list[dict],
        marks: dict[str, float],
        costs: dict[str, CostInputs],
        *,
        opened_ts: datetime | None = None,
        opened_cycle: int | None = None,
        opened_cadence: str | None = None,
        target_signed_quantities: dict[str, float] | None = None,
        execution_ts_by_symbol: dict[str, datetime] | None = None,
        execution_completed_by_symbol: dict[str, bool] | None = None,
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
        carries no fill price/qty. Production execution may pass ``target_signed_quantities``
        after lot-step, minimum-notional, and available-depth treatment. Those quantities are an
        execution result of the PM's requested order, not a new trading decision. Per-symbol fill
        timestamps preserve the observation/execution sequence in the resulting position state.
        An explicit execution target that reduces, drops, flips, or transfers an existing
        lifecycle also requires ``execution_completed_by_symbol[symbol]=True``. Without full-fill
        proof, a single Position cannot truthfully carry both the unfilled legacy sleeve and the
        new target thesis/role, so the whole application fails before any account mutation.

        Convergent across weeks (weekly re-emits the full book -> delta 0 on unchanged legs) and
        correct for daily (each rebalance_trades leg carries that symbol's NEW target_notional)."""
        default_ts = opened_ts or datetime.now(tz=UTC)
        # Consolidate by symbol into a single NET signed target_notional, preserving first-seen
        # order (so a single-leg book is processed exactly as before — an exact no-op on a re-send).
        net_signed_notional: dict[str, float] = {}
        seat_role_by_symbol: dict[str, Literal["alpha", "hedge"]] = {}
        thesis_by_symbol: dict[str, dict] = {}
        for trade in executed_trades:
            sym = trade["symbol"]
            direction: Direction = trade["direction"]
            target_notional = abs(float(trade["target_notional"]))
            leg_sign = 1.0 if direction == "long" else -1.0
            net_signed_notional[sym] = (
                net_signed_notional.get(sym, 0.0) + leg_sign * target_notional
            )
            role = str(trade.get("seat_role") or "alpha")
            if role not in {"alpha", "hedge"}:
                raise ValueError(f"invalid seat_role for {sym}: {role}")
            # Any alpha component makes a consolidated symbol alpha; a hedge label may never hide
            # directional risk in another same-symbol leg.
            if role == "alpha" or sym not in seat_role_by_symbol:
                seat_role_by_symbol[sym] = role
                thesis_fields = {
                    "thesis_cycle",
                    "thesis_book_sha256",
                    "expected_price_edge_frac",
                    "edge_horizon_hours",
                    "edge_calibration_basis",
                    "invalidation_condition",
                }
                if any(field in trade for field in thesis_fields):
                    thesis_by_symbol[sym] = {field: trade.get(field) for field in thesis_fields}
        # CLOSE any currently-HELD symbol ABSENT from the new (full intended) book — a name dropped
        # at reselection/rebalance must be FLATTENED (realizing its PnL + a close fee/slippage), not
        # left lingering. Without this the dropped leg silently survives and breaks neutrality. We
        # synthesize a target-0 leg for it so it routes the SAME consolidated reconcile/close path
        # as an explicit zero-target leg (no special-casing). Snapshot the keys first — the
        # reconcile loop below mutates `positions`.
        dropped = [sym for sym in self.positions if sym not in net_signed_notional]
        for sym in dropped:
            net_signed_notional[sym] = 0.0
        if target_signed_quantities is not None:
            if set(target_signed_quantities) != set(net_signed_notional):
                raise ValueError(
                    "execution target quantities must cover the exact consolidated book"
                )
            if any(not math.isfinite(float(qty)) for qty in target_signed_quantities.values()):
                raise ValueError("execution target quantities must be finite")
            for sym, net_notional in net_signed_notional.items():
                existing = self.positions.get(sym)
                mark = marks.get(sym)
                if existing is None or mark is None or mark <= 0.0:
                    continue
                desired_target = net_notional / mark
                current_target = _signed_qty(existing)
                target_role = seat_role_by_symbol.get(sym, existing.seat_role)
                lifecycle_trade = bool(
                    not math.isclose(desired_target, current_target, abs_tol=1e-12)
                    and (
                        desired_target * current_target <= 0.0
                        or abs(desired_target) < abs(current_target) - 1e-12
                        or target_role != existing.seat_role
                    )
                )
                if lifecycle_trade and not bool(
                    (execution_completed_by_symbol or {}).get(sym, False)
                ):
                    raise ValueError(
                        f"apply_fills: {sym} lifecycle-changing execution lacks full-fill proof"
                    )
        for sym, net_notional in net_signed_notional.items():
            existing = self.positions.get(sym)
            target_seat_role = seat_role_by_symbol.get(
                sym, existing.seat_role if existing is not None else "alpha"
            )
            thesis = thesis_by_symbol.get(sym, {})
            mark_raw = marks.get(sym)
            try:
                mark = float(mark_raw) if mark_raw is not None else None
            except (TypeError, ValueError):
                mark = None
            if mark is None or not math.isfinite(mark) or mark <= 0.0:
                # A held symbol with no valid mark cannot be valued or truthfully reconciled, and a
                # new target cannot be sized. Skipping either would silently execute a different
                # book. HALT; the prior PAPER generation remains standing.
                if existing is not None or abs(net_notional) > 1e-9:
                    raise ValueError(
                        f"apply_fills: {sym} has no finite positive mark — cannot reconcile "
                        "truthfully (HALT rather than execute a different PAPER book)"
                    )
                continue
            desired_direction = "long" if net_notional >= 0.0 else "short"
            desired_sign = 1.0 if desired_direction == "long" else -1.0
            desired_target_signed_qty = desired_sign * (abs(net_notional) / mark)
            target_signed_qty = (
                float(target_signed_quantities[sym])
                if target_signed_quantities is not None
                else desired_target_signed_qty
            )
            if target_signed_qty > 0.0:
                direction = "long"
            elif target_signed_qty < 0.0:
                direction = "short"
            else:
                direction = existing.direction if existing is not None else desired_direction
            # A partially executed flip/drop may still leave old-side inventory. It remains the old
            # lifecycle until execution actually reaches zero or crosses to the PM's requested side.
            if (
                existing is not None
                and not math.isclose(target_signed_qty, 0.0, abs_tol=1e-12)
                and (
                    math.isclose(desired_target_signed_qty, 0.0, abs_tol=1e-12)
                    or desired_target_signed_qty * target_signed_qty < 0.0
                )
            ):
                target_seat_role = existing.seat_role
                thesis = {}
            role_change = bool(
                existing is not None
                and existing.direction == direction
                and existing.seat_role != target_seat_role
            )
            ci = costs.get(sym) or CostInputs()
            current_signed_qty = _signed_qty(existing)
            ts = (execution_ts_by_symbol or {}).get(sym, default_ts)
            role_change_reduction = bool(
                role_change and abs(target_signed_qty) < abs(current_signed_qty)
            )
            # Transfer retained inventory before a no-op/increase, but close a reduced slice under
            # the role that actually owned it and transfer only the surviving residual afterward.
            if role_change and not role_change_reduction:
                self._roll_seat_lifecycle(
                    existing,
                    mark=mark,
                    ts=ts,
                    opened_cycle=opened_cycle,
                    opened_cadence=opened_cadence,
                    target_seat_role=target_seat_role,
                    thesis=thesis,
                )
            delta_signed_qty = target_signed_qty - current_signed_qty
            if math.isclose(
                target_signed_qty,
                current_signed_qty,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                if existing is not None:
                    existing.seat_role = target_seat_role
                    self._assign_thesis(existing, thesis)
                continue  # already at target -> no-op (re-sent unchanged book)
            delta_notional = abs(delta_signed_qty) * mark
            if ci.depth is not None:
                side = ci.depth  # pre-selected (back-compat)
            elif delta_signed_qty > 0:
                side = ci.depth_asks or None  # BUY crosses the ASKS
            else:
                side = ci.depth_bids or None  # SELL crosses the BIDS
            slip = estimate_slippage(
                sym,
                abs(delta_signed_qty),
                mark,
                depth=side,
                adv_usd=ci.adv_usd,
                half_spread_bps=ci.half_spread_bps,
                adverse_selection_bps=ci.adverse_selection_bps,
                legging_bps=ci.legging_bps,
            )
            # Binance taker fees apply to the quote value actually crossed, not the midpoint
            # reference used for PnL. For a buy, implementation shortfall raises quote spent; for
            # a sell it lowers quote received. This keeps the fee base consistent with walked VWAP
            # plus the explicit adverse-selection/legging reserve.
            execution_quote_notional = delta_notional + math.copysign(
                slip, delta_signed_qty
            )
            if not math.isfinite(execution_quote_notional) or execution_quote_notional <= 0.0:
                raise ValueError(f"apply_fills: invalid execution quote notional for {sym}")
            fee = trade_fee(execution_quote_notional, maker=ci.maker)

            if existing is not None and (
                target_signed_qty * current_signed_qty < 0
                or abs(target_signed_qty) < abs(current_signed_qty)
            ):
                # NOT a pure same-side increase -> reduce/close/FLIP (Task 4). A FLIP
                # (opposite signs) makes the delta overshoot past zero, so the old
                # `delta_signed_qty * sign < 0` predicate came out POSITIVE and let a
                # short leg silently grow a long position; route every non-increase here.
                self._reconcile_opposite(
                    existing,
                    sym,
                    direction,
                    target_signed_qty,
                    mark,
                    fee,
                    slip,
                    ts,
                    opened_cycle=opened_cycle,
                    opened_cadence=opened_cadence,
                    target_seat_role=(
                        existing.seat_role if role_change_reduction else target_seat_role
                    ),
                    thesis={} if role_change_reduction else thesis,
                )
                if role_change_reduction and sym in self.positions:
                    self._roll_seat_lifecycle(
                        self.positions[sym],
                        mark=mark,
                        ts=ts,
                        opened_cycle=opened_cycle,
                        opened_cadence=opened_cadence,
                        target_seat_role=target_seat_role,
                        thesis=thesis,
                    )
                continue

            # same-side open/increase: fill |delta| at the mark, blend entry VWAP.
            fill_qty = abs(delta_signed_qty)
            self._charge_frictions(sym, fee, slip, existing)
            if existing is None:
                self.positions[sym] = Position(
                    symbol=sym,
                    direction=direction,
                    qty=fill_qty,
                    entry_price=mark,
                    opened_ts=ts,
                    opened_cycle=opened_cycle,
                    opened_cadence=opened_cadence,
                    seat_role=target_seat_role,
                    accrued_fees=fee,
                    accrued_slippage=slip,
                )
                self._assign_thesis(self.positions[sym], thesis)
            else:
                existing.seat_role = target_seat_role
                total_qty = existing.qty + fill_qty
                existing.entry_price = (
                    existing.entry_price * existing.qty + mark * fill_qty
                ) / total_qty
                existing.qty = total_qty
                self._assign_thesis(existing, thesis)

    def _charge_frictions(self, sym: str, fee: float, slip: float, pos: Position | None) -> None:
        self.cash -= fee + slip
        self.fees_paid += fee
        self.slippage_paid += slip
        if pos is not None:
            pos.accrued_fees += fee
            pos.accrued_slippage += slip

    def _reconcile_opposite(
        self,
        existing: Position,
        sym: str,
        direction: Direction,
        target_signed_qty: float,
        mark: float,
        fee: float,
        slip: float,
        ts: datetime,
        *,
        opened_cycle: int | None = None,
        opened_cadence: str | None = None,
        target_seat_role: Literal["alpha", "hedge"] = "alpha",
        thesis: dict | None = None,
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
        new_open_qty = (
            abs(target_signed_qty) if (target_signed_qty * current_signed_qty < 0) else 0.0
        )
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
            existing.seat_role = target_seat_role
            self._assign_thesis(existing, thesis or {})
            return
        # fully closed this side -> snapshot its realized outcome (for the journal patch), then pop.
        self.closed_legs.append(
            ClosedLeg(
                symbol=existing.symbol,
                direction=existing.direction,
                opened_cycle=existing.opened_cycle,
                opened_cadence=existing.opened_cadence,
                seat_role=existing.seat_role,
                thesis_cycle=existing.thesis_cycle,
                thesis_book_sha256=existing.thesis_book_sha256,
                expected_price_edge_frac=existing.expected_price_edge_frac,
                edge_horizon_hours=existing.edge_horizon_hours,
                edge_calibration_basis=existing.edge_calibration_basis,
                invalidation_condition=existing.invalidation_condition,
                fees=existing.accrued_fees,
                slippage=existing.accrued_slippage,
                realized_funding=existing.accrued_funding,
                realized_pnl=existing.realized_pnl,
            )
        )
        self.positions.pop(sym, None)
        residual_new_qty = abs(target_signed_qty)
        if residual_new_qty > 1e-12:  # FLIP: open to reach the opposite-side target
            new_pos = Position(
                symbol=sym,
                direction=direction,
                qty=residual_new_qty,
                entry_price=mark,
                opened_ts=ts,
                opened_cycle=opened_cycle,
                opened_cadence=opened_cadence,
                seat_role=target_seat_role,
                accrued_fees=0.0,
                accrued_slippage=0.0,
            )
            self.positions[sym] = new_pos
            self._assign_thesis(new_pos, thesis or {})
            # the NEW leg's share of the flip frictions bills the NEW decision (see docstring)
            self._charge_frictions(sym, new_fee, new_slip, new_pos)
        elif new_fee or new_slip:
            # target went exactly to zero but a new-open share was computed — charge it to the
            # account (no new position to carry it); keeps cash identical to the old behavior.
            self._charge_frictions(sym, new_fee, new_slip, None)


def _account_path(state_dir) -> Path:
    return Path(state_dir) / "account.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """Power-loss-durable replacement used by legacy/direct account callers."""
    durable_write_text(path, text)


def load_account(state_dir, default_cash: float) -> PaperAccount:
    """Load the single account.json at the state root, or init a fresh account at `default_cash`
    (zero positions, no funding clock) on a clean state dir — the restart-from-scratch path."""
    p = _account_path(state_dir)
    if p.exists():
        return PaperAccount.from_dict(json.loads(p.read_text()))
    return PaperAccount(cash=default_cash)


def save_account(state_dir, account: PaperAccount) -> None:
    _atomic_write_text(_account_path(state_dir), json.dumps(account.to_dict(), indent=2))
