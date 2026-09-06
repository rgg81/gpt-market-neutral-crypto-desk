# Desk Cycle Runbook — Weekly Selection, Daily Weights

This is the exact production sequence for one PAPER cycle. The launcher pins the root and every
subagent to `gpt-5.6-sol` at `xhigh` using Codex subscription authentication.

## 0. Preflight and cadence

Run sequentially:

```bash
uv run python scripts/ensure_binance_proxy.py
uv run python scripts/desk_data_preflight.py
uv run python scripts/desk_recover.py --state-dir live_state
uv run python scripts/desk_watchdog.py --state-dir live_state
```

An `EARLY` watchdog result is an immediate stand-down. Any other command failure halts with the
prior completed book standing. Never open more than one cycle per invocation.

## 1. Deterministic selection and weight packet

```bash
uv run python scripts/desk_cross_section_prepare.py \
  --state-dir live_state --memory-dir live_memory
```

On the first admitted cycle of an ISO week, this command:

- enumerates active crypto-only Binance USDT perpetuals old enough for complete history;
- fetches 182 daily rows per eligible market only through `~/binance-proxy`;
- ranks the Top 50 by quote volume across 180 completed UTC days;
- fetches and coverage-checks every Top-50 funding event over seven days;
- freezes the best ten funding-adjusted returns as longs and worst ten as shorts.

Later cycles in that ISO week load the immutable snapshot. Every daily cycle fetches 170 fresh 1h
candles and current funding/marks for the 20 selected names plus held names due to exit. It writes
a compact `weight_packet.json`, `market_state.json`, `weekly_universe.json`, `meta.json`, and sealed
runtime provenance under `live_memory/pending/<cycle>/`.

## 2. Independent allocators — parallel

Spawn exactly two real agents concurrently, inheriting the root model and effort:

| Role | Prompt | Output |
|---|---|---|
| Alpha Allocator | `agents/allocator-alpha.md` | `allocator_alpha.json` |
| Risk Allocator | `agents/allocator-risk.md` | `allocator_risk.json` |

Wait for both agents, then validate:

```bash
uv run python scripts/desk_cross_section_validate.py alpha
uv run python scripts/desk_cross_section_validate.py risk
uv run python scripts/desk_cross_section_validate.py consensus-input
```

A malformed allocator gets one retry. A second failure halts; do not replace it with equal weights
or impersonate it. Neither agent may browse the web. Each must cover all 20 fixed symbol/sides,
with each sleeve summing to exactly 1.0 inside the packet's weight bounds.

## 3. Weight PM

Spawn one real agent with `agents/weight-pm.md`. It reads the packet, both proposals, and their
deterministic digest, then writes `pm_weights.json`. Validate and build the precheck:

```bash
uv run python scripts/desk_cross_section_validate.py pm
uv run python scripts/desk_cross_section_validate.py precheck
```

Malformed PM output gets one retry. The PM may resolve weights only—never symbols, sides, gross,
or cash deployment.

## 4. Weight Adversary and sole revision

Spawn one real agent with `agents/weight-adversary.md`; it writes
`allocation_adversary.json`. Then run:

```bash
uv run python scripts/desk_cross_section_validate.py adversary
```

Exit 0 means accepted. Exit 2 means rejected with structured constraints. On rejection, spawn
exactly one PM revision using `agents/weight-pm-revision.md`, which writes
`pm_weights_revision.json`. There is no second adversarial pass. Finalize either path:

```bash
uv run python scripts/desk_cross_section_validate.py finalize
```

Malformed Adversary output gets one retry. A malformed or constraint-violating sole revision
halts. Deterministic validation enforces the agents' exact symbol/side and weight contracts; it
does not choose a replacement weight.

## 5. Fresh PAPER execution and durable commit

```bash
uv run python scripts/desk_cross_section_reconcile.py \
  --state-dir live_state --memory-dir live_memory
```

Reconcile revalidates every hash and recaptures the sealed source/prompt build. It rejects a weight
decision older than 90 minutes. It then captures fresh exchange filters and two-sided L2 for every
changed name, applies the configured displayed-depth/latency/adverse-selection/legging mechanics,
and requires one-way slippage at or below 50bp. Market drift since the decision is recorded but is
not slippage and does not invalidate the weekly rank.

The simulated achieved book must contain the exact 20 frozen names and sides, remain 85–115%
gross deployed, have at most 2% dollar residual after lot rounding, and no name above 11% of gross.
Historical funding is settled before fills. The existing write-ahead transaction publishes the
account, artifacts, ledger, and equity row, then `complete.json` last. Any failure before commit
leaves the prior PAPER account unchanged.

## 6. Operator output

Print the reconcile JSON and then:

```bash
uv run python scripts/desk_status_performance.py
```

The token-free heartbeats at 08:07 and 16:07 UTC continue to mark positions and settle funding;
they never change weights. The daily GPT cycle remains at 00:07 UTC. Weekly selection refreshes
automatically on the first admitted ISO-week cycle—there is no separate weekly cron job.
