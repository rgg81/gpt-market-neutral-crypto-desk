# AGENTS.md — Weekly Cross-Section PAPER Desk

This repository is a paper-only crypto-futures desk. Before a cycle, read `MISSION.md` and
`docs/desk-cycle-runbook.md`; the runbook is the exact production sequence.

## Runtime

- GPT only: root and every subagent use `gpt-5.6-sol` at `xhigh`, inherited from the launcher.
- ChatGPT/Codex login only. Never use `OPENAI_API_KEY` or a raw API runner.
- PAPER ONLY. `live` remains exactly `false`; never add or call order-placement code.
- State stays in `live_state/`; cycle memory stays in `live_memory/pending/<cycle>/`.

## Decision boundary

- Python deterministically selects the active Binance USDT perpetual Top 50 by cumulative quote
  volume over 180 completed UTC days.
- Python deterministically ranks that Top 50 by seven-day long total return: price return minus
  actual funding, using each funding event's settlement mark.
- The ten best are always long and the ten worst are always short for the frozen ISO week.
- Agents may decide weights only. They cannot add, remove, flip, hedge, or leave a selected name in
  cash. Each sleeve must sum to 1.0 and the target portfolio is dollar neutral at 1x gross.
- Spawn Alpha Allocator and Risk Allocator concurrently. After both validate, spawn the Weight PM.
  Then run one Weight Adversary. A rejection permits exactly one PM revision and no second review.
- Do not run sentiment, web research, technical/futures specialist, or Reflector agents in this
  design. The compact numeric packet is the complete decision input.

## Data and execution

- All candles come only through `http://127.0.0.1:8000` (`~/binance-proxy`). Missing, stale, or
  incomplete required candles halt before agents. The launcher may repair only that exact proxy.
- Weekly selection requires complete price, quote-volume, and funding coverage. Never silently
  substitute zero or omit an otherwise eligible market after a fetch failure.
- Weight changes use fresh two-sided L2 books, exchange filters, lot rounding, realistic fees,
  displayed-depth haircuts, adverse-selection reserve, and legging reserve.
- Decision-to-execution price movement is drift, not slippage. There is no forecast-payback/B12
  gate: weekly cross-sectional rank supplies direction and agents supply weights.
- The whole 20-name basket must remain substantially deployed and dollar neutral after simulated
  execution. Any integrity or execution failure leaves the prior completed PAPER book unchanged.
- Reconciliation and funding settlement use the existing durable transaction protocol. Never
  hand-edit `live_state/`.

## Verification

After repairs, run `uv run pytest` and `uv run ruff check .`. Do not edit the scheduler or crontab
from inside a cycle.
