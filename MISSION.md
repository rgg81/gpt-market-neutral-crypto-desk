# OPERATION MARKET-NEUTRAL — LLM Desk

**We are an autonomous crypto-futures PAPER desk run by a team of GPT agents. One mandate: stay roughly neutral to the overall crypto market and harvest RELATIVE value — cross-sectional sentiment, technical trend, and futures positioning — on Binance USD-M perpetual futures (paper). The agents decide; deterministic code only feeds data and records fills.**

We run an **LLM desk, not a deterministic one**: three specialist analysts (sentiment via live web search, technical, futures/funding-OI) read every candidate coin in parallel, a market-neutral **portfolio manager** ranks the top buyers and top sellers and constructs the book, and an **Adversary** challenges it — fact-checking cited catalysts and auditing risk. The Adversary is the desk's only anti-hallucination and risk check; a book it rejects gets exactly one PM revision. There is no deterministic reviewer and no deterministic sizing — the agents own ranking, construction, sizing, and neutrality.

We run **equal capital on both sides** on a $20k paper account (~1× gross): ~$9.5k long and ~$9.5k short is the default. Neutrality (dollar + beta) is the **default construction stance**, never an excuse to sit flat; ≥90% deployed is the default state. A directional tilt is allowed only with an explicit, written justification and is to be avoided. A dedicated BTC-perp hedge leg absorbs residual beta; rolling beta is re-estimated each cycle.

We are **all-weather by construction**: because the book is market-neutral, it aims to be positive across regimes rather than betting on direction.

We deploy on **one clock**: an **8h cycle** aligned to the funding boundaries (00/08/16 UTC). Each cycle scans the top-40 by 24h volume fresh and quality-filters to at most 20 liquid, established names on a new candle. We pay **realistic costs** — taker/maker fees, per-symbol signed funding, depth-aware slippage — and the reconcile ledger records the achieved deploy / dollar / beta residuals and equity truthfully every cycle.

Decision provenance stays bound to the evidence snapshot the agents analyzed. Paper execution uses
a separate fresh two-sided order-book snapshot, with that book's midpoint as the fill reference, so
market movement while the agents reason is never charged as slippage. The pre-trade liquidity
curve follows the same rule: it walks one L2 snapshot against that snapshot's own
`liquidity_mid`, without a volatility multiplier for reasoning delay. PM target notionals are
converted to quantities at the decision mark; the fresh execution mark prices those fixed
quantities rather than silently resizing held legs.

We trade **cryptocurrencies only** — no tokenized stocks, indexes, metals, or gold coins. The
`live` field only accepts `false`, the Binance feed is always public/keyless, and the desk runs on
the ChatGPT subscription with `gpt-5.6-sol` agents at `xhigh` effort, never a raw API key.

We remember: every decision — the specialists' reads, the PM's book, the Adversary's verdict — is written down before its outcome is known. *We get a little sharper every cycle.*
