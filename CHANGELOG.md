# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`ban_crypto` flag — crypto banned on bot 3 + zaza** (user 2026-06-24, "ban crypto on grinder 3 + zaza"): new `ban_crypto` profile flag (`POLYMARKET_RACE_BAN_CRYPTO`) drops any market that `classify_category` tags `crypto` at entry selection — an ALWAYS-ON exclusion that survives `unban_all_markets` (which otherwise bypasses the whole ban list). Set `true` on `grinder_b.toml` + `grinder_zaza.toml`; bot 1 (`grinder.toml`) leaves it off. Live scan confirmed 614 crypto markets dropped, 0 reach eligibility. `BanCryptoLaneTests` pin the drop-under-unban and the off-path.

### Changed

- **bot 3 + zaza realigned to bot 1** (user 2026-06-23, "align bot 3 + zaza with bot 1 — it's making money"): reverted bot 3's weather-only experiment (`weather_only` off, window 24 h → 4 h). Both `grinder_b.toml` and `grinder_zaza.toml` `[race]` sections are now **byte-identical to bot 1** (`grinder.toml`): v4 — 4 h window, 0.80–0.94 band (hard cap 0.96), `fixed_stake_usd = 5`, `unban_all_markets`, 12 orders/tick, `sl_pct 0.30` + `sl_min_exit_price 0.50`, `resolved_exit_threshold 0.99`. (zaza already matched; only bot 3 changed.) The `weather_only` flag + `is_weather_market` detector remain in code (disabled on all profiles) for future use.

### Added

- **Weather-only lane** (user 2026-06-23): `weather_only` profile flag (`POLYMARKET_RACE_WEATHER_ONLY`) + `is_weather_market` detector (temperature / °C / °F / weather / rainfall / snowfall / high-low temp). When on, entry selection keeps ONLY weather markets and bypasses the normal weather ban. Briefly enabled on bot 3 then reverted (weather markets rarely price in the 0.80–0.94 favorite band). Flag stays in code, **off on every profile**. `WeatherOnlyLaneTests` pin detection + keep-only-weather / bypass-ban behavior.
- **p / q / edge in the live Telegram report** (user 2026-06-22). The 30-min LIVE REPORT's `PERFORMANCE v4` block now shows a `🎯` line **for all-time and for today**: `p` = average entry price paid, `q` = win rate (wins/(wins+losses), the realized proxy for the true win probability), and `edge(q−p)` in points. The bot is +EV only when `q > p`, so this surfaces the single number that decides whether the strategy has an advantage. Deterministic, fail-soft (`_pq_line` in `scripts/live_analyst.py`).

### Changed

- **Zaza migrated to the v4 strategy to match bot 1** (user 2026-06-22, "zaza not doing enough bets, match bot 1"): `grinder_zaza.toml` was still on the pre-v4 config (15% stakes, 4 orders/tick, banned categories, 0.85–0.97 band) while bot 1 (main `grinder.toml`) and bot 3 (`grinder_b.toml`) had moved to v4. Ported the full v4 `[race]` to zaza: `unban_all_markets = true` (+ data-driven category auto-disable, `min_resolution_clarity = 60` always-on), `fixed_stake_usd = 5` (no %-sizing / double-down), `max_orders_per_tick = 12`, band 0.80–0.94 with `max_price_hard_cap = 0.96`, floors `min_liquidity 250 / min_volume 1000`, `sl_pct 0.30` + `sl_min_exit_price 0.50`, `resolved_exit_threshold 0.99`. Kept zaza's 8 h window (bot 1 uses 4 h) for extra volume. Measured effect: eligible candidates 10 → 31, distinct actionable bets per tick 2 → 9.

### Fixed

- **Resolved positions now close promptly at their TRUE value** (user 2026-06-21, "some bets don't seem to close when they should / redeemed up with 0 or with gains"). `_sync_live_positions` gained a resolution-reconcile pass over the Data API holdings: a position the chain marks **`redeemable`** is booked as a **resolved win** (closed at its full on-chain value), and a position whose value has collapsed below the dust floor **and whose endDate has passed** is booked as a **resolved loss at ~$0**. Previously a settled loser was dropped by the min-value filter and written off at its **stale ~0.50 mid-price** (overstating realized P&L / equity), and a redeemable winner could sit "open" until a +6h/8h expiry timer. The past-endDate guard means a mid-game gap (low value but still live) is **not** mis-booked as a loss. Diagnostic on the live wallet confirmed the "résolution en cours" 15-min crypto positions were genuinely *pending oracle settlement* (`redeemable=false`, curPrice≈0.50), not lost — the bot now books them the moment the chain settles them. New `ResolvedReconcileTests`.

### Added

- **Resolution-safety filter (always-on, survives unban)** (user 2026-06-21, "do everything"). New `race_min_resolution_clarity` (set to **60** in both profiles): `_build_eligible_candidates` skips any market whose `resolution_clarity` (in `forecast.py`) is below the threshold — subjective / ambiguous-settlement wording (judges' discretion, "deemed", "disputed", "considered", "to be determined", …). A clean objectively-resolvable market scores 100; one strong subjective marker drops it below 60. Unlike the EV/quality gates this needs **no history**, so it is the one structural protection that stays ON even under `unban_all_markets`. Tests: `ResolutionSafetyTests` in `tests/test_forecast.py`.
- **v4 performance block in the Telegram LIVE REPORT** (`scripts/live_analyst.py`). After ≥10 closed trades the report adds `PERFORMANCE v4 :` — ROI / Sharpe / profit factor / max drawdown — and `🏷️ Catégories à risque :` listing the worst per-category ROIs (⛔ on any already auto-disabled), so the data-driven governance is visible at a glance without running `journal-stats`. Deterministic, fail-soft.

### Changed

- **Maximize bets within the 4h window** (user 2026-06-21, "as many bets as possible"). `race.max_orders_per_tick` 4 → **12** in both grinder profiles so a fresh bankroll deploys fast across many distinct eligible games each tick (each new bet is a flat $5 and still passes every entry filter; one bet per game/event keeps correlated markets out). The self-tuner bound for `race.max_orders_per_tick` was widened (1,5) → **(1,20)** so the daily loop can't drag it back down. The $5 cap bounds per-trade risk regardless of count.
- **Docs refreshed to v4 across the board** — `CLAUDE.md`, `README.md`, `CODEX.md`, `AGENTS.md`, both skill files, `docs/STRATEGIES.md` (fixed-$5 sizing section replacing the Kelly one; v4 entry/exit), `docs/AUTONOMY.md` (TUNABLE bounds), `docs/PROFILES.md`, and the `.claude`/`.codex` `memory.md` files (retired copy-strategy content replaced with the v4 grinder summary). Stale 0.97 winner-exit / 0.85–0.97 band / Kelly-sizing references removed.

- **Winners sell at 0.99** (user 2026-06-21, "sell at 0.99 as well"). `resolved_exit_threshold` 0.97 → **0.99** in both grinder profiles, the `WINNER_FLOOR` in `trading.execute_live_sell` 0.97 → **0.99**, and the self-tuner bound pinned (0.97,0.97) → **(0.99,0.99)**. The fast-lane 0.98 downgrade for esports/stocks is removed — one flat 0.99 winner exit across every lane: a winner sells only at a real 0.99 bid, else rides to on-chain settlement at 1.00. Tests updated (`test_live_sell_winner_floor_refuses_sub_099_resolved_exit`, `test_auto_improve_tuner_pins_resolved_exit_threshold_at_099`, `test_v4_winner_exit_requires_099_even_for_low_entry`, `test_v4_no_fast_lane_098_exit_holds_until_099`).
- **bot 3 + zaza entry window 4 h → 8 h** (user 2026-06-17, "not enough bets"): diagnosis showed both bots sat on abundant cash ($89 / $127) with only 1–2 open positions despite 10 eligible candidates — because within a 4 h window there's usually only ~1 distinct game, and one-bet-per-game (correctly) takes a single bet from it (the other 9 eligibles were the same game's specials + player props). Cash/stake/filters were not the limiter; distinct games were. Widening to 8 h roughly doubles the games in the window (still same-day — no overnight holds, `daily_expiry_fallback` stays false). Hard cap (`max_hours_cap = 0`). Intentionally diverges from bot 1's 4 h for volume on bots 2/3 + zaza.

### Added

