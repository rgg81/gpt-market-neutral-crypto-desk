# GPT Weekly Cross-Section Crypto Desk

A paper-only, dollar-neutral Binance USD-M perpetual-futures desk. Symbol selection is
deterministic; GPT agents reason only about portfolio weights.

> This is research software, not financial advice. `live` is structurally fixed to `false`, no
> exchange credentials are used, and the code has no order-placement path.

## Strategy

Once per ISO week the desk:

1. finds active crypto-only USDT perpetuals with a complete six-month history;
2. ranks them by cumulative quote volume over 180 completed UTC days;
3. keeps the 50 most traded;
4. calculates seven-day long total return from price and every realized funding settlement;
5. fixes the best 10 as longs and worst 10 as shorts for the week.

The portfolio targets 1x gross: 50% of equity long and 50% short. All 20 selected names must be
held. A daily GPT ensemble changes only within-sleeve weights:

```text
compact numeric packet ─┬─ Alpha Allocator ─┐
                        └─ Risk Allocator  ─┤
                                            ▼
                                      Weight PM
                                            ▼
                                    Weight Adversary
                                            ▼
                              fresh L2 PAPER reconciliation
```

The daily packet contains weekly ranks/returns, 24h/72h/168h moves, realized volatility, current
funding, compact position-PnL correlations, current weights/PnL, turnover, costs, drawdown, and desk
performance. There is no web search, sentiment analysis, prompt reflection, trade-discovery debate,
or self-authored forecast-payback gate.

## Data and realism

- Every candle is fetched through the local `~/binance-proxy`; stale or incomplete data halts.
- Weekly volume uses Binance quote-asset volume, not a close-times-base-volume approximation.
- Weekly funding uses every event's actual rate and settlement mark.
- PAPER execution uses fresh two-sided L2, lot/minimum rules, taker fees, depth-aware slippage,
  displayed-depth haircut, adverse-selection reserve, and legging reserve.
- Decision-to-execution movement is recorded as drift, not charged as slippage.
- Funding, fills, ledger, account, and all decision artifacts publish through a replayable durable
  transaction with `complete.json` written last.

## Runtime

Production orchestration uses Codex subscription authentication with `gpt-5.6-sol` at `xhigh` for
the root and all four roles. The full weight review runs daily at 00:07 UTC. Token-free heartbeats
mark the unchanged book and settle funding at 08:07 and 16:07 UTC. The first admitted cycle of a
new ISO week refreshes the Top 50 automatically.

Requirements: Linux, Python 3.11+, `uv`, Codex CLI login, cron, and the local Binance proxy.

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
bash scripts/run_scheduled_cycle.sh --probe-only
```

Run one eligible PAPER cycle:

```bash
bash scripts/run_scheduled_cycle.sh
```

The production stages are implemented by `desk_cross_section_prepare.py`,
`desk_cross_section_validate.py`, and `desk_cross_section_reconcile.py` under `scripts/`.

The exact role and command sequence is in
[`docs/desk-cycle-runbook.md`](docs/desk-cycle-runbook.md).

Token-free status and performance:

```bash
uv run python scripts/desk_status_performance.py
tail -f logs/desk-cycle.log
```

## Safety invariants

- fixed weekly symbols and sides;
- exactly 10 longs and 10 shorts;
- each sleeve sums to 100%;
- configured per-name sleeve weight bounds;
- target dollar neutrality and 1x gross;
- achieved 85–115% gross, at most 2% dollar residual, and at most 11% of gross per name;
- fresh one-way estimated slippage at most 50bp;
- one Adversary review and at most one PM revision;
- any unresolved failure leaves the prior completed PAPER book standing.

Licensed under Apache-2.0. See [LICENSE](LICENSE).
