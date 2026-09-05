# AGENTS.md — GPT Desk Operating Rules

This repository is the **paper-only LLM market-neutral crypto desk**. Before a cycle, read
`MISSION.md` and `docs/desk-cycle-runbook.md`. The runbook is the exact one-cycle orchestration and
these rules are non-negotiable.

## Runtime identity

- **GPT only, fixed tier.** The scheduled root is `gpt-5.6-sol` at `xhigh` reasoning. Every
  Reflector, specialist, PM, and Adversary subagent inherits that exact model and effort. Never
  select, mention, or fall back to Claude/Opus or a cheaper/faster GPT model.
- **ChatGPT subscription only.** Use Codex login authentication, never `OPENAI_API_KEY` or another
  raw-API runner. `scripts/run_desk_cli.py` and `futures_fund.desk_cycle.run_cycle` are inert
  offline-injection test seams: they require an explicit offline flag plus an opaque capability
  bound to the exact canned `StubAgentRunner`. They are never a production orchestration path.
- **Use real agents.** The root orchestrates; it does not impersonate the specialist, PM,
  Reflector, or Adversary. Spawn the three specialists concurrently and wait for all of them before
  dispatching the PM.

## Hard safety rules

- **PAPER ONLY.** `live` remains exactly `false`. Never add a live-order code path or call an
  exchange order method.
- **LLM proposes, code records.** GPT agents own ranking, construction, sizing, and neutrality.
  Deterministic code only gathers data, computes the documented precheck, validates decision-chain
  provenance, and records paper fills. It never creates or vetoes a trading decision.
- **Stay inside v2.** State is `live_state/`; working memory is
  `live_memory/pending/<cycle>/`. Never inspect, migrate, or mutate the original sibling desk.
- **Fresh proxy candles are mandatory.** Every production OHLCV request goes exclusively through
  `http://127.0.0.1:8000` (`~/binance-proxy`). Before the watchdog, run
  `scripts/desk_data_preflight.py`; evidence must prove that every required 1h and 1d series
  contains its currently-forming UTC candle. Proxy failure, missing coverage, or stale candles
  HALT before agents. Never use a direct-Binance or neutral/empty-series fallback for candles.
  The host launcher is authorized to start or restart only the exact `~/binance-proxy` Uvicorn
  process after repeated health failures, before it claims the scheduled cycle.
- **Neutral by default.** Build a substantially deployed, dollar- and beta-neutral long/short book
  with a BTC hedge for residual beta. A directional tilt requires explicit PM justification.
- **The Adversary is the sole decision veto.** A rejection gets exactly one PM revision. Keep the
  original book/precheck and recorded verdict; do not run a second adversarial pass.
- A rejected original must carry structured revision constraints. Bind the single PM revision to
  those constraints and to the Adversary's explicitly allowed final bound failures; this enforces
  the veto without deterministic code choosing a trade.
- The Adversary must open every URL behind every non-flat sentiment read and persist complete
  `citation_checks`, including unselected names. An accepted selected leg cannot use a claim the
  Adversary marked unsupported.
- Never fabricate evidence, sources, agent outputs, fills, reports, or a successful cycle. HALT on
  an unresolved safety/provenance failure and leave the prior completed paper book standing.
- Drops and same-side material decreases are loss control and do not consume B9's cap on aggressive
  entries, flips, and increases. A price-regime-broken non-hedge alpha seat with non-positive
  expected price edge exits fully, even if deployment temporarily falls below its ordinary floor;
  preserve dollar/beta safety and disclose the Adversary-approved B1 override.

## One cycle

Follow `docs/desk-cycle-runbook.md` exactly:

```text
proxy freshness → watchdog → evidence → score → optional reflector
         → post-reflection decision-start seal
         → sentiment + technical + futures (parallel)
         → PM → precheck → adversary → at most one PM revision
         → reconcile → heartbeat
```

An EARLY watchdog result means immediate stand-down. All three failed specialists means HALT.
Malformed outputs get only the documented retry. Do not open multiple cycles to backfill a gap.

## Correctness and repair

Never weaken `live=false`, the absence of order placement, decision-chain binding, truthful
friction/PnL accounting, or the one-revision limit. Diagnose root causes; do not guess-patch.
Liquidity cost must compare depth with the same book's midpoint; decision-to-execution drift is
not slippage. Convert PM target notionals to quantities at the decision mark so a preserved held
leg stays a no-op at the later execution mark.
Run `uv run pytest` and `uv run ruff check .` after code repairs. Reflection edits may touch only
the managed prompt regions and must be journaled. Do not edit the scheduler or crontab from inside
a desk cycle.
