# Mission — Weekly Cross-Section Market-Neutral Desk

This is a PAPER-only Binance USD-M perpetual-futures desk designed to harvest cross-sectional
momentum while remaining dollar neutral.

The strategy is intentionally simple and testable:

1. Once per ISO week, enumerate active crypto-only USDT perpetuals with at least 181 completed UTC
   daily observations.
2. Rank them by cumulative Binance quote-asset volume over the latest 180 completed UTC days and
   freeze the Top 50.
3. Measure each Top-50 market's trailing seven-day long total return:

   `price return - Σ(funding rate × settlement mark / starting price)`

4. Freeze the ten best as longs and the ten worst as shorts until the next weekly refresh.
5. Target 1x gross with exactly half of gross in each sleeve. Every selected name is held.
6. GPT agents decide only the within-sleeve weights. Two independent allocators emphasize alpha
   and risk/cost respectively; a Weight PM resolves them and a concise Adversary audits the result.
7. The daily decision loop may revise weights, but never the weekly symbols or sides.

The desk does not ask agents to discover trades, browse news, predict catalysts, or decide whether
to deploy. This keeps token use focused on the one judgment that remains useful: how strongly to
weight each already-selected winner and loser.

All candles come exclusively from the local Binance proxy and must include the current UTC candle.
Weekly funding history must be complete. PAPER fills use fresh two-sided order books, exchange lot
rules, taker fees, depth-aware slippage, adverse-selection reserve, and legging reserve. Funding is
settled from the historical event stream. State commits remain durable and replayable.

Profit is the objective, not a guarantee. The desk is evaluated on net return, drawdown, turnover,
and realized Sharpe after funding, fees, and slippage. Agents receive those performance facts every
cycle and must prefer expected improvement over cosmetic daily churn.

The runtime is GPT-only through Codex login: `gpt-5.6-sol`, `xhigh`, PAPER forever (`live:false`).
