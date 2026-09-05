# Research validation and promotion policy

This desk does not equate a profitable backtest, a high raw Sharpe ratio, or a few profitable
cycles with evidence of alpha. Research is PAPER-only and cannot alter the live paper book.

## Immutable trial registry

Every champion, challenger, and benchmark must be registered **before** its evaluation window.
The definition freezes its policy hash, parameters, evaluation start, and maximum label horizon:

```bash
uv run python scripts/desk_research_validation.py register \
  --registry live_memory/research-trials.jsonl \
  --definition research/my-trial.json
```

Reusing a trial ID with different content is rejected. Failed and discarded trials remain in the
registry because they count toward multiple-testing risk. Registration timestamps must be
timezone-aware and no later than the declared evaluation start. The evaluator rejects a common
return window that begins before any registered policy's evaluation start.

## Required comparisons

The production policy is evaluated against contemporaneous, point-in-time shadow returns for:

- cash;
- the unchanged prior book;
- equal-weight dollar-neutral eligible names;
- a frozen beta-adjusted residual-momentum long/short policy;
- a frozen, history-qualified carry policy; and
- BTC as opportunity-cost context only, never as the risk-equivalent primary benchmark.

All streams use the same available-at-the-time universe, execution clock, exchange quantization,
fees, funding, conservative depth/latency model, and missing-data rules. Survivorship-filled or
future-ranked universes are invalid.

## Walk-forward protocol

Research uses chronological walk-forward splits. Purge is at least the longest forward label
horizon and embargo is declared before evaluation. Parameter selection occurs inside each
training window; the next test window remains untouched. A final forward paper shadow begins only
after the policy and hash are frozen.

The validation command reports active return, information ratio, drawdown, Deflated Sharpe Ratio,
Probability of Backtest Overfitting, and a minimum-track-record estimate:

```bash
uv run python scripts/desk_research_validation.py evaluate \
  --registry live_memory/research-trials.jsonl \
  --returns research/common-horizon-returns.json \
  --benchmark residual-momentum-v1 \
  --purge 7 --embargo 1 \
  --output live_memory/research-validation.json
```

The report is measurement only. It never promotes a strategy or grants trading permission.

## Promotion evidence

The GPT investment committee may recommend a challenger only when all predeclared criteria are
available. At minimum:

1. positive net active return and information ratio versus the frozen risk-equivalent benchmark;
2. positive results after all fees, realized funding, quantization, latency, slippage, partial-fill,
   and legging assumptions;
3. no material dependence on one asset, one week, or one market regime;
4. a 95% Deflated Sharpe result and acceptable PBO under the declared purged protocol;
5. the computed minimum track record is satisfied; and
6. forward PAPER shadow behavior is consistent with the walk-forward result and stays within the
   predeclared drawdown and risk budget.

Failure to meet the evidence threshold means **insufficient evidence**, not proof that a strategy
has no value. No criterion guarantees future profit, and no policy may promise a positive result
every week.
