# GPT Market-Neutral Crypto Desk

A paper-trading research desk for Binance USD-M perpetual futures, operated by a team of GPT
agents through Codex.

Three specialists study the same liquid crypto universe in parallel:

- **Sentiment** opens recent news and community sources.
- **Technical** ranks cross-sectional momentum and volatility.
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
- fees, order-book slippage, funding, realized P&L, and unrealized P&L reconcile in one ledger;
- self-learning edits are restricted to fenced prompt regions and must be backed by a local
  reflector journal.

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

universe → evidence → score → reflection → precheck → execution audit → account/ledger
```

The decision team is pinned to `gpt-5.6-sol` at `xhigh` reasoning effort in the supplied launcher.
Every specialist, PM, Adversary, and due Reflector inherits the root model and effort.

## One 8-hour cycle

The desk is funding-aligned and normally fires at **00:07, 08:07, and 16:07 UTC**:

1. **Provenance check** — reject active reflector text that is absent from the local journal.
2. **Watchdog** — stand down if the previous completed cycle is too recent.
3. **Evidence** — scan the top 40 by quote volume, apply quality/liquidity gates, and retain up to
   20 candidates plus required held symbols.
4. **Score and reflect** — score the prior decisions at fresh marks and tune a prompt only after a
   documented recurrence.
5. **Specialists** — run Sentiment, Technical, and Futures concurrently.
6. **Portfolio Manager** — produce a substantially deployed, approximately dollar- and
   beta-neutral book.
7. **Precheck** — compute deployment, neutrality, concentration, turnover, liquidity, and
   size-aware friction/payback metrics.
8. **Adversary** — audit the arithmetic, trading rules, and all non-flat sentiment citations.
9. **Reconcile** — freeze target quantities at decision marks, capture a fresh two-sided execution
   book, simulate only the quantity delta, settle funding, and persist the heartbeat.

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
| B9 | Added, dropped, or flipped legs stay within the turnover limit |
| B10 | Estimated slippage remains below the liquidity ceiling |
| B11 | No duplicate or unpriced legs |
| B12 | New/flipped-leg carry repays size-aware round-trip friction promptly |

These checks are evidence for the GPT Adversary, which remains the sole decision veto. Any accepted
override of a failing bound must be explicit and quantified.

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
uv run python scripts/desk_precheck.py --state-dir live_state --memory-dir live_memory
uv run python scripts/desk_reconcile.py --state-dir live_state --memory-dir live_memory
```

Do not run reconcile without valid specialist, PM, precheck, and Adversary artifacts in the active
per-cycle pending directory; decision-chain validation will refuse them.

## Installing the 8-hour schedule

Preview the managed crontab block:

```bash
uv run python scripts/install_desk_cron.py --print
```

Install or update it:

```bash
uv run python scripts/install_desk_cron.py --install
uv run python scripts/install_desk_cron.py --check
```

The installer maps the UTC funding boundaries to the host timezone. It refuses timezones whose UTC
offset changes during the following year; the launcher also verifies the UTC slot and fails closed
if the cron mapping is stale.

## Configuration

[`config.yaml`](config.yaml) contains deterministic plumbing settings:

- paper account size;
- top-volume scan and post-gate universe sizes;
- minimum average daily volume;
- 8-hour cadence;
- BTC reference symbol;
- rolling beta lookback.

Trading decisions and sizing remain agent-owned. `live: false` is a permanent project invariant.

## Local runtime data

The following are intentionally ignored by Git:

```text
live_state/       paper account, reports, execution audits, and ledger
live_memory/      pending agent outputs, scorecard, and reflector journal
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

---

Built with Codex, GPT agents, Python, and an unreasonable affection for audit trails.
