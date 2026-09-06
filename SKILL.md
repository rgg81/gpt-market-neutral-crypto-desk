---
name: gpt-weekly-cross-section-desk
description: Run one PAPER cycle of the deterministic Top-50, top-10-long/bottom-10-short crypto desk with GPT agents deciding only daily weights.
---

# GPT Weekly Cross-Section Desk

Read `AGENTS.md`, `MISSION.md`, and the complete `docs/desk-cycle-runbook.md`. Execute that runbook
exactly. Use the launcher-pinned `gpt-5.6-sol` at `xhigh`; all subagents inherit it. PAPER only.

The production path is:

```text
proxy → recovery → watchdog → deterministic weekly selection/daily packet
      → alpha allocator + risk allocator (parallel)
      → Weight PM → precheck → Weight Adversary → at most one PM revision
      → fresh-L2 PAPER reconcile → status
```

Do not invoke the legacy sentiment, technical, futures, Reflector, scoring, or Book-discovery
roles. Do not browse. Symbols and sides are immutable for the week: ten long and ten short. Agents
may output weights only, all 20 names remain held, each sleeve sums to one, and the target is 1x
gross/dollar neutral. On any unresolved error, halt and leave the prior completed book standing.