- **v4 production config — Phase 2 (part 2): forecasting model + EV/quality gates + dashboard analytics** (user 2026-06-21, "build a real forecasting model too"; "no pause halts though"). New `polymarket_bot/forecast.py`:
  - **Forecasting model** — a deterministic empirical-calibration forecaster. `predicted_probability(category, ask, table)` calibrates a favorite's win probability per (category, price-bucket) from the realized ledger, shrunk toward the prior (overall realized win rate, default 0.95) by a pseudo-count. **Edge = predicted_probability − ask** (the ask is the market's implied probability). A cell that historically underperforms its price yields predicted < ask → negative edge. No LLM — pure arithmetic over the ledger; degrades to the prior with no history.
  - **EV + quality gates (OPT-IN, default OFF)** — `race_min_edge` filters sub-edge outcomes; `race_min_quality_score` filters low `quality_score` (0–100 blend of edge / volume-vs-`preferred_volume` / resolution clarity / historical category & price-bucket ROI). Both **default 0 (off)** and are 0 in the profiles — they need realized history to calibrate, so enabling them on a fresh bot would starve it (the spec's own "no decisions before data" rule). `_run_race_tick` builds the calibration context per tick (fail-open) and `_build_eligible_candidates` applies the gates when enabled.
  - **Dashboard analytics** — `pmbot journal-stats` gains `by_v4_price_bucket` (0.80-0.85 … 0.94-0.96), and `v4_performance` with Sharpe ratio, profit factor, max drawdown, ROI, and the **promotion gate** (`promotion_status`: scale only after ≥ 500 trades AND ROI ≥ 5%).
  - New knobs `race_min_edge` / `race_min_quality_score` / `race_forecast_prior` / `race_forecast_pseudo_count` / `race_preferred_volume_usd` / `race_promotion_min_trades` / `race_promotion_min_roi`. `tests/test_forecast.py` (14 tests). **Per user 2026-06-21 the daily/weekly drawdown halts + large-loss pause were intentionally NOT built.**

- **v4 production config — Phase 2 (part 1): data-driven category auto-disable** (user 2026-06-21, "go phase 2"). New `polymarket_bot/categories.py` — the governance that makes `unban_all_markets` safe by replacing manual bans with data:
  - `classify_category(question, slug)` buckets every market into one of the v4 categories (politics, economics, crypto, ufc, golf, soccer, sports, entertainment, other) via ordered, collision-safe regexes.
  - `category_stats(records)` computes per-category trades / wins / losses / win-rate / total_pnl / total_cost / **ROI** (= pnl / cost) / avg_pnl from the realized ledger.
  - `disabled_categories(records, min_samples, roi_threshold)` returns the categories to drop: ≥ `min_samples` (100) realized trades **and** ROI < `roi_threshold` (−5%). Forward-looking — a fresh bot disables nothing; `other` is never auto-disabled.
  - `_run_race_tick` computes the disabled set from the realized ledger each tick (fail-open) and `_build_eligible_candidates` drops auto-disabled categories at entry selection. New knobs `race_category_min_samples` (100) / `race_category_disable_roi` (−0.05) in both profiles.
  - `pmbot journal-stats` gains a `by_v4_category` breakdown (the same ROI signal the auto-disable acts on). New `tests/test_categories.py` (classification, ROI, sample-floor, the gate). **Still pending in Phase 2: price-bucket analytics, daily/weekly drawdown + large-loss pause, dashboard, and the forecasting model behind the EV / quality-score / Sharpe / promotion gates.**

- **v4 production config — Phase 1: fixed-dollar sizing, tighter band, unban flag** (user 2026-06-21, "Polymarket Bot v4"). The first slice of the v4 rewrite, optimizing for capital preservation / low drawdown over win-rate or volume:
  - **Fixed $5 position sizing** (`race_fixed_stake_usd`): when > 0 every entry stakes EXACTLY that many USD — no Kelly, no % of equity, no martingale, no averaging/double-down, no confidence scaling, no dynamic spread. `_position_cap_usd`, `_entry_cap_usd`, and `_dynamic_stake_target` all short-circuit to the flat amount (capped only by available cash), so the bankroll fully deploys across `bankroll / 5` positions and worst single-trade loss is $5. Double-down disabled in both profiles.
  - **Entry band 0.80–0.94 with a 0.96 hard cap** (`race_max_price_hard_cap`): the absolute ceiling clamps the entry ask regardless of `max_price`, so 0.97/0.98/0.99 are never tradeable.
  - **Liquidity floors**: min liquidity $250, min 24 h volume $1000.
  - **`unban_all_markets` flag**: when on, `is_excluded_market` is bypassed at entry selection — every category is allowed, governed instead by the (Phase 2) data-driven category auto-disable; per-trade risk is bounded by the $5 stake. Set true in both grinder profiles.
  - Both `grinder.toml` and `grinder_b.toml` updated; new `V4ConfigTests` pin fixed sizing, the hard cap, the band, the unban bypass, and the liquidity floors. **Phase 2 (next): category tracking + auto-disable, price-bucket analytics, daily/weekly drawdown + large-loss pause, rich trade logging + dashboard, and the forecasting model driving the EV / minimum-edge / quality-score / Sharpe / promotion gates.**

- **"What will be said" markets banned outright** (user 2026-06-18: a bot bought 'Will the announcers say "Golden Boot" during the Canada vs Qatar FIFA World Cup Match?' — "never bet about what something will say"). New `_SPEECH_MARKET_RE` in `models.is_excluded_market` rejects any market whose question/slug contains word-bounded `say/says/said/saying/mention(s/ed)/utter(s/ed)` across every lane. Bets on whether a commentator/announcer/person will utter a given word or phrase are pure linguistic coin-flips with no convergence edge. Word-bounded so it can't collide with "essay"/"naysayer". Tests: `test_speech_markets_banned`, `test_speech_market_regex_no_false_positives`.

- **Fed / FOMC rate-decision markets banned** (user 2026-06-17, "too far away"): `is_excluded_market` now excludes monetary-policy markets — `fed rate`, `rate cut`/`rate hike`, `cut rates`/`hike rates`/`raise rates`, `interest rate`, `fomc`, `fed funds`, `rate decision`, `basis points`. "Fed rate cut by September 2026 meeting?" resolves months out but shows a near-term Gamma `endDate`, so it slipped through the 4 h entry window and the bots kept re-buying it after manual sells. Phrases are monetary-policy-specific so "approval rating" / "rate of scoring" etc. are not affected (test-pinned).

### Changed

- **bot 3 + zaza `scan_limit` 100 → 750** (user 2026-06-16): match bot 1's full scan breadth (the 2026-06-15 top-100 setting is reverted on these two bots to mirror bot 1 exactly).
- **Zaza + bot 3 synced to bot 1's strategy** (user 2026-06-16, "bring the same strategy as bot 1 — the best bot ever"): `grinder_zaza.toml` was on a stale config (`stake_pct = 0.50`, `max_hours = 8`, `min_price = 0.88`, no double-down, `resolved_exit_threshold = 0.99`, no profit-margin floor) and is now aligned to bot 1 (`grinder.toml`): 15% hard cap with 5% initial entry, 4 h hard window, 0.85 band, generalized dip double-down (`min_price 0.60`), 3-tick confirmed soccer-moneyline SL, 0.97 resolved exit floored above entry by `min_profit_margin 0.02`. `grinder_b.toml` (bot 3) already matched functionally; its double-down keys were made explicit (`max_dip 0.40`, `min_price 0.60`). Both keep `scan_limit = 100` (bot 1 uses 750 — kept per the 2026-06-15 instruction). Profile loader gains the `[race].double_down_min_price` key (was in `config.py` but unmapped).
- **Scan reverted to the top 100 markets** (user 2026-06-15): `scan_limit` 750 → **100** in all live profiles (`grinder.toml`, `grinder_b.toml`, `grinder_zaza.toml`) — a single Gamma page per ordering (soonest-closing / highest-volume), no pagination. The scan no longer walks ~1,000–2,000 raw markets per tick.

### Added

- **Kelly position sizing — near-full-Kelly, aggressive** (user 2026-06-18: "10% isn't enough to make significant money"). Derived the growth-optimal bet fraction from the realized distribution: for win rate p≈0.97, gain b≈8.4%, worst-case total loss a≈1.0, two-point Kelly `f* = (p·b − q·a)/(a·b) ≈ 0.35` (the empirical 304%/98% figures are artifacts of the mean-loss approximation and an undersampled −100% tail — survival, not the empirical peak, is the constraint). Key structural insight: the binding lever is the ENTRY size, not the cap — at a 97% win rate most winners never dip, so they ride at `initial_stake_pct`; the cap is only reached via the dip double-down. So `initial_stake_pct` 0.05 → **0.20** (entries open at 20% of equity, ~4× the prior compounding) and `stake_pct` 0.15 → **0.35** (hard cap / double-down ceiling). Worst single total loss −20% (entry) / −35% (doubled). The self-tuner's `race.stake_pct` clamp was widened (0.05, 0.15) → (0.05, 0.35) so the daily loop can't drag it back down; `initial_stake_pct` stays frozen (outside `TUNABLE`). Both profiles updated; full Kelly derivation added to `docs/STRATEGIES.md`.

- **Daily end-of-day self-learning sidecar** (`scripts/daily_self_improve.sh`, user 2026-06-17: "auto analyse at the end of the day … make adjustment … as part of the main script I run every day … use claude code cli … as a try catch"). The launchers (`run_live_70.sh`, `run_live_b.sh`) now spawn it as a process-group sidecar. Once per day after `DAILY_SELF_IMPROVE_HOUR` (default 23:00 local) it (1) writes a deterministic **end-of-day analysis** — `auto_improve.py --analyze-only` now emits all-time/today/7-day P&L, the win/loss asymmetry, a per-category breakdown, and the 5 worst trades — then (2) runs the existing **fenced Claude self-tuner**. Hard safety: the whole sidecar is wrapped (`set +e` + try/catch) so it can **never crash or stall the live trade loop**; it runs at most once/day (`data/.last_self_improve`); it **always restores the git branch** so the live repo is never left on an auto/ branch (the running bot keeps its loaded code — a tuned config only applies on the next manual restart); all the tuner's fences stay intact (EXIT/SIZING only, entry FROZEN, no stop-loss ever, tests + CI gated, only `grinder.toml` writable). Toggle `DAILY_SELF_IMPROVE=0`.

- **Soccer-moneyline gap fix: 0.92 entry floor + SL anti-gap guard** (user 2026-06-17, after Difaâ El Jadida). We held **"No"** on "Will Difaâ Hassani El Jadida win on 2026-06-17?" at entry 0.8949. Difaâ scored early → our "No" crashed → the confirmed −30% SL fired and **sold the bottom at 0.02 (−97.8%)** — then Maghreb AS de Fès won, so "No" **resolved to 1.0**. Gamma confirms it: Yes=0, No=1. A +$2.55 winner was booked as a −$21.25 loss. Two fixes: (1) **entry floor `SOCCER_MONEYLINE_MIN_ASK = 0.92`** on soccer/sport "Will <X> win on <date>?" moneylines (both Yes and No sides, `_is_soccer_moneyline_text`) — EVERY moneyline loss in the realized history entered at ≤ 0.90; the 0.90+ band is 29 trades with zero losses. (2) **`race_sl_min_exit_price = 0.50` anti-gap guard** — the confirmed SL only executes while the live bid is still ≥ 0.50 (orderly decline); a bid that has gapped below it is a goal-crash that mean-reverts → HOLD to on-chain resolution instead of dumping. New `SoccerMoneylineEntryFloorTests` and `SoccerMoneylineAntiGapSLTests`; the esports-floor test's "non-lane" example switched from a "Will France win" moneyline (now floored) to a first-to-score market.

- **Stop-loss raised to −30% and the gate broadened to all sport moneylines** (user 2026-06-16: "I thought we agreed we would use stop losses for games like this at −30%?"). América FC ("Will América FC win on 2026-06-16?", No) rode **0.88 → 0.30 (~−66%) with no SL** and was sold manually for a ~$22 loss. Root cause: `_is_soccer_moneyline_position` required the slug to contain one of ~20 hardcoded league keywords — América's slug had none, so the gate returned `False` and dropped SL protection. Fixed two ways: (1) `sl_pct` **0.25 → 0.30** in both `grinder.toml` and `grinder_b.toml`; (2) the gate is now an **exclusion model** — it trusts the "Will <X> win on YYYY-MM-DD?" Yes/No regex and returns `True` *unless* the question/slug carries a politics/election keyword (election, primary, governor, senate, president, mayor, nominee, congress, parliament, referendum, ballot, caucus, approval) or an award keyword. So any soccer club / national team is covered regardless of league, while date-phrased elections still ride to resolution and never stop out. New `SoccerMoneylineSLGateTests` pin the América case (covered), elections/awards (excluded), and non-moneyline/non-Yes-No (excluded).
- **Dynamic take-profit floored above entry** (user 2026-06-15): the resolved-exit threshold is now per-position — `min(0.99, max(resolved_exit_threshold, entry + race_min_profit_margin))` (margin 0.02). A high-entry favorite (e.g. Cabo Verde at 0.97) must clear **0.99** to sell, never exiting at break-even; a normal 0.87 entry still exits at the global 0.97. Above 0.99 it rides to on-chain settlement at 1.00. Applied in both `_execute_race_exits` and the winners-only sweep (`_force_close_resolved_positions`), so neither can close a position for ~zero profit. New `race_min_profit_margin` knob; regression tests for high-entry (0.97→0.99) and low-entry (0.87→0.97).
- **Initial-entry size below the hard cap, so the double-down has room** (user 2026-06-14): new `race_initial_stake_pct` (5% in both profiles) sizes a FRESH entry — and any passive top-up — to 5% of equity, reserving the headroom up to the 10% hard cap (`race_stake_pct`) for the dip double-down to fill. Without this, opportunity-spread sizing put every bet straight at the 10% cap on a small bankroll, leaving the double-down $0 of room (it could never fire). `_entry_cap_usd` (entries/passive top-ups) vs `_position_cap_usd` (hard cap / double-down ceiling); `initial_stake_pct = 0` or ≥ cap restores the old single-cap behavior.
- **Redemption watchdog in the RAPPORT LIVE** (user 2026-06-12): resolved positions with real money still awaiting payout (`redeemable: true`, value ≥ $1, losing-side dust ignored) get their own `💰 GAINS RÉSOLUS EN ATTENTE DE PAIEMENT` section. Polymarket auto-redeems winners, so the section is empty in normal operation — when something does linger (the event page is already delisted, so the report is the only place it stays visible), the operator sees it every cycle with a pointer to polymarket.com/portfolio. On-chain self-redemption was deliberately NOT wired into the loop: the SDK has no redeem support and signature-type-3 proxy wallets would need raw transactions through the undocumented relayer.
- **Esports + stocks conditionally re-allowed — "ongoing only"** (user rule 2026-06-12, replacing the blanket bans): **esports** markets are tradeable ONLY while the game is in progress (`gameStartTime` in the past, within 8 h; pre-game or unknown start stays excluded) and **stock/index markets** ONLY during the ongoing regular NYSE session (Mon–Fri 09:30–16:00 ET) for that day's close (`endDate` within 12 h; overnight, weekends, and multi-day stock bets stay excluded). `is_excluded_market` gains a `now` parameter; all detection patterns unchanged. Test-pinned: live LoL game allowed, pre-game/stale/unknown banned; in-session same-day AAPL allowed, after-hours/pre-open/weekend/multi-day/no-endDate banned.
- **Entry-window ladder extended — 24 h rung + daily-expiry fallback** (user rule 2026-06-12): after 4 → 6 → 8 → 10 → 12 the ladder now jumps to **24 h** (`max_hours_cap` 12 → 24), and when even 24 h has nothing actionable a final rung reaches the **end of tomorrow (UTC)** so daily markets ("Will X be Y on <date>?", stamped midnight UTC like the Trump-approval one) stay reachable (new `race_daily_expiry_fallback`, profile key `daily_expiry_fallback`, frozen against the self-tuner). Narrowest-window preference unchanged.
- **RAPPORT LIVE — open positions sorted by expiry + real end info** (user request 2026-06-11, fixed same day): `POSITIONS OUVERTES` lists positions soonest-to-resolve first and each line shows when it ends. First version rendered Gamma's midnight-UTC date-only stamps as a fabricated clock time ("10/06 20:00 ET" for a game kicking off the next afternoon). Now: **sports show the kickoff** from `gameStartTime` (`🏟 Coup d'envoi : 14:00 ET (dans 2h)` / `Match en cours` / `Match terminé — résolution en cours`), timed end dates show the exact ET time + countdown, and **date-only stamps show the date alone** (`📅 Expire le 12/06 (heure exacte non publiée)`). Metadata comes from one batched Gamma reverse-lookup per report (fail-open). Sports sort by kickoff; date-only by end of that day; unknown last.
- **Stock market / equities banned** (user rule 2026-06-11): `is_excluded_market` now blocks indices & ETFs (S&P 500/SPY, Nasdaq/QQQ, Dow/DJIA, Russell, Nikkei, FTSE, DAX), big-cap tickers and company names (GOOGL/Google, AAPL/Apple, TSLA/Tesla, NVDA/Nvidia, MSFT/Microsoft, AMZN/Amazon, META, NFLX/Netflix, AMD, INTC), generic equity terms ("stock market/price", "share price", "market cap", "wall street"), and the `closes above/below $X` price-threshold pattern. Short tickers are matched with a word-bounded regex (`_STOCK_MARKET_RE`) so "spy" never bans a spying scandal and "meta" never bans a metal band — both pinned by tests. The grinder had bought "S&P 500 (SPY) closes above $725" on 2026-06-10.
- **Dynamic entry window — 4 h preferred, 12 h max**: the entry scan prefers markets closing within **4 h**; when that window has no actionable candidate it widens in 2 h steps (4 → 6 → 8 → 10) and stops at the **12 h cap** (`race_max_hours = 4.0`, new `race_max_hours_cap = 12.0`, ladder in `_entry_window_ladder`; cap ≤ base or 0 disables the ladder). The Gamma load always covers the cap so held positions beyond the base window keep being marked, exited, and swept. `max_hours_cap` is registered in the profile loader and added to the self-tuner's frozen-entry audit.
- **One bet per game + soccer under-4.5 priority**: same-event candidates collapse to a single pick before selection (`_dedup_same_event`): for soccer the **under-4.5-goals** market wins over everything else in the event (moneyline, specials — pattern `O/U 4.5` + `Under`, the only O/U line the exclusions allow); otherwise the highest-bid candidate is kept. The in-loop `EVENT_EXPOSURE_CAP` drops 2 → **1**, closing the path where two same-tick picks from one event could both execute. Regression tests for the ladder, the dedup priority (both orderings), and the cap.

- **Dynamic opportunity-spread sizing — 20% hard cap per bet**: `stake_pct` 0.30 → **0.20** in both profiles, and the per-bet target now spreads the available cash across the actionable opportunities (`cash/N`): a busy window funds every qualifying market instead of the first picks taking the full cap, while a slow market gives each bet the full 20%. The near-resolution boost (1.5× <30 min, 1.25× <1 h) scales the spread share but can never pierce the cap. The top-up cap follows `stake_pct`, so it tightens to 20% automatically (`_dynamic_stake_target` in `race_strategies.py`).
- **`oneHourPriceChange` logged, NOT gated**: a 1h flux entry gate was added and removed the same day (2026-06-10) — user decision: recently-moving markets are often exactly the ones converging toward resolution and must stay tradeable. No 1h knob exists; the `one_hour_change` value is logged in the forward-observation net so its edge contribution can be measured, and a test pins that no `oneHourPriceChange` value can exclude a market.
- **Top-up lane — depth-capped entries can be completed**: when a buy fills below its sizing target because the book was thin (e.g. $229 of a $379 PPI target), the market stays actionable and later ticks may buy more of the *same token*, averaging stake/shares/entry into the existing position. Hard bound: the position's total cost basis never exceeds the per-position cap (`equity × race_stake_pct`, ~30%, min'd with the ceilings when set) — the cap is what bounds the old "$45 → $4 in 22 ticks" averaging spiral that the blanket token-dedup used to prevent; with `race_stake_pct ≤ 0` top-ups stay disabled. Each top-up must re-pass all entry filters and the book-depth cap; it is exempt from the one-position-per-event guard (it grows that very position) and does not inflate the per-event exposure count.

### Fixed

- **CRITICAL: runaway duplicate orders on "delayed" in-play fills** (2026-06-15): an in-play BUY can return `status: "delayed"` (matching deferred) — success=true with an orderID but empty making/taking. The bot treated it as *not filled* (so no position recorded) but the order *did* settle on-chain, so `has_pending_token()` saw nothing and the bot **re-bought the same market every tick**, stacking duplicate orders until the wallet drained (live: ~$48 of duplicate "submission No" FOKs, $89 → $40, while the ledger showed one $4.30 position). Fix: an accepted-but-unfilled order in a *working* status (`delayed`/`live`/`pending`/`open`) is now recorded as a **pending order** so the dedup blocks the re-buy; `_sync_live_positions` promotes it to a real position once it settles and `_cancel_stale_pending_orders` (now run in the race tick) frees the token after the TTL if it never does. A killed FOK (`unmatched`) is still left unrecorded (safe to retry). Regression tests for delayed/live/killed.

- **Entertainment / "Divertissement" markets banned** (user 2026-06-14): awards (academy award, best picture, grammy, emmy, golden globe, palme d'or, tony award), box office / rotten tomatoes, music charts (billboard, spotify, album, streams), streaming (netflix, tiktok), social metrics (subscribers, followers), and movie/film/celebrity — no convergence edge, they jump on hype. Name-collision-safe terms only (e.g. "academy award"/"best picture" not bare "oscar"). The audit confirmed only one entertainment market ever traded (the MrBeast view bet, already banned), so zero winners are lost; the strategy's actual winning lanes (moneylines, geopolitics, AI-model markets, golf, WNBA) are pinned tradeable.

- **O/U 4.5 goal-total markets banned (data-driven)** (user 2026-06-14): a loss audit of the realized trades showed **O/U 4.5 Unders were 80% of all losses ($765 of $960)** and the three worst trades ever (Derry −$277, US-Paraguay −$266, FC Lahti −$194 — each bigger than total profit) — textbook gap risk (an Under at 0.94 craters to $0 on the goal that crosses the line). 4.5 was the only O/U line still allowed; now every O/U goal total (0.5–7.5) is banned. The dip double-down also skips excluded markets, so existing O/U 4.5 holds are never topped up.

- **YouTube view/subscriber-count markets banned** (user 2026-06-14, after losing a MrBeast view-count bet): `is_excluded_market` now blocks `youtube`/`mrbeast`/`mr beast` titles and a word-bounded `\bviews\b` view-count rule — "reviews" and "interviews" stay tradeable (no word boundary before the 'v'), pinned by tests. View totals have no convergence signal and jump unpredictably.

- **League of Ireland soccer banned** (user 2026-06-12): every Premier Division (Ireland) market carries the `irl1-` slug prefix — the whole championship is excluded (the question text has no league marker, so the slug is the identifier). Both live markets from tonight pinned as regression tests; other leagues' O/U 4.5 markets pinned as still tradeable.

- **Stock market re-banned outright** (user 2026-06-12, ending the same-day in-session experiment): all equities/indices/ETFs/price-threshold markets are excluded again, always — the session gate, the same-day window, and the 0.90 stock entry floor are removed; every detection pattern (tickers, companies, generic `(TICKER) … $`, weekly/touch) stays and now feeds the unconditional ban. Esports (LoL-only, live, ≥0.92) unchanged.

- **Esports narrowed to League of Legends ONLY + fast-lane entry floors** (user 2026-06-12, twice the same day): only LoL (`LoL:`) qualifies for the live-game lane — **Mobile Legends, Counter-Strike, Valorant, Dota, and every other title (incl. generic BO1/BO3/BO5 markers) are banned outright**, live or not (MLBB added to the recognition patterns; it previously matched nothing). **`Game Handicap:` / `Map Handicap:` markets banned outright** (the esports spread variant — "Game Handicap: HLE (-2.5) vs T1 (+2.5)" slipped past the `Spread:` pattern and was bought pre-game at 0.889). New per-lane entry floors: **esports ask ≥ 0.92, stocks ask ≥ 0.90** (`ESPORTS_MIN_ASK`/`STOCK_MIN_ASK` applied in `_build_eligible_candidates` on top of the global band). Test-pinned: live CS/Valorant/MLBB/unknown-BO3 banned, live LoL allowed, LoL at ask 0.90 rejected / 0.93 accepted, non-fast-lane keeps the 0.85 floor.

- **Fast-lane winner exit at 0.98 — esports + stocks** (user 2026-06-12): in-play esports and in-session stock books rarely print a 0.99 bid before the market closes, so winners there sat unsold until settlement risk crept back in. Esports and stock positions (`is_fast_lane_text` on question/slug) now trigger the resolved exit at a live-book bid ≥ **0.98** and the winner floor accepts 0.98 for them; every other lane keeps the strict 0.99 rule (test-pinned both ways).

- **Entry cap tightened to 12 h — nothing beyond, ever** (user 2026-06-12: "i need bets for today and max 4 6 8 12h"): `max_hours_cap` 24 → **12** in both profiles after the 24 h rung bought an overnight "Israel closes airspace by June 13" ~26 h before its end. Ladder: 4 → 6 → 8 → 10 → 12 and stops. A by-tomorrow market becomes tradeable only once it is within 12 h of its end (e.g. a June-13 daily from Saturday ~noon).

- **Daily-expiry fallback OFF — nothing beyond 24 h, ever** (user 2026-06-12, same-day revert of the end-of-tomorrow rung): the fallback bought ~36 h holds (Trump approval Jun 12, Israel airspace Jun 12) that sat overnight against the "resolve fast" thesis. `daily_expiry_fallback = false` in both profiles; the ladder is 4 → 6 → 8 → 10 → 12 → 24 h and stops. The knob and code stay for explicit re-enabling.

- **Tweet-count markets banned + stock-detection gaps closed** (2026-06-12): the bot bought "Will Elon Musk post 240-259 tweets from June 5 to June 12?" and "Will Airbnb, Inc. (ABNB) hit (LOW) $124 Week of June 8 2026?" on 2026-06-11 — tweet counts were never a banned category, and ABNB matched no stock pattern. Now: (1) any `tweet` market is banned outright; (2) `_STOCK_MARKET_RE` gains ABNB/Airbnb, UBER, Coinbase, PLTR, Robinhood/HOOD, plus a generic rule classifying any "(TICKER) … $" title as stock (the `$` requirement keeps "(GOP)" politics out); (3) **weekly "Week of" ranges and "hit (LOW)/(HIGH)" touch markets are banned outright**, session or not — a touch market can flip on any intraday print, there is no end-of-session convergence to ride. Both real markets pinned as regression tests.

- **One game = one bet, across event slugs** (2026-06-11): the PR #48 dedup keyed on `event_slug`, but Polymarket files one game under several events — the Mexico–South Africa moneyline (`fifwc-mex-rsa-2026-06-11`), the O/U 4.5 (`…-more-markets`), and the first-to-score special (`…-first-to-score`) — so the bot stacked **$958 across three positions on one game** in a single tick. Games are now identified by the date-truncated event slug AND the team names parsed from the question (`_game_keys`): same-game candidates collapse to one pick (under 4.5 preferred for soccer), an open position on any market of a game blocks every other market of that game across ticks (`_open_game_keys`), and the execution loop rejects same-tick repeats (`same_game_already_bet`). Mexico-trio regression tests included.
- **Auto-improve daily schedule disabled** (user request 2026-06-11): the 06:17 UTC cron is commented out in `.github/workflows/auto-improve.yml`; `workflow_dispatch` remains for manual runs.
- **Winner floor — resolved winners sell at 0.99, period**: after the universal sweep printed 0.97/0.98 exits (Spurs/Knicks, Iran-airspace — both from a pre-restart process running code older than the sweep `max()` fix), the 0.99 rule is now structural instead of configuration-deep: (1) `execute_live_sell` refuses any `race_big_win_resolved` / `resolved_market_sweep_win` order priced below 0.99 (`winner_floor`), and the race exit holds the position instead of writing it off; (2) `_sweep_sell_live` clamps its order to exactly 0.99; (3) the offline self-tuner's bounds for `race.resolved_exit_threshold` are pinned to (0.99, 0.99) so an auto-merged tuning PR can never lower the winner exit again. Regression tests for all three.
- **Trade journal now records `exit_price`**: `_append_trade_journal` never wrote the exit price, so every race exit landed as `exit: None` and the Telegram TRADES DU JOUR had to reconstruct the exit from the PnL. The realized price (stored in `current_price` by `record_live_exit`) is now journaled explicitly.
- **Resolved-exit now reads the LIVE order book — stale quotes held winners past 0.99**: the exit decision used Gamma's flipped market-level quote and the data-API `curPrice`, both of which lag the CLOB near resolution. Seen live 2026-06-10 on all three bots: Israel-airspace No had a real 0.99 book bid while the exit loop saw 0.95 and the winner was never sold. `_execute_race_exits` now probes the live CLOB best bid per open position (`live_best_bid` in `trading.py`, fail-open to the cached price when the book is unavailable) and decides + prices the sell off it. `resolved_exit_threshold` was dropped to 0.98 in the same change, then **reverted to 0.99 hours later** (user decision): live inspection of the Iran-airspace book showed the displayed "98¢" is the midpoint — the executable bid was 0.966 — and a near-settled bet pays 1.00 on-chain, so selling at 0.98 gives up real cents for no benefit. Regression tests in `tests/test_strategy.py` (live-bid override fires at a real 0.99 bid, a 0.98 bid holds, fail-open probe holds).
- **Winners-only sweep front-ran the 0.99 resolved-exit**: PR #29 raised `race_resolved_exit_threshold` to 0.99 but `_force_close_resolved_positions` kept its own `smart_resolved_exit_threshold` default of 0.97, so the sweep sold winners at 0.97 before the race exit could ride them to 0.99 (Spurs/Knicks O/U 196.5 on 2026-06-10, ~2¢/share left on the table). The sweep now uses the strictest configured threshold (`max` of both). Regression tests in `tests/test_resolved_sweep.py`.
- **Ledger books the true fill, not the price guard**: a filled BUY is now recorded with the actual USDC spent (`makingAmount`) and the real average fill price (`making/taking`) instead of the requested stake and the ask+tick price guard. Booking the guard overstated the entry (PPI 2026-06-10: 0.954/$229.04 booked vs 0.9496/$228.51 real), skewing the −25% SL trigger, the never-sell-below-entry floor, and the share count. Falls back to the request values when the response lacks fill fields.
- **FOK buys no longer bounce on thin books — stake capped to executable depth**: an all-in stake larger than the ask-side liquidity within the price guard made the exchange kill the entire FOK order (`FOK orders are fully filled or killed`), so the bot bought *nothing* — seen live 2026-06-10 when a $380.89 PPI buy bounced while the smaller bots filled instantly. `execute_live_trade` now reads the CLOB book and caps the stake at 90% of the executable ask depth (≤ max price guard) before sending; if even the minimum order can't be covered it rejects with `book_too_thin` and no order is sent. Fail-open when the book is unavailable. The remainder of the cash stays free for other markets (token-level dedup intentionally blocks averaging in later).
- **Already-held markets no longer burn pick slots**: the grinder selector returned only the top `race_max_orders_per_tick` candidates by score, *then* the execution loop skipped duplicates — so when the soonest-closing markets were lines on an event already held, every pick slot was wasted on guaranteed skips and the next-ranked actionable market was never attempted. Seen live 2026-06-10: four Spurs/Knicks O/U lines filled all 4 slots tick after tick while the 5th-ranked PPI market (taken immediately by the other bots) was never tried. Candidates whose token is open, whose order is pending, or whose event is already held are now filtered out *before* selection (`_actionable_candidates` in `race_strategies.py`); the in-loop guards remain as a safety net. Regression test in `tests/test_strategy.py`.
- **Gamma scans now paginate past the API's silent 100-row cap** (#30): the Gamma `/markets` endpoint truncates every response to 100 rows regardless of the requested `limit`, so every `scan_limit` above 100 was an illusion — the grinder scan saw ≤200 unique markets of the ~1,900 closing within its window (the 100 soonest-closing + the 100 highest-volume). `GammaClient.get_markets` now walks pages of 100 with `offset`, deduplicates by market id across pages, stops on a short page, and treats `limit` as a client-side ceiling. A page failure after the first returns the partial result instead of discarding it. All scan lanes benefit; exclusions (crypto, esports, …) are unchanged and still applied downstream.

## [2.2.0] - 2026-06-10

Winners ride to 0.99, the live report shows every full-size trade again, and the esports ban is airtight.

### Fixed

- **Esports ban gap — `LoL:` titles slipped through**: Polymarket titles League of Legends markets `LoL: <team> vs <team> - Game N Winner`, which matched neither `league of legends` nor any other esports pattern; the bot bought $351 of `LoL: FENNEL vs KT Rolster` on 2026-06-10. Added `lol:` (question) and `lol-` (slug) to `is_excluded_market`, with unit tests for the esports ban.
- **LIVE REPORT — big wins missing from `TRADES DU JOUR`**: `load_todays_trades` dropped every closed trade with `cost_basis > $100` (a 2026-06-01 guard against swept wallet-level positions, added when the bankroll was ~$50). With percentage sizing the normal stake is now ~$350, so all full-size wins (Nigeria, Málaga–Las Palmas, Orebro on 2026-06-10) silently vanished from the report and from `Gains du jour`. The dollar cap is removed; dedup + tracking-start filtering already cover the original problem. Regression test in `tests/test_live_analyst.py`.

### Changed

- **Bot 2 reverted to the grinder strategy — copy lane removed** (user 2026-06-16): bot 2 no longer runs the whale copy-trading lane; it runs the same grinder strategy/controls as bot 1 (`grinder_b.toml`, identical to `grinder.toml` apart from the per-machine bankroll baseline, via `run_live_b.sh`). Removed the copy-only artifacts: `scripts/run_live_copy_b.sh`, `configs/profiles/smart_b.toml`, and the `CopyLaneProfileTests`. The dormant `smart_whale_*` config keys remain (default-off, used by no profile).

- **Per-bet cap 10% → 15%** (user 2026-06-14): `stake_pct` 0.10 → **0.15** in both profiles to size up at the 89% live win rate; fresh entries still open at the 5% `initial_stake_pct`, so the dip double-down now has headroom to fill toward 15%. Self-tuner `race.stake_pct` upper bound raised (0.05, 0.10) → (0.05, 0.15).

- **Dip double-down now gated by a 0.60 "alive" floor, not an 8¢ max-dip** (user 2026-06-14, Sweden-Tunisia Under): the double-down fires whenever a held position's ask has dipped below entry AND is still ≥ `race_double_down_min_price` (0.60) — the deterministic proxy for "the bet is still going well / few goals" (the bot has no live-score feed). The old `max_dip` (8¢) cap and the 0.85 band floor are replaced by the single 0.60 alive-floor; below it the bet has turned and is never topped up. Still once per position, never past the 10% cap.

- **Entry window = game starts OR market closes within 4h** (user 2026-06-14): `_build_eligible_candidates` now keeps a market only when its `gameStartTime` is within the next `max_hours` OR its `endDate` is — a game already in progress that doesn't close inside the window is dropped. The dynamic widening ladder stays disabled (`max_hours_cap=0`).

- **Resolved-exit / winner floor reverted to 0.97** (user 2026-06-14, "sell at 0.97 as we had before"): `resolved_exit_threshold` 0.99 → **0.97** in both profiles; the `execute_live_sell` winner floor, the `_sweep_sell_live` clamp, and the self-tuner pin all move to 0.97 (one flat floor — the 0.99 + 0.98-fast-lane scheme is gone). The live-book bid probe is unchanged.

- **Soccer under-4.5 priority dropped** (user 2026-06-14): the one-bet-per-game dedup (`_dedup_same_game`) no longer prefers the under-4.5-goals market — it simply keeps the single highest-bid (most-resolved) candidate per game, like every other sport. One bet per game is unchanged. Dead `_is_under_45_candidate`/`_UNDER_45_RE` removed.

- **Per-bet cap 20% → 10% + entry window hard-capped at 4h** (user 2026-06-14): `stake_pct` 0.20 → **0.10** in both profiles (the self-tuner's `race.stake_pct` upper bound is pinned to 0.10 so an auto-PR can never raise it back); and `max_hours_cap` → **0**, which makes `_entry_window_ladder` return a single `[4h]` rung — the 6/8/10/12h widening is OFF and nothing beyond 4h is ever scanned or entered.

- **Resolved-exit raised 0.97 → 0.99** (`resolved_exit_threshold` in both `grinder.toml` and `grinder_b.toml`) — winners ride closer to settlement before the bot realizes them; the winners-only sweep follows the same threshold. Fallback to 0.98 if 0.99 rarely fills before resolution.
- **LIVE REPORT — `POSITIONS OUVERTES`**: each open position now shows a 🟢/🔴 light (winning vs. losing on unrealized P&L) and a **`▶️ Voir le match`** link to the Polymarket event page (`eventSlug` from the Data API). The section header carries the **overall unrealized P&L** (`🟢/🔴 ±$X`) next to the count.

## [2.1.0] - 2026-06-09

Grinder hardening: real risk controls + safer exits, crypto/esports banned, per-bot resets, and a documentation pass.

### Added

- **Controlled stop-loss** (`race_stop_loss_confirmed`): sells at **−25%** only after the loss persists for **3 consecutive ticks** (`sl_pct`, `sl_confirm_ticks`), so a one-tick thin-book phantom bid can never dump a winner. It is the only path exempt from the never-sell-below-entry floor.
- **Never-sell-below-entry floor** in `execute_live_sell` — every exit except the confirmed SL holds a losing position to natural on-chain resolution.
- **Per-machine baseline** (`data/starting_cash.txt`, gitignored) so each of the 3 bots keeps its own report baseline without touching the shared profile; read by `live_analyst` and the Telegram all-time line.
- **`scripts/fresh_start.py`** — reset that wipes closed-trade history but **keeps open trades** (re-synced on start) and sets the per-machine baseline.

### Changed

- **All crypto banned** (bitcoin/btc/ethereum/solana/dogecoin/xrp/… + Up/Down) and **esports banned** (CS/valorant/LoL/dota/… + BO1/BO3/BO5) in `is_excluded_market`. `btc_edge` lane disabled.
- **Daily drawdown halt disabled** across all launchers (`POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0`) — the per-trade confirmed SL is the risk control.
- **LIVE REPORT trimmed**: equity, P&L since start, **total trades + win rate**, open positions — no per-trade lists, no `💓 Bilan` heartbeat, no BUY/SELL alerts. All-time P&L is now **equity − baseline** (not realized-from-entry), so a re-based account never shows phantom losses. Cadence configurable via `LIVE_ANALYST_CYCLE_SECONDS` (startup + interval + daily 10:00 ET).
- Documentation rewritten to match the live strategy (`README.md`, `CLAUDE.md`, `.claude/skills/polymarket-bot/SKILL.md`).

### Fixed

- **Expiry no longer force-closes a still-open market** — confirms via a live lookup and uses `gameStartTime` (Gamma `endDate` is often set before kickoff), so winning favorites are no longer dumped pre-game. Genuinely-resolved losers are written off locally ~8 h after expiry, no order.
- Removed the **EOD flatten** and the **loss-sweep** that dumped winning Unders at $0.01–$0.46 on thin live-game books; the universal sweep now realizes **winners only** (≥ 0.97).

## [2.0.0] - 2026-06-05

Official "Grinder V1" release. Heavy-favorite, ride-to-resolution strategy with a deterministic live trade path.

### Added

- **Autonomous self-improvement engine** (`scripts/auto_improve.py` + `.github/workflows/auto-improve.yml`): opt-in loop that uses the Claude Code CLI to tune the live strategy's **exit/sizing** knobs and ship the change as an auto-merged PR. Entry/bet-selection is frozen and a stop-loss can never be introduced; gated by the unit-test suite and green CI. Off by default. See `docs/AUTONOMY.md`.
- **Hourly LIVE REPORT** (`scripts/live_analyst.py`): per-bot Telegram report on startup, every 1 h, and a daily 10:00 ET fire. Shows equity since start, every closed trade with **entry → sell prices**, and open positions. French localisation with UTF-8-safe translation.
- Three-bot live deployment (Grinder Bot 1/2/3), each with its own wallet, ledger, and scoped analyst.

### Changed

- Entry band widened to **0.85–0.97**; `resolved_exit_threshold` held at **0.97**.
- Per-bot all-time baselines re-based after manual deposits; "depuis le début" % now reflects deposited capital.
- Documentation refreshed (`README.md`, `CHANGELOG.md`) to match the live config.

### Fixed

- French accents no longer mojibake on Telegram (force UTF-8 decode of the translation CLI output — fixes Windows cp1252 hosts).

## [1.5.0] - 2026-05-28

Grinder strategy tuning: faster exits, bigger wins per trade, more opportunities per tick.

### Changed

- `resolved_exit_threshold` 0.99 → **0.97** — exits positions as the market trends toward resolution, eliminating `race_expired_close` outcomes on markets that close without ever printing 0.99.
- `tp_pct` 0.06 → **0.07** — larger take-profit for entries in the lower band (≤ 0.906) where the TP fires before the resolved exit.
- `max_orders_per_tick` 1 → **2** — deploys both 50%-stake slots in a single tick when two eligible markets exist simultaneously.
- `max_hours` 3.0 → **4.0** — pushes the time-to-close window to the 4h-only rule limit for more eligible markets per tick.
- `max_hold_hours` 3.5 → **4.5** — backstop aligned with the widened entry window.
- Docs overhaul: README, STRATEGIES.md, AGENTS.md, SECURITY.md, CONTRIBUTING.md, and both SKILL files updated to reflect the grinder-only live stack and current config values.

## [1.4.0] - 2026-05-24

Fresh leaderboard restart: restored all archived profiles, auto-discover launcher, bash 3.2 compat fix, lenient kill thresholds.

### Added

- Restored 88 previously-archived profiles from `configs/profiles/_archived/` back to active — 95 total profiles in the dry race.
- `scripts/run_all.sh` and `scripts/run_both_dry.sh` now auto-discover all `configs/profiles/*.toml` instead of a hardcoded list of ~9–50. Skips special profiles (`copy-wallet`, `live-90`).
- Bash 3.2 (macOS default) compatibility: replaced `mapfile` with POSIX `for` loop in both launcher scripts.

### Changed

- Analyst kill thresholds (`scripts/dry_analyst.py`) relaxed to let strategies build longer track records before being culled:
  - `KILL_AUTO_MIN_TRADES`: 8 → 25, `KILL_HUMAN_MIN_TRADES`: 20 → 50
  - `KILL_ROI_THRESHOLD`: -10% → -25%, `KILL_WR_THRESHOLD`: 40% → 30%
  - `KILL_EQUITY_FLOOR_PCT`: 50% → 30%

### Fixed

- `scripts/run_all.sh` silently launched 0 dry bots on macOS because `mapfile` is a bash 4+ feature and macOS ships bash 3.2. Replaced with a POSIX-compatible array append loop.

## [1.3.0] - 2026-05-16

Operational release: shared HTTP cache, unified live+dry launcher, live profile switched to `whale_entry_detection` on a $45 bankroll. No public-API breakage.

### Added

- `scripts/run_all.sh` — single launcher that pre-warms the HTTP cache, boots the live bot (`whale_entry_detection`), launches auto-discovered dry profiles (now 95 via glob, was ~50 curated) at a 10min tick, spawns the dry-analyst + live-analyst + leaderboard sidecars, and runs a background cache re-warmer every 8 min so live + dry never hit a cold cache.
- `scripts/cache_warmer.py` — pre-fetches leaderboards (3 windows × 8 categories × 4 limits) and the top wallets' recent trade histories into `data/cache/http/`. Used both at startup and periodically by the re-warm loop.
- Shared HTTP cache in `polymarket_bot/smart_money.py:_get_json` — sha1-keyed disk cache at `data/cache/http/` with a 600s TTL (override via `POLYMARKET_HTTP_CACHE_TTL_SECONDS`). Drops the API load of a 50-bot swarm from ~2,500 calls/min to ~33.
- `scripts/winner_consistency.py` — sliding-window analyzer (30min windows over 8h lookback) for ranking strategies window-by-window.
- `scripts/live_analyst.py` — executive-summary live report sidecar (30 min interval): open positions w/ entry→current→PnL, top closed trades, dry-twin comparison, dry top 5 with a star marker on the live profile.

### Changed

- Live profile switched to `whale_entry_detection` (race mode, no leaderboard fetch — immune to data-api 429s).
- Live bankroll = $45 USDC. Sizing: 10% per trade (~$4.50 base), max position $9, 3 min open positions, 5% cash floor, 4h hard cap. Exits: TP +25% / SL -25% / resolved at bid ≥0.97 / near-expiry flush at 5min.
- Dry race trimmed from "all 195 profiles" to ~50 curated representatives covering every thesis family. (Replaced in 1.4.0 by auto-discover of all 95 restored profiles.)
- Dry bots are silent on Telegram BUY/SELL alerts via per-subshell env vars in `run_dry_bot()` — only the live bot speaks.
- Live analyst now exports `POLYMARKET_PROFILE_LABEL` BEFORE the sidecar spawns (it previously logged `(unknown)` in reports).
- Dry-analyst `_pick_favorite` says "Top of N profitable strategies" when N > 1 (was always "Only profitable", which lied when several were positive).

### Fixed

- `live_available_balance` fallback was returning a stale `$29.90` from `assume` when the pUSD RPC failed, even after real cash had been spent on live BUYs. Now reads ledger cash and caps by `assume - sum(open_positions_cost)`. RPC-failure log throttled to once per 5 min.
- Per-position sizing: `ceiling = max(ceiling_usd, total_equity * pct)` was unconditionally `max(...)` — allowed a $25 BUY on a $29.90 bankroll. Now defended in the profile via `max_position_ceiling_usd` + `max_trade_usd` absolute cap.
- Telegram leaderboard rendering: removed all `_md_escape` calls and `\\!` MarkdownV2 literals; plain text everywhere, truncated to top 15 + bottom 5 (was exceeding the 4096-char message cap with 100+ strategies).
- `_default_transport` retries with `parse_mode` stripped on HTTP 400, so MarkdownV2 failures no longer silently swallow alerts.
- `load_live_snapshot` now prefers `current_price × shares` for equity (falls back to `size_usd → notional_usd → stake → cost_basis`). Previously rendered `$4.91` (cash only) when live-synced positions lacked `size_usd`.
- Analyst journal counter accepts both `realized_pnl_usd` (sweep) and `realized_pnl` (race/smart_money/news) — previously showed 100% win rate everywhere because only sweep entries populated `realized_pnl_usd`.
- `cache_warmer.py` invoked via `uv run python` (plain `python3` doesn't see the venv → `ModuleNotFoundError: No module named 'dotenv'`).
- `scripts/run_all.sh`: dropped `set -u` (crashed on harmless unset vars), removed EXIT from the trap (only INT/TERM now), `cleanup()` made idempotent via `CLEANED_UP=1` — fixes the bug where one unset-var failure tore down all 50+ bots through the EXIT trap.

## [1.2.0] - 2026-05-08

Documentation refresh release. All Markdown files (`README.md`, `CLAUDE.md`, `CODEX.md`, `AGENTS.md`, `docs/AUTONOMOUS_STRATEGY.md`, and the structured `.claude/` and `.codex/` skill files) are now in sync with the live `scripts/run_live_70.sh` configuration and the multi-level exit waterfall introduced in 1.1.0.

### Changed

- README env-var examples updated to the current live values: `AUTO_INTERVAL_SECONDS=10`, `NOISE_FALLBACK_MAX_TRADES_PER_TICK=8`, `NOISE_FALLBACK_MAX_TRADE_USD=15`, `NOISE_FALLBACK_CASH_PRESSURE_PCT=0.25`, and the +25% take-profit tier added to the default ladder (`0.25:0.15,0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15`).
- Take-profit ladder description updated everywhere (README, CLAUDE.md, CODEX.md, AUTONOMOUS_STRATEGY.md) to list five tiers: +25% / +50% / +100% / +200% / +300% with partial sells of 15% / 25% / 50% / 25% / 15%.
- Multi-level exits sections now mention the resolved-market exit (force-close at bid ≥ 0.97) and the auto-cancel-resting-order behaviour on `balance is not enough` SELL rejections.
- `docs/AUTONOMOUS_STRATEGY.md` exit waterfall renumbered to 9 steps to include the resolved-market exit and the parallelised cohort-trade fetch.

### Notes

No code changes in this release. The runtime, tests (54), and live config script are byte-identical to 1.1.0.

## [1.1.0] - 2026-05-08

Performance and reliability release on top of 1.0.0. Adds the +25% take-profit tier, parallelises the cohort-exit check, and fixes two production bugs (counter-trades on the same binary market, tick crashes on sell errors).

### Added

- `+25%` take-profit tier (sells 15% of initial shares) so positions that peak in the 25-50% range and round-trip to flat still book realised P&L. Default ladder is now `0.25:0.15,0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15`.
- Resolved-market exit (`POLYMARKET_SMART_RESOLVED_EXIT_THRESHOLD`, default 0.97) — force-closes positions when the live bid is at or above the threshold so terminal-price winners no longer pin capital until the 24h max-hold cap.
- `cancel_active_orders_for_token` on the trading session — when a SELL is rejected with "balance is not enough", the bot now lists active CLOB orders, cancels the resting one on that token, and retries on the next tick. Removes the death-loop where stuck sells caused tick crashes.
- Cash-pressure trigger on the noise fallback: the lane now also fires when cash share of equity exceeds `POLYMARKET_SMART_NOISE_FALLBACK_CASH_PRESSURE_PCT` (default 0.25), even when open-position count is above `MIN_OPEN_POSITIONS`. Prevents idle cash piling up during dry hours.

### Changed

- Cohort-exit check parallelised through the same `ThreadPoolExecutor` used by the smart-money trade fetch (default 24 workers). Tick latency dropped from 20-30 seconds to 8-12 seconds with 30+ open positions.
- Tick interval lowered from 20 seconds to 10 seconds now that cohort-exit is no longer the bottleneck. Faster cash redeployment after sells, more opportunities to catch fresh signals.
- Noise fallback throughput: `MAX_TRADES_PER_TICK` raised from 4 to 8 and `MAX_TRADE_USD` from $10 to $15. Idle cash now drains in 2-3 ticks instead of 7+.
- Noise fallback selection now ranks candidates by total smart-money flow on the token (informed noise) instead of generic Gamma top-scorers.
- Multi-period leaderboard fetch (`POLYMARKET_SMART_TIME_PERIODS=MONTH,ALL`) — long-term consistent winners join the cohort alongside recent monthly leaders.

### Fixed

- **Counter-trade on the same binary market** — the previous `has_open_position(market_id)` dedupe failed when a position synced from the Data API used `conditionId` as `market_id` while a fresh candidate from the Gamma scan used Gamma's market id. The bot was opening YES and NO of the same market. Fixed by switching the event-level dedupe (`has_open_event_position`) to use `event_slug` for all markets, not just sports. `event_slug` is consistent across both APIs.
- **Tick crashes on sell-side API errors** — `_execute_sell_strategy` only caught `ValueError`, so any other exception (notably `PolyApiException` for "balance is not enough" / 4xx) bubbled up and killed the entire tick. Now catches `Exception`, logs the failure to the exit report, and continues with the next position.
- **Noise fallback gated on smart-money idle** — the early return in `smart_money_once` skipped the noise lane whenever any smart-money trade had executed in the same tick. Now noise fallback runs unconditionally if enabled, with the per-iteration safety checks preventing duplicates of what smart-money just bought.
- **Reverse-lookup HTTP 414** — Gamma's `/markets?clob_token_ids=...` was being called with 100 token-ids in one URL, blowing past the URL length limit. Now chunked at 20 ids per request with response dedupe.
- **Coinbase 503 killing the BTC edge tick** — added retry with exponential backoff and a fallback to the public `api.coinbase.com/v2/prices/BTC-USD/spot` endpoint when the exchange API degrades.

### CI

- `requirements.txt` and `pyproject.toml` switched from the unpublished `py-clob-client-v2` to the actually-PyPI-available `py-clob-client>=0.21.0`.
- Top-level SDK import in `trading.py` made lazy via `_load_clob_types()`, so tests load cleanly even if the SDK isn't on the PATH.
- The lint job is now advisory (`continue-on-error`) so ruff warnings don't block CI.

## [1.0.0] - 2026-05-08

First stable release. The strategy, sizing, exits, journal, auto-tuner, BTC edge, and noise fallback are now in production shape and have been validated on a real live bankroll. CI is green on Python 3.10 / 3.11 / 3.12.

### Added since 0.1.0

- `+50%` take-profit tier (sells 25% of initial shares) so partial winners that don't reach `+100%` still book realised P&L.
- Holding-time cap (`POLYMARKET_SMART_MAX_HOLD_HOURS`, default 24h) — force-closes stale positions when no other exit rule has fired so capital can be redeployed on fresh signals.
- Bankroll-aware position ceiling (`POLYMARKET_SMART_MAX_POSITION_CEILING_PCT`) — the absolute USD ceiling becomes a floor; the larger of `static $` and `equity * pct` wins, so high-conviction signals scale up as the bankroll grows.
- Cash-pressure trigger on the noise fallback so the lane fires when cash share of equity exceeds the configured threshold even when open positions are above `MIN_OPEN_POSITIONS`.
- Smarter noise fallback selection: candidates whose token has had any smart-money activity in the lookback are preferred over generic Gamma top-scorers — informed noise rather than random.
- Multi-period leaderboard support (`POLYMARKET_SMART_TIME_PERIODS`) — fetches `MONTH` and `ALL`-time leaderboards together so long-term consistent winners join the cohort alongside recent monthly leaders.
- Coinbase BTC client now retries on 5xx / 429 / network blips with exponential backoff and falls back to the public `api.coinbase.com/v2/prices/BTC-USD/spot` endpoint when the exchange API is degraded.
- Repository polish: `Makefile` with the common dev targets, module-level docstrings on every Python file, refreshed `AGENTS.md` and `docs/AUTONOMOUS_STRATEGY.md`.
- CI install fix: switched runtime dependency from the unpublished `py-clob-client-v2` to the actually-PyPI-available `py-clob-client>=0.21.0`, with a lazy `_load_clob_types()` helper inside `trading.py` so tests load cleanly without the SDK on PATH.

### Changed since 0.1.0

- Lint job is now advisory in CI so ruff warnings on existing code don't block the test job from going green.
- Default take-profit ladder now includes the `+50%` tier: `0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15`.
- Live script (`scripts/run_live_70.sh`) bumped to `POSITION_PCT=0.18`, `MAX_POSITION_CEILING_USD=150`, `MAX_POSITION_CEILING_PCT=0.30`, `CASH_FLOOR_PCT=0.05`, `MIN_OPEN_POSITIONS=7`, `MAX_HOLD_HOURS=24`, and the multi-period leaderboard `MONTH,ALL`.

## [0.1.0] - 2026-05-08

First public release.

### Strategy

- Smart-money copy-trading on Polymarket with multi-wallet consensus, configurable freshness window, and strict execution filters (absolute spread, relative spread, chase premium, price band).
- Three-pass scan per tick: strict → relaxed (consensus floor relaxed) → deep fallback (consensus=1, looser filters).
- Reverse-lookup of high-flow tokens missed by the initial Gamma scan, batched at 20 token-ids per request.
- Parallel trade fetching per wallet (24 workers default) — tick latency in the trade-fetch phase drops from minutes to ~15 seconds.
- Trader filters: PnL, volume, and ROI floors against the monthly leaderboard.

### Sizing

- Percentage-of-bankroll sizing with conviction multipliers (0.55× for crypto-micro up to 2.5× for very-high-conviction 5+ wallets at $5k+ flow).
- Cash floor target (5%) with dynamic per-slot redistribution to drive ~95% deployment.
- Per-position ceiling: `max(static, equity × pct)` so the cap scales with the bankroll.

### Exits

- Take-profit ladder with four tiers: +50% / +100% / +200% / +300%.
- Trailing stop arms at +25% peak, exits on 50% giveback while still positive.
- Peak-protect arms at +100% peak, exits below +40%.
- Stop-loss at -40% after a 15-minute minimum hold age.
- Cohort-sell exit with active SELL detection from the entry wallets.
- Cohort-silent exit when no cohort wallet has re-bought within the lookback window.
- Maximum hold-time cap (24h) — force-close stale positions to redeploy capital.
- Near-expiry positive-PnL exit.

### Auto-tuner and journal

- Persistent JSONL trade journal at `data/trade_journal.jsonl` with full entry-signal metadata and exit PnL.
- Defensive auto-tuner reads the journal each tick and applies bounded overrides to `data/strategy_overrides.json` once 30 closed trades are recorded.
- `journal-stats` CLI for breakdown by category, consensus, exit reason, and entry-price bucket.
- `tune-strategy` CLI to run the tuner manually.

### Adjacent strands

- Integrated BTC edge: Black-Scholes-from-volatility model runs after every smart-money tick with exponential retry and a fallback to the public `api.coinbase.com` spot endpoint when `api.exchange.coinbase.com` returns 5xx.
- Noise fallback: up to 4 trades of $10 per tick when no smart-money signal qualifies AND (open positions below target OR cash share above 35% of equity). Tagged in the journal so the cost can be measured.

### CLI

- 6 commands: `auto-loop`, `dashboard`, `journal-stats`, `tune-strategy`, `bootstrap-creds`, `reset-ledger`.

### Project hygiene

- MIT license.
- `pyproject.toml` with proper metadata, console-script entry point, and ruff lint configuration.
- GitHub Actions CI: unittest on Python 3.10/3.11/3.12 plus ruff lint on every push.
- `.editorconfig`, hardened `.gitignore`, `CONTRIBUTING.md`, `SECURITY.md`, `.env.example`.
- Structured skill definitions for Claude Code (`.claude/skills/polymarket-bot/SKILL.md`) and Codex (`.codex/skills/polymarket-bot/SKILL.md`).

### Safety

- No LLM call in the trading loop.
- No ability for the bot to commit or push source code.
- The bot does not modify itself at runtime; strategy adjustments are auditable data files, not code edits.
