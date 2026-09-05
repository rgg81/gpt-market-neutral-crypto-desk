# GPT Market-Neutral Crypto Desk

A paper-trading research desk for Binance USD-M perpetual futures, operated by a team of GPT
agents through Codex.

Three specialists study the same liquid crypto universe in parallel:

- **Sentiment** opens recent news and community sources.
- **Technical** ranks multi-horizon raw and BTC-beta-adjusted momentum, acceleration, drawdown,
  and volatility.
- **Futures** evaluates funding, basis, open interest, and positioning.

A **Portfolio Manager** constructs the long/short book, an **Adversary** fact-checks and challenges
it, and a **Reflector** calibrates agent prompts from measured forward outcomes. Deterministic
Python code collects market evidence, validates decision provenance, simulates execution, and
maintains the account ledger.

> [!WARNING]
> This project is **paper trading only**. `live` is constrained to `false`, there is no
> order-placement path, and the exchange integration uses public market-data endpoints. It is
> research software, not financial advice.

## What makes this desk different

The central rule is:

> **LLM agents propose; deterministic code records and verifies provenance.**

The Python layer does not rank trades or override the agents. It does enforce structural integrity:

- the proposed book is bound to the exact evidence snapshot the agents received;
- a deterministic precheck publishes twelve risk and honesty checks to the Adversary;
- the Adversary must echo the metrics, rule on B1–B12, and bind its verdict to the precheck hash;
- every non-flat sentiment URL receives a persisted citation audit;
- accepted selected legs cannot rely on a citation the Adversary marked unsupported;
- unchanged target quantities remain no-ops even when execution prices move during reasoning;
- decision-to-execution price movement is recorded as drift, not mislabeled as slippage;
- every execution persists the exact validated raw and effective L2 ladders used to replay VWAP
  and slippage; malformed, duplicate, unordered, locked, or crossed books fail closed;
- fees, order-book slippage, funding, realized P&L, and unrealized P&L reconcile in one ledger;
- every GPT role receives a cycle-matched performance packet with desk PnL/drawdown, frictions,
  seat economics, and its own measured calibration history;
- every incumbent alpha seat must requalify each cycle with a calibrated beta-adjusted price edge,
  objective invalidation, risk review, and hold-versus-cash/replacement comparison;
- persistent relative-price alpha is the anchor; history-qualified carry is an overlay that may
  lead only in verified chop, while BTC is the only permitted hedge-labeled seat;
- alpha gross, BTC-hedge gross, alpha beta before hedge, hedge efficiency, same-side positive
  residual-correlation clusters, and full signed-position co-risk clusters are reported separately
  so a hedge cannot manufacture economic deployment or hide offset-side concentration;
- new alpha forecasts use only committed-mark horizons `24`, `72`, or `168` hours, with
  horizon-matched calibration and objective invalidation bound through any sole PM revision;
- changed-slice break-even caps price forecasts at their declared horizon, requires fully covered
  two-sided depth points, and compares hedge changes with the carried BTC counterfactual;
- every 1h momentum and 1d beta candle comes exclusively through the local caching Binance proxy,
  with the currently-forming candle and per-symbol provenance required before agents run;
- the current-forming tail is freshness/live-mark evidence only; completed bars drive declared
  momentum horizons, beta, covariance, and volatility;
- last-settled funding is never called expected carry; conservative carry requires sufficiently
  recent and complete 24h/72h/168h history, while positioning uses contract OI and own-history
  normalized long/short ratios;
- funding after an outage uses each historical boundary's actual rate and settlement mark, and
  halts on incomplete history instead of multiplying the latest rate across missed events;
- self-learning edits are restricted to fenced prompt regions and must match a versioned latest
  head that binds the local reflector journal, source cycle, validated proposal, sealed surfaced
  recurrences, an exclusive state authority receipt, and an independent monotonic state anchor.

## Architecture

```text
                       GPT reasoning layer

               ┌──────── Sentiment + web search
Evidence pack ─┼──────── Technical
               └──────── Futures / funding / OI
                              │
                              ▼
                    Portfolio Manager
                              │
                    deterministic precheck
                              │
                              ▼
               Adversary + citation fact-check
                              │
                     accept / one revision
                              │
                              ▼
                     paper reconciliation

                       Python control layer

universe → evidence → score → performance → reflection → precheck → execution audit → account/ledger
```

The decision team is pinned to `gpt-5.6-sol` at `xhigh` reasoning effort in the supplied launcher.
Every specialist, PM, Adversary, and due Reflector inherits the root model and effort.

## One daily decision cycle

The full GPT desk fires once daily at **00:07 UTC**. Token-free funding and portfolio heartbeats
run at **08:07 and 16:07 UTC** without starting Codex or changing positions:

1. **Data preflight** — require a healthy `~/binance-proxy` and fresh current 1h/1d candles.
2. **Provenance check** — reject any active reflector region that differs from its exact v1 head.
3. **Watchdog** — stand down if the previous completed cycle is too recent.
4. **Evidence** — scan the top 40 by quote volume, apply quality/liquidity gates, and retain up to
   20 candidates plus required held symbols.
