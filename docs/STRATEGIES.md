# Live Strategy

The production strategy is a deterministic, weather-only grinder. Its purpose is to enter short-dated temperature markets only when an external forecast supports the traded outcome at a better probability than the market price.

This document describes policy, not a profit guarantee. Weather observations, venue resolution rules, forecast error, liquidity, and execution can all produce losses.

## Selection pipeline

Every candidate passes the following sequence:

1. **Active market** — the market must be open, tradable, and have identifiable outcome tokens.
2. **Weather classification** — the question must describe a supported weather or temperature event. All other categories, including cryptocurrency, are rejected.
3. **Time window** — the configured close or `gameStartTime` must be within six hours. A weather market whose Gamma deadline is already stale can still pass when its target date is today in US/Eastern and Polymarket continues accepting orders. A non-current target date must also have at least 12 hours to close, which normally excludes it from the six-hour discovery window.
4. **Market activity** — reported liquidity and reported 24-hour volume must each be at least $50. `max_spread` is 1.0, so it rejects invalid or extreme quote relationships rather than imposing a practical tight-spread gate.
5. **Price band** — the candidate's current ask must be between 0.90 and 0.97 inclusive.
6. **Forecast mapping** — the city, target date, unit, temperature band, and traded outcome must map unambiguously to forecast inputs.
7. **Forecast edge** — for a fresh entry, the ensemble probability for the traded outcome must be at least `candidate ask + 0.02`. Missing, invalid, insufficient, or excessively divergent model data fails closed.
8. **Portfolio checks** — the position must respect the city/date count, opposite-outcome, exposure, minimum-order, and available-cash rules.
9. **Execution check** — executable order-book depth must support the venue minimum and intended order.

Discovery runs three Gamma queries: soonest closing, highest volume, and a Weather-tagged slice covering the preceding 24 hours for stale daily deadlines. The production `scan_limit` is zero, which means no client-side result ceiling; each query walks Gamma's 100-row pages until the API returns a short page. Results are then deduplicated by market ID.

The live FOK order uses a maximum price of `candidate ask + one tick`, capped at 0.99. Consequently, a candidate admitted at 0.97 can have a 0.98 order guard when the tick size is 0.01. The engine records the actual fill price returned by the venue. Before submitting, it limits the stake to 90% of executable ask-side depth; if that cannot cover the minimum order, the trade is rejected.

The scanner records considered weather outcomes in `data/decision_journal.jsonl`. Each row captures the deepest state reached during that scan: rejected, eligible but not actionable, actionable but not selected, picked but not executed, or executed. This file does not itself add later resolution results. The broader `data/forward_eligible_log.jsonl` can be reconciled later with `scripts/reconcile_forward_log.py`.

## Forecast model

`polymarket_bot/weather_forecast.py` requests Open-Meteo `best_match`, ECMWF IFS 0.25°, and GFS Global forecasts in parallel. At least two models must respond, and a model spread above 3°C causes the lookup to return no probability. The engine converts the consensus daily high or low into a probability for the selected outcome. The decision record includes the probability, candidate ask, calculated edge, city, date, and broad region.

Forecast data is a gate, not ground truth. A probability of 1.0 is still an estimate and must not be interpreted as certainty.

## Portfolio sizing

Full-deployment mode attempts to distribute equity across eligible lines:

- A fresh line targets an equal share of equity across open and actionable lines, capped at the greater of 5% of equity or $5, and never above total equity.
- When a tick has no fresh actionable market and at least one live line is open, unallocated cash may be redistributed equally among qualifying held lines up to the greater of 10% of equity or $5.
- Existing positions can be topped up toward the shared target, but never beyond the on-chain line cap.
- The engine enforces five shares; at the configured entry prices that is approximately $4.50–$4.95. Its top-up threshold is $5.
- If too few independent opportunities exist, the portfolio may retain cash.

For held-line redistribution only, the engine rebuilds a relaxed eligibility pool with the forecast-edge, bracket-margin, generic EV, and quality gates disabled. Weather classification, city bans, timing, 0.90 floor, 0.97 hard cap, activity, spread, and accepting-orders checks remain. This path can add only to a token already held; it cannot open a fresh position. “Full deployment” therefore means deploy practical capital across fresh qualified entries and qualifying held lines, not guarantee zero cash.

## Diversification and duplicates

The engine permits multiple distinct temperature bands within the same event. It does not treat different bands or outcomes as identical bets. It still applies these protections:

- No more than two open positions for one city and target date.
- No simultaneous opposite outcomes on the same binary market.
- No accidental repeat purchase beyond the configured line cap.

## Exit behavior

Weather positions are designed to ride through market noise:

- The configured percentage take-profit, weather price stop, weather forecast-flip exit, double-down path, and soccer stop loss are disabled in all three production profiles.
- Sell a normal weather winner only when the executable bid reaches 0.99, or allow the position to settle.
- Do not apply a stop loss to weather positions.
- The execution layer blocks a sale below entry unless an operator explicitly enables the manual loss-sale override or the reason is one of three confirmed loss exits. Those confirmed exits are disabled by the current profiles.
- Do not force-close merely because the nominal `endDate` has passed while the market remains open for resolution.

This policy avoids realizing losses from transient repricing, but it also accepts that an incorrect forecast can lose most or all of the stake.

## Disabled live modes

The repository retains general-purpose grinder and smart-money components for research and compatibility. They are not part of the maintained production launchers. Random fallback selection is disabled, and no LLM participates in the live scan or trade-selection path.

Any future live-strategy change must update this document and include focused unit tests.
