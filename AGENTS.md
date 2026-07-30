# AGENTS.md — GPT Desk Operating Rules

This repository is the **paper-only LLM market-neutral crypto desk**. Before a cycle, read
`MISSION.md` and `docs/desk-cycle-runbook.md`. The runbook is the exact one-cycle orchestration and
these rules are non-negotiable.

## Runtime identity

- **GPT only, fixed tier.** The scheduled root is `gpt-5.6-sol` at `xhigh` reasoning. Every
  Reflector, specialist, PM, and Adversary subagent inherits that exact model and effort. Never
  select, mention, or fall back to Claude/Opus or a cheaper/faster GPT model.
- **ChatGPT subscription only.** Use Codex login authentication, never `OPENAI_API_KEY` or another
  raw-API runner. `scripts/run_desk_cli.py` is an offline/injection test seam, not the production
  orchestration path.
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
- **Neutral by default.** Build a substantially deployed, dollar- and beta-neutral long/short book
  with a BTC hedge for residual beta. A directional tilt requires explicit PM justification.
- **The Adversary is the sole decision veto.** A rejection gets exactly one PM revision. Keep the
  original book/precheck and recorded verdict; do not run a second adversarial pass.
- The Adversary must open every URL behind every non-flat sentiment read and persist complete
  `citation_checks`, including unselected names. An accepted selected leg cannot use a claim the
  Adversary marked unsupported.
- Never fabricate evidence, sources, agent outputs, fills, reports, or a successful cycle. HALT on
  an unresolved safety/provenance failure and leave the prior completed paper book standing.

## One cycle

Follow `docs/desk-cycle-runbook.md` exactly:

```text
watchdog → evidence → score → optional reflector
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