5. **Score and performance** — score only scheduled-horizon outcomes for learning, retain late
   labels for audit, exclude overlapping unchanged renewals, then build the packet every role reads.
6. **Reflect** — tune or retire performance calibration only after a documented recurrence.
7. **Specialists** — run Sentiment, Technical, and Futures concurrently.
8. **Portfolio Manager** — build a price-alpha-first, approximately dollar- and beta-neutral book;
   every incumbent re-earns its seat, and defensive underdeployment is preferable to weak alpha.
9. **Precheck** — compute alpha/hedge deployment, neutrality, residual clusters, full-book expected
   economics, turnover, liquidity, and both carry-only and forecast-inclusive payback diagnostics;
   fixed decision-mark quantities are valued at the L2 midpoint before depth-tier and friction
   calculations.
10. **Adversary** — audit every continuation and forecast calibration, residual-risk economics,
   arithmetic, trading rules, and all non-flat sentiment citations.
11. **Reconcile** — freeze target quantities at decision marks, capture a fresh two-sided execution
   book, simulate only the quantity delta, settle funding, and publish one durable generation with
   `complete.json` last.

The proxy is managed at `http://127.0.0.1:8000`. Before claiming a scheduled slot, the launcher
checks it and may start/restart only the exact `~/binance-proxy` Uvicorn app after repeated health
failures. `desk_evidence.py` then rechecks every required series immediately before writing the
cycle snapshot. Proxy failure or stale candles leave the previous PAPER book standing.

The complete operating contract is in
[`docs/desk-cycle-runbook.md`](docs/desk-cycle-runbook.md).

## Safety and risk checks

The precheck exposes these bounds to the Adversary:

| ID | Check |
|---|---|
| B1 | Gross deployment is within the allowed cash band |
| B2 | Dollar residual is bounded |
| B3 | BTC-beta residual is bounded |
| B4 | Single-leg concentration is bounded |
| B5 | BTC hedge size is bounded |
| B6 | Per-leg beta-dollar exposure is bounded |
| B7 | PM-stated metrics match recomputed metrics |
| B8 | Turnover fields describe the proposed change truthfully |
| B9 | New entries, flips, and same-side increases stay within the aggressive-change limit; every resize is reported/costed, while exits and decreases remain uncapped loss control |
| B10 | Priced selected seats/exits remain ≤75bp; aggressive alpha actions also require a complete ≤50bp 2k screen |
| B11 | No duplicate or unpriced legs |
| B12 | Explicit price-plus-conservative-carry edge repays size-aware entry/flip/resize/drop friction promptly; carry-only and required-forecast diagnostics expose self-justifying forecasts |

These checks are evidence for the GPT Adversary, which remains the sole decision veto. A rejection
must issue machine-checkable constraints for the single PM revision. Reconcile proves the revision
obeys the exhaustive typed symbol-mutation envelope, selects no sentiment the Adversary marked
unsupported, and permits only the exact B1/B9/B12 failures explicitly authorized; every other
failing bound halts before any paper fill.

## Requirements

- Linux or macOS with Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for the locked Python environment
- Codex CLI authenticated with a ChatGPT subscription
- Cron and `flock` for the supplied persistent scheduler
- Public internet access to Binance market data and web search during a live paper cycle

No exchange credentials or raw LLM API keys are required.

## Installation

```bash
git clone https://github.com/rgg81/gpt-market-neutral-crypto-desk.git
cd gpt-market-neutral-crypto-desk

uv sync --frozen
uv run pytest
uv run ruff check .
```

Verify the Codex launcher without opening a cycle:

```bash
bash scripts/run_scheduled_cycle.sh --check
```

The launcher resolves the repository, `codex`, and `uv` dynamically. Non-standard binary
locations can be supplied through `CODEX_BIN_OVERRIDE` and `UV_BIN_OVERRIDE`.

## Running a paper cycle

Read [`AGENTS.md`](AGENTS.md), [`MISSION.md`](MISSION.md), and the
[cycle runbook](docs/desk-cycle-runbook.md) first.

The simplest entry point is:

```bash
bash scripts/run_scheduled_cycle.sh
```

This invokes Codex with multi-agent support and the repository's cycle prompt. The watchdog may
stand down instead of opening a cycle when the last completed cycle is too recent.

To inspect the deterministic stages independently:

```bash
uv run python scripts/desk_evidence.py --state-dir live_state --memory-dir live_memory
uv run python scripts/desk_score.py --state-dir live_state --memory-dir live_memory
uv run python scripts/desk_performance.py --state-dir live_state --memory-dir live_memory
uv run python scripts/desk_precheck.py --state-dir live_state --memory-dir live_memory
uv run python scripts/desk_reconcile.py --state-dir live_state --memory-dir live_memory
```

Do not run reconcile without valid specialist, PM, precheck, and Adversary artifacts in the active
per-cycle pending directory; decision-chain validation will refuse them.

