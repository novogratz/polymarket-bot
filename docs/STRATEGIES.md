# Live Strategy

The production strategy is a deterministic, weather-only grinder. Its purpose is to enter short-dated temperature markets only when an external forecast supports the traded outcome at a better probability than the market price.

This document describes policy, not a profit guarantee. Weather observations, venue resolution rules, forecast error, liquidity, and execution can all produce losses.

## Selection pipeline

Every candidate passes the following sequence:

1. **Active market** — the market must be open, tradable, and have identifiable outcome tokens.
2. **Weather classification** — the question must describe a supported weather or temperature event. All other categories, including cryptocurrency, are rejected.
3. **Time window** — the configured close must be no more than six hours away.
4. **Price band** — the executable ask must be between 0.90 and 0.97 inclusive.
5. **Forecast mapping** — the city, target date, unit, temperature band, and traded outcome must map unambiguously to forecast inputs.
6. **Forecast edge** — the ensemble probability for the traded outcome must be at least `ask + 0.02`. Missing or invalid forecast data fails closed.
7. **Portfolio checks** — the position must respect the city/date count, opposite-outcome, exposure, minimum-order, and available-cash rules.
8. **Execution check** — executable order-book depth must support the venue minimum and intended order.

The scanner records decisions in `data/decision_journal.jsonl`, including opportunities that were not traded. This provides the evidence needed to compare actual outcomes with missed or rejected entries.

## Forecast model

`polymarket_bot/weather_forecast.py` requests multiple Open-Meteo models and converts their daily high or low estimates into a probability for the selected market outcome. The decision record includes the resulting probability, market ask, calculated edge, city, date, and region.

Forecast data is a gate, not ground truth. A probability of 1.0 is still an estimate and must not be interpreted as certainty.

## Portfolio sizing

Full-deployment mode attempts to distribute equity across eligible lines:

- Each line initially targets approximately 5% of current equity.
- Unallocated cash may be redistributed while no line exceeds the 10% hard cap.
- Existing positions can be topped up toward the shared target, but never beyond the on-chain line cap.
- Orders below the venue's approximately $5 minimum are not submitted.
- If too few independent opportunities exist, the portfolio may retain cash.

“Full deployment” therefore means deploy all practical capital across valid opportunities, not bypass selection or execution controls.

## Diversification and duplicates

The engine permits multiple distinct temperature bands within the same event. It does not treat different bands or outcomes as identical bets. It still applies these protections:

- No more than two open positions for one city and target date.
- No simultaneous opposite outcomes on the same binary market.
- No accidental repeat purchase beyond the configured line cap.

## Exit behavior

Weather positions are designed to ride through market noise:

- Sell only when an executable bid reaches 0.99, or allow the position to settle.
- Do not apply a stop loss to weather positions.
- Do not intentionally sell a weather position below its entry price.
- Do not force-close merely because the nominal `endDate` has passed while the market remains open for resolution.

This policy avoids realizing losses from transient repricing, but it also accepts that an incorrect forecast can lose most or all of the stake.

## Disabled live modes

The repository retains general-purpose grinder and smart-money components for research and compatibility. They are not part of the maintained production launchers. Random fallback selection is disabled, and no LLM participates in the live scan or trade-selection path.

Any future live-strategy change must update this document and include focused unit tests.
