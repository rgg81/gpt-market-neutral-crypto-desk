# OPERATION MARKET-NEUTRAL — LLM Desk

**We are an autonomous crypto-futures PAPER desk run by a team of GPT agents. One mandate: stay roughly neutral to the overall crypto market and harvest RELATIVE value — cross-sectional sentiment, technical trend, and futures positioning — on Binance USD-M perpetual futures (paper). The agents decide; deterministic code only feeds data and records fills.**

We run an **LLM desk, not a deterministic one**: three specialist analysts (sentiment via live web search, technical, futures/funding-OI) read every candidate coin in parallel, a market-neutral **portfolio manager** ranks the top buyers and top sellers and constructs the book, and an **Adversary** challenges it — fact-checking cited catalysts and auditing risk. The Adversary is the desk's only anti-hallucination and risk check; a book it rejects gets exactly one PM revision. There is no deterministic reviewer and no deterministic sizing — the agents own ranking, construction, sizing, and neutrality.

We run **equal capital on both sides** on a $20k paper account (~1× gross): ~$9.5k long and ~$9.5k short is the default. Neutrality (dollar + beta) is the **default construction stance** and ≥90% total gross is the ordinary state, not a command to manufacture weak alpha. Alpha gross and BTC-hedge gross are reported separately. When one side has no qualifying alpha, or drawdown and calibration-eligible PM edge justify defense, the GPT PM may scale both alpha sides and the hedge down with an explicit GPT Adversary-approved B1 under-deployment override. The ordinary operating target is absolute beta-dollar residual ≤5% of equity; 15% is an emergency hard ceiling, not a target. A directional tilt requires an explicit, quantified justification and is to be avoided. A dedicated BTC-perp hedge leg absorbs residual beta; rolling beta is re-estimated each cycle.

We are **all-weather by construction**: because the book is market-neutral, it aims to be positive across regimes rather than betting on direction.

Profit hierarchy is regime-aware. Persistent beta-adjusted relative price momentum is the alpha
anchor. In verified chop, conservative, history-qualified funding carry and low friction may lead.
In a strong trend, price momentum dominates small carry: a few basis points of funding can
never by itself justify fading a 20–40% move or preserving a materially losing contradicted seat.
Agents must compare maximum-horizon carry with seat loss/risk and require a concrete forward price
thesis for any exception; “recovery potential” is not evidence.

Price evidence is multi-horizon and relative: raw and BTC-beta-adjusted 6h/24h/72h/168h momentum,
24h acceleration, and 72h drawdown come from the mandatory candle proxy. The desk seeks to own
relative leaders and short relative laggards, not blindly fade an all-market rally for carry. A
broken non-hedge alpha seat with no positive forward price edge exits completely; loss-control
decreases and drops never compete with new risk under the aggressive-turnover cap. Temporary cash
is preferable to contradicted alpha when no neutral replacement has positive net edge.
Every incumbent alpha seat must pass a fresh continuation, calibration, invalidation,
hold-versus-cash, and residual-risk review every cycle. Incumbency reduces switching cost; it is
never evidence. Forecast-inclusive friction payback is arithmetic, not proof that a self-authored
forecast is credible.
The price forecast is a one-time horizon outcome, never a perpetual per-8h rate: break-even caps
its contribution at maturity and permits only carry to accrue afterward.
New forecasts use only the committed-mark 24h, 72h, or 168h buckets and must cite the matching
non-overlapping calibration sample. Risk review includes both same-side positive residual
correlation and signed-position co-risk, so opposite-side/negative-correlation exposures cannot
masquerade as diversification.

We operate on **two clocks**: one full **24h GPT decision cycle** at 00:07 UTC and token-free
deterministic funding/portfolio heartbeats at 08:07 and 16:07 UTC. Each full cycle scans the top-40
by 24h volume fresh and quality-filters to at most 20 liquid, established names. The intervening
heartbeats settle and mark the unchanged PAPER book; they never propose or record a fill. We pay
**realistic costs** — taker/maker fees, per-symbol signed funding, depth-aware slippage — and the
records preserve achieved deployment, dollar/beta residuals, funding, and equity truthfully.
Reconcile and heartbeat accounting publish through durable, replayable PAPER transactions so a
process crash cannot separate the account mutation from its audit trail.
The deterministic watchdog receipt is recomputed at evidence time, hash-bound through the decision
packet, and reproduced before reconcile. EARLY/future-clock firings and any cycle other than the
single next manifest generation fail closed; one launcher invocation can attest exactly one cycle.

Candles are a non-negotiable data dependency. Every 1h momentum and 1d beta kline comes exclusively
through the local caching service at `~/binance-proxy`; direct Binance OHLCV is forbidden. The
launcher checks the proxy before a scheduled claim, and evidence records per-symbol proxy
provenance and requires the currently-forming UTC candle for every series. An unavailable proxy,
missing series, or stale tail HALTS before GPT agents see a cycle—never a silent flat/neutral
substitute.
The current-forming tail remains mandatory freshness proof and supplies a timestamped live momentum
mark, but incomplete 1h/1d returns never enter beta, covariance, or volatility statistics.

Funding evidence distinguishes the last settled rate from conservative forward carry derived from
historical sign persistence and dispersion. Positioning uses explicit 24h/72h/168h contract-OI
changes and own-history long/short-ratio normalization; USD-value OI and one funding print cannot
masquerade as independent crowding confirmation. Realized funding uses every raw published
rate/settlement-mark pair. Its account-clock collector requires the exact nominal-boundary set of
a stable schedule or a provable single old-to-new interval transition; it persists the proof and
HALTs on an unexplained missing/extra event rather than inventing interval-effective history.

Decision provenance stays bound to the evidence snapshot the agents analyzed. Paper execution uses
a separate fresh two-sided order-book snapshot, with that book's midpoint as the fill reference, so
market movement while the agents reason is never charged as slippage. The pre-trade liquidity
curve follows the same rule: it walks one L2 snapshot against that snapshot's own
`liquidity_mid`, without a volatility multiplier for reasoning delay. PM target notionals are
converted to quantities at the decision mark; the fresh execution mark prices those fixed
quantities rather than silently resizing held legs. Precheck applies the identical quantity
contract: it converts decision-mark dollar deltas to the L2 midpoint before selecting a depth tier
and computing fee/slippage dollars, so a mark move cannot make a large clip look artificially
small.

We trade **cryptocurrencies only** — no tokenized stocks, indexes, metals, or gold coins. The
`live` field only accepts `false`, the Binance feed is always public/keyless, and the desk runs on
the ChatGPT subscription with `gpt-5.6-sol` agents at `xhigh` effort, never a raw API key.

We remember: every decision — the specialists' reads, the PM's book, the Adversary's verdict — is written down before its outcome is known. *We get a little sharper every cycle.*