The older `futures_fund.desk_cycle.run_cycle` and `scripts/run_desk_cli.py` interfaces exist only
for canned offline integration tests. They require an explicit acknowledgement and a runner-bound
opaque capability issued only for the exact `StubAgentRunner`; the checked-in CLI runner factory
always fails. Neither interface is referenced by the scheduler or production cycle prompt.

To watch the lightweight portfolio marks between GPT decisions:

```bash
tail -f logs/desk-heartbeat.log
tail -n 5 live_state/portfolio-heartbeats.jsonl
```

For a token-free, read-only operational report:

```bash
bash scripts/run_scheduled_cycle.sh --probe-only
uv run python scripts/desk_health.py --state-dir live_state --log-dir logs
```

The report takes a state-transaction-consistent snapshot and checks completed-cycle and heartbeat
age, manifest/account consistency, unified account/heartbeat chains, funding-clock age, continuous
flat-book duration, and the proxy's current HTTP + exact managed-PID/listener identity. The older
manager receipt is retained as separate audit context. Local deduplicated alerts require no
credentials and send nothing externally by default:

```bash
uv run python scripts/desk_health_alert.py --state-dir live_state --log-dir logs
```

An operator can explicitly supply `--alert-command '/path/to/program args'`; it runs without a
shell and receives report JSON on stdin. Durable generations use file and directory `fsync`,
atomic replace, immutable account snapshots, and account-event-anchored generation roots that bind
exact artifact membership plus ledger/equity state. Runtime fingerprints cover Git/dirty source,
config, lock/build inputs, prompts, Python, and proxy build/config.

## Installing the daily decision schedule

Preview the managed crontab block:

```bash
uv run python scripts/install_desk_cron.py --print
```

Install or update it:

```bash
uv run python scripts/install_desk_cron.py --install
uv run python scripts/install_desk_cron.py --check
```

The managed cron polls the launchers every ten minutes. A UTC-aware gate accepts the daily GPT
slot and intervening heartbeat slots for up to six hours. Full GPT cycles retain one attempt claim;
token-free heartbeats use a durable idempotent account+audit commit and release a failed exact-slot
claim for safe retry. This survives delayed cron dispatch, host clock corrections, and short
internet outages without duplicate GPT cycles. Network and runtime preflights happen before any
claim, so a failed preflight remains retryable without consuming an agent cycle.

Optional non-privileged systemd service/timer templates live in `ops/systemd-user/`. They are not
installed automatically. Use them instead of cron and review the restart/HA guidance in
[`docs/desk-restart-runbook.md`](docs/desk-restart-runbook.md); never run two writable schedulers.

## Configuration

[`config.yaml`](config.yaml) contains deterministic plumbing settings:

- paper account size;
- top-volume scan and post-gate universe sizes;
- minimum average daily volume;
- 24-hour full-GPT cadence;
- token-free 8-hour funding/portfolio heartbeats;
- BTC reference symbol;
- rolling beta lookback.

Trading decisions and sizing remain agent-owned. `live: false` is a permanent project invariant.

## Local runtime data

The following are intentionally ignored by Git:

```text
live_state/       paper account, reports, execution audits, and ledger
live_memory/      pending outputs, scorecard, reflector journal, and latest-head trust anchor
logs/             scheduler and Codex output
.venv/            local Python environment
.uv-cache/        local uv cache
```

This keeps account history, agent deliberation artifacts, machine paths, and potentially sensitive
local directives out of public commits.

## Repository map

```text
agents/           GPT role prompts
futures_fund/     deterministic evidence, integrity, execution, and accounting modules
scripts/          cycle CLIs, scheduler launcher, and cron installer
tests/            offline tests with stubbed agent paths
ops/              the autonomous Codex cycle prompt
docs/             runbooks, forensic review, and operational notes
AGENTS.md         non-negotiable operating rules
MISSION.md        desk charter
SKILL.md          one-cycle orchestration skill
config.yaml       paper-desk configuration
uv.lock           reproducible dependency lock
```

The forensic rationale for the hardened version is documented in
[`docs/v2-review.md`](docs/v2-review.md).

## Development

```bash
uv run pytest
uv run ruff check .
bash -n scripts/run_scheduled_cycle.sh
```

The test suite is offline: it uses deterministic fixtures and stubbed agent outputs, and does not
place orders or call a live LLM.

## Limitations

- This is a live-market paper experiment, not evidence of future profitability.
- LLM judgments and web sources can be wrong or unavailable; the citation and Adversary layers
  reduce that risk but cannot eliminate it.
- Paper fills approximate execution from visible order-book depth and configured fees.
- Results depend on model availability, market-data quality, and the machine running the schedule.
- The public repository does not include the author's live paper account or historical run logs.

## Disclaimer

For research and educational use only. Nothing in this repository is investment, legal, or tax
advice. Crypto derivatives are high risk. Do not connect this project to real capital.

## License

Licensed under the [Apache License 2.0](LICENSE). See [`NOTICE`](NOTICE) for attribution.

---

Built with Codex, GPT agents, Python, and an unreasonable affection for audit trails.
