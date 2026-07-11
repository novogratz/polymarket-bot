# Claude Code Guide

Claude Code entry point for the Polymarket bot. See also the structured skill in `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

The live trade loop is **fully deterministic — no LLM in the scanning or trade-selection path.** The only sanctioned LLM use is the *offline* `auto_improve` self-tuner (see Safety), which never touches the live loop.

## New machine / fresh account setup

1. **Install uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh` → open a new terminal.
2. **Install v2 SDK**: `uv add py-clob-client-v2` — required since Polymarket CLOB v2 (old SDK gives `order_version_mismatch`).
3. **Create `.env`** from `.env.example`. Critical fields:
   - `POLYMARKET_SIGNATURE_TYPE=3` — all new accounts (2026+) use the deposit wallet flow (POLY_1271), not POLY_PROXY (type 1).
   - `POLYMARKET_FUNDER_ADDRESS` — your wallet address as shown on polymarket.com profile page.
   - `POLYMARKET_PRIVATE_KEY` — your EOA private key (the key that controls the deposit wallet).
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID_LIVE` — create bot via @BotFather, get chat_id from `getUpdates` after messaging the bot.
4. **Generate API credentials**:
   ```bash
   uv run python -c "
   from py_clob_client_v2.client import ClobClient
   c = ClobClient('https://clob.polymarket.com', chain_id=137, key='<PRIVATE_KEY>', signature_type=3, funder='<FUNDER_ADDRESS>')
   creds = c.create_or_derive_api_key()
   print('KEY:', creds.api_key); print('SECRET:', creds.api_secret); print('PASS:', creds.api_passphrase)
   "
   ```
5. **Approve CLOB allowance** (first time only) — `update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))` with the creds above.
6. **Make one manual trade on polymarket.com** — new accounts must place at least one UI trade to register the maker address with the CLOB, else API orders fail with `maker address not allowed, please use the deposit wallet flow`.
7. Run the bot (see Launch).

## Strategy — `grinder` (race mode) — WEATHER-ONLY

Buy a heavily-favored binary outcome near its resolution and **ride it to resolution**. The edge is the implied-probability gap between the entry price and a near-certain outcome settling at 1.0. Source of truth: `configs/profiles/grinder.toml` (bot 1) and `configs/profiles/grinder_b.toml` (bots 2 & 3) — keep their strategy keys in sync. All three bots run this same grinder strategy. Selector `select_grinder` in `polymarket_bot/race_strategies.py`.

**WEATHER-ONLY lane (user 2026-07-06, "put bot 1 to the same strategy as bot 2 which is weather only bets"):** `weather_only = true` (env `POLYMARKET_RACE_WEATHER_ONLY`) in BOTH profiles restricts entry selection to ONLY weather / temperature markets (`is_weather_market` in `models.py` — temperature, °C/°F, weather, rainfall, snowfall, high/low temp) and bypasses the normal weather ban. Every non-weather market (sports, elections, crypto, …) is dropped at entry selection; exits/sizing are untouched. Ported from `kzer_windows` (bot 3's 2026-06-23 experiment). The entry window is widened to 24 h because weather markets resolve end-of-day (~22–46 h out) — a 4 h window yields zero weather candidates.

**Entry** (`_build_eligible_candidates`):
- **Weather / temperature markets ONLY** (`weather_only`, see above).
- price (ask) ∈ **[0.80, 0.94]** with an absolute **hard cap 0.96** (`race_max_price_hard_cap`, user 2026-06-21 v4 — 0.97/0.98/0.99 never tradeable), **game STARTS or market CLOSES within ≤ 24 h** (weather-only lane 2026-07-06; was 4 h since 2026-06-14). The dynamic widening ladder is disabled (`race_max_hours=24`, `race_max_hours_cap=0` → single `[24h]` window).
- **Per-lane entry floor** (above the global 0.80): **soccer/sport "Will <X> win on <date>?" moneylines ≥ 0.92** (`SOCCER_MONEYLINE_MIN_ASK`, user 2026-06-17). Moneylines gap catastrophically on a single goal: EVERY moneyline loss in the realized history entered at ≤ 0.90 (0.85, 0.86, 0.867, 0.895, 0.90); the 0.90+ band has ZERO losses across 29 trades. The SL can't protect a goal-gap (Difaâ "No" 0.89 → 0.02), so the control is at entry — both Yes and No sides of such a market are floored (`_is_soccer_moneyline_text`).
- **One bet per GAME** (`_dedup_same_game` + `_open_game_keys` + `EVENT_EXPOSURE_CAP=1`): a game is identified by its date-truncated event slug AND the team names parsed from the question — Polymarket files one game under several events (moneyline / `-more-markets` / `-first-to-score`), which let $958 stack onto Mexico–South Africa on 2026-06-11. Same-game candidates collapse to a single pick before selection, an open position on any market of a game blocks all its other markets across ticks, and an in-loop backstop rejects same-tick repeats. The single best (highest-bid) candidate per game is kept (the soccer under-4.5 priority was dropped 2026-06-14 — just the best bet per game).
- spread ≤ 4¢, liquidity ≥ $250, 24 h volume ≥ $1000 (v4, user 2026-06-21)
- **Resolution-safety filter (v4, ALWAYS-ON — `race_min_resolution_clarity = 60`):** skips markets with subjective / ambiguous settlement wording (judges' discretion, "deemed", "disputed", "considered", "to be determined", …) via `forecast.resolution_clarity`. A clean objectively-resolvable market scores 100; one strong subjective marker drops below 60. Needs NO history, so unlike the EV/quality gates it is the one structural protection that **stays on under `unban_all_markets`**.
- **Forecasting EV/quality gates (v4, OPT-IN — default OFF):** the `forecast.py` model calibrates a favorite's win probability per (category, price-bucket) from realized history (shrunk toward the prior = overall win rate); `edge = predicted − ask`. When `race_min_edge > 0` it drops sub-edge outcomes; when `race_min_quality_score > 0` it drops low-`quality_score` ones. Both default 0 (need history to calibrate — enabling on a fresh bot would starve it). `_run_race_tick` builds the calibration context per tick (fail-open). `pmbot journal-stats` shows `by_v4_price_bucket` + `v4_performance` (Sharpe / profit factor / max DD / promotion gate: ≥500 trades & ROI ≥5%). The daily/weekly drawdown halts + large-loss pause were intentionally NOT built (user 2026-06-21 "no pause halts").
- **No price-movement gates** (removed 2026-06-10): the >10% day-change gate, the −5% day-momentum floor, and the short-lived 1h gates are all gone — recently-moving markets stay tradeable (they are often the ones converging toward resolution). Both day and 1h values are logged in the forward net only; tests pin that neither can ever exclude a market.
- The scan paginates the Gamma API past its silent 100-row cap (~1,000–2,000 raw markets/tick) and held/pending/capped markets are removed **before** the pick truncation (`max_orders_per_tick = 12` in v4 — maximize bets per tick) so they never burn slots.

**Execution (2026-06-10):** FOK BUY with ask+1-tick guard, stake capped at 90% of the executable ask depth (no more FOK kills on thin books), true fill (`making/taking`) booked to the ledger. With v4 fixed $5 sizing each entry targets exactly $5 and there is no double-down/top-up; one position per event.

**Sizing (FULL-DEPLOY + DIVERSIFICATION CAP — user 2026-07-09/11):** `full_deploy = true` (`POLYMARKET_RACE_FULL_DEPLOY`) in BOTH profiles. Each tick spreads **ALL available cash across the actionable picks** (`cash / N` per bet, no near-resolution boost), bounded by the **diversification cap** (user 2026-07-10 "positions at $90 when bankroll total is $200 is not acceptable… take more positions… diversifying between the different bets weather at different locations"): `full_deploy_max_position_pct = 0.05` — **no position may exceed 5% of equity** (floored at $5 for Polymarket's minimum; 0 = uncapped). All three sizing functions route through `_full_deploy_cap_usd`, so the top-up lane also stops at the cap — leftover cash from depth-capped fills keeps flowing into held markets up to 5%, then waits for NEW distinct markets (different cities) rather than piling on. Diversification wins over strict 100% deployment. `cash_floor_pct = 0` (no reserve). Worst-case loss on a single market ≈ 5% of equity. `full_deploy` OVERRIDES `race_fixed_stake_usd`; the retired v4 fixed-$5 mode (user 2026-06-21) is a one-line rollback (`full_deploy = false`, `fixed_stake_usd = 5.0`). The legacy `stake_pct`/`initial_stake_pct` Kelly knobs stay ignored. **Dip double-down remains DISABLED** (`race_double_down_enabled = false`) — the filter-gated top-up lane is the only way a position grows. `FullDeploySizingTests` pin the behavior. See `docs/STRATEGIES.md`.

**Exits** (`_execute_race_exits`):
- **Resolved-exit** — sell at **live CLOB book** bid ≥ a **dynamic per-position threshold** = `min(0.99, max(resolved_exit_threshold, entry + race_min_profit_margin))` (2026-06-15). **v4 (user 2026-06-21, "sell at 0.99 as well"): `resolved_exit_threshold = 0.99`**, so EVERY winner exits at a real 0.99 bid (the fast-lane 0.98 downgrade is removed). Above 0.99 it rides to settlement at 1.00. The exit probes the live book per position (`live_best_bid` in `trading.py`); the winners-only sweep applies the same per-position floor. Probe fail-open → cached price.
- **Controlled stop-loss — −30%, confirmed over 3 consecutive ticks, SPORT MONEYLINES ONLY** (`sl_pct=0.30`, `sl_confirm_ticks=3`, min age 5 min; gate `_is_soccer_moneyline_position`). A one-tick thin-book phantom bid can never trigger it; the loss must persist. The gate matches "Will <X> win on YYYY-MM-DD?" Yes/No bets and **excludes** politics/elections and awards (exclusion model since 2026-06-16 — any soccer club passes regardless of league slug, after América FC rode 0.88→0.30 with no SL because its slug lacked a hardcoded league keyword). O/U totals, elections, and everything else never stop out. Tagged `race_stop_loss_confirmed`.
  - **Anti-gap floor (`sl_min_exit_price=0.50`, user 2026-06-17):** the confirmed SL only EXECUTES while the live bid is still in orderly-decline territory (≥ 0.50). A moneyline that has gapped below it is a goal-crash that mean-reverts — Difaâ "No" went 0.8949 → 0.02, the SL sold the bottom at 2¢, then the opponent won so "No" **resolved to 1.0**: a +$2.55 winner booked as a −$21.25 loss. Below the floor the position **HOLDS to on-chain resolution** instead of dumping into the crash. The −30% trigger is now a window [0.50, entry×0.70], not an open-ended dump.
- **Never sell below entry** — hard floor in `trading.execute_live_sell`. The confirmed stop-loss is the **only** exempt path; every other path holds a losing position to natural on-chain resolution.
- **Winner floor (0.99)** — `execute_live_sell` refuses winner-reason orders below **0.99** (the position holds instead), the winners-only sweep uses max(smart, race)=0.99, and the self-tuner's `resolved_exit_threshold` bounds are pinned to (0.99, 0.99). One flat floor across every lane (user 2026-06-21 v4, "sell at 0.99 as well" — back to the 0.99 floor, fast-lane 0.98 downgrade removed).
- **Expiry** never force-closes a market that is still `acceptingOrders` — it confirms via a live lookup and uses `gameStartTime` (Gamma `endDate` is frequently set *before* kickoff for sports). A genuinely-resolved loser is written off locally ~8 h after expiry, no order.
- No EOD flatten, no blanket stop-loss, no loss-sweep. The universal sweep realizes **winners only** and uses `max(smart, race)` resolved-exit thresholds (0.99, v4) — it can never fire earlier than the race exit.
- **Daily drawdown halt: disabled** (`POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0`). The per-trade confirmed SL is the risk control.

**Excluded markets** (`models.py:is_excluded_market`, across every lane). **WEATHER-ONLY (2026-07-06): with `weather_only = true` this whole section is moot at entry selection — the lane keeps ONLY weather markets and bypasses the ban list entirely (weather is itself on it).** **v4 (user 2026-06-21): `unban_all_markets = true` BYPASSES this entire list at entry selection** — every category is allowed and governed instead by the **data-driven category auto-disable** (`categories.py`): after ≥ `race_category_min_samples` (100) realized trades in a category, it is dropped from entry selection if its ROI < `race_category_disable_roi` (−5%). `_run_race_tick` computes the disabled set from the realized ledger each tick (fail-open) and `_build_eligible_candidates` filters on it. Forward-looking — a fresh bot disables nothing; `other` is never auto-disabled. Per-trade risk is bounded by the $5 fixed stake. The ban list below is what applies when `unban_all_markets = false`:
- **All crypto** — bitcoin/btc/ethereum/solana/dogecoin/xrp/cardano/litecoin/"crypto" + Up/Down binaries
- **All stock market / equities — BANNED OUTRIGHT (re-banned 2026-06-12)**: the one-day in-session experiment is over — indices & ETFs, big-cap tickers & companies (word-bounded `_STOCK_MARKET_RE` incl. ABNB/Airbnb, UBER, COIN, PLTR, HOOD), equity terms, `closes above/below $X`, the generic `(TICKER) … $` rule, weekly ranges, and touch markets are all excluded, always
- **Tweet-count markets** — banned outright (2026-06-12): "Will Elon Musk post 240-259 tweets…" — week-long counts with no convergence signal (`"tweet"` + `-tweets`/`of-tweets` slugs)
- **Macro / central-bank interest-rate markets** — banned outright (2026-06-16): Fed/FOMC, ECB, BoE/BoJ, Bank of Brazil Selic, "rate cut/hike/decision", "interest rate(s)", "(raise|cut|hold|lower|hike) rates", "basis points" (`_MACRO_RATE_RE`, word-bounded). They resolve weeks-to-months out — never inside the ≤4h window; one slipped in via live-position sync ("Fed rate cut by September 2026") and was sold manually.
- **"What will be said" markets — banned outright (2026-06-18):** bets on whether a commentator/announcer/person will SAY or MENTION a word or phrase ('Will the announcers say "Golden Boot" during Canada vs Qatar?'). Pure linguistic coin-flips, no convergence edge (user: "never bet about what something will say"). `_SPEECH_MARKET_RE` matches word-bounded `say/says/said/saying/mention(s/ed)/utter(s/ed)` so "essay"/"naysayer" can't collide.
- **YouTube view/subscriber-count + entertainment ("Divertissement")** — banned outright (2026-06-14): `youtube`/`mrbeast` + word-bounded `\bviews\b`, plus awards/box-office/charts/streaming/social-metrics (academy award, best picture, grammy/emmy/golden globe, palme d'or, box office, rotten tomatoes, billboard, spotify, netflix, tiktok, movie, film, album, subscribers, followers, streams, celebrity). No convergence edge; name-collision-safe terms only
- **League of Ireland soccer banned (2026-06-12)** — every Premier Division (Ireland) market carries the `irl1-` slug prefix; the whole championship is excluded (live O/U positions slid mid-game with no league marker in the question)
- **Esports — BANNED OUTRIGHT (2026-06-19, user "remove completely esports bets from bot 1 2 3 — no counter strike no league of legends LoL etc")**: every title — League of Legends/LoL, Counter-Strike, CS2, Valorant, Dota, Mobile Legends, Rainbow Six, Rocket League, Overwatch, generic BO1/BO3/BO5 — is excluded regardless of live status or ask (`is_esports_text` drives the ban). The prior LoL-only-while-live carve-out is gone.
- temperature/weather (°C + °F), exact-score, **ALL O/U goal-total lines (0.5/1.5/2.5/3.5/4.5/5.5/6.5/7.5)** — 4.5 banned 2026-06-14 after a loss audit showed O/U 4.5 Unders were 80% of all losses (3 worst trades ever) — Asian-handicap "Spread:"/"Game Handicap:", draw markets, halftime leading/score
- `btc_edge` lane and `noise_fallback` are **disabled**.

## Multi-bot layout

Three independent live bots, each with its own wallet, `.env`, and ledger.

- **Profiles:** `grinder.toml` (bot 1), `grinder_b.toml` (bots 2 & 3) — all grinder, keep strategy keys in sync. Live data (`paper_state.json`, journals, `starting_cash.txt`) is **gitignored = per-machine**; only code + profiles are shared.
- **Launchers:** `run_live_70.sh` (bot 1), `run_live_b.sh` (bots 2 & 3, grinder), `run_live_win.sh` (Windows). Branches: `main` + `kzer_windows`.
- **Per-machine baseline:** `data/starting_cash.txt` (gitignored) sets each bot's report baseline independently of the shared profile. Written by `fresh_start.py`. Both `live_analyst._starting_cash` and `notifications._total_pnl_vs_start` prefer it.

## Launch

```bash
bash scripts/run_live_b.sh        # bots 2 & 3 (or run_live_70.sh for bot 1)
```

Boots the live grinder (`--profile grinder_b`, 10 s tick) + a dry paper twin + the read-only `live_analyst` sidecar + the **daily self-learning sidecar** (`daily_self_improve.sh`, see Safety). Live position sync is on (`POLYMARKET_SYNC_LIVE_POSITIONS=1`). `Ctrl+C` cleans up the process group.

> **Do not use `run_all.sh` for live** — it resets the ledger on startup and runs a retired dry race.

## Reporting — `scripts/live_analyst.py`

The **only** Telegram message the live stack sends — deterministic, no AI. A French "RAPPORT LIVE" that fires **on startup**, then every `LIVE_ANALYST_CYCLE_SECONDS`, plus a daily 10:00 US/Eastern. Shows:
- **Capital:** equity + **P&L since start = equity − baseline** ("depuis le début")
- **Total trades + win rate** (count + %, V/D)
- **v4 performance** (≥10 closed trades): `PERFORMANCE v4 :` ROI / Sharpe / profit factor / max drawdown; a **p/q/edge** line for **all-time and today** (`🎯 p=avg entry price · q=win rate · edge(q−p) in pts` — the bot is +EV only when q>p, user 2026-06-22); a **best/worst category** line (`🥇 Meilleure catégorie … 🥶 Pire …` ranked by realized $ P&L, ROI + trade count alongside, user 2026-06-23 — since 2026-07-10 "weather" is a first-class category, so under the weather-only lane this line reads `🥇 Meilleure catégorie : weather +$X (ROI%, N)`, user "put best category as weather and show how much we did"); plus `🏷️ Catégories à risque :` (worst per-category ROIs, ⛔ on auto-disabled ones) — the data-driven governance at a glance (`_v4_performance_lines` / `_pq_line`)
- **Open positions + trades-of-the-day — capped to top movers** (user 2026-06-22, "too many positions, I want something clear as a summary", "top 5 winning and top 5 where we lost"): each of the two lists shows only the **top `LIVE_REPORT_TOP_N` winners + N worst losers** (default 5, `_winners_losers`), with the rest folded into a `… +X autres` line. The summary header above each list (counts, totals, latent P&L) still covers everything; `LIVE_REPORT_TOP_N=0` → summary only. Shown open positions keep their estimated end time (`⏳ Fin prévue : HH:MM ET (dans XhYY)`); the per-position event link was dropped to keep entries to two lines.
- **Equity read is timeout-resilient** (user 2026-06-22): `_fetch_live_equity` retries the `/positions` API 3× and, on persistent failure, returns `None` so the report falls back to the bot-synced local ledger — never cash-only (which previously tripped a stale `assumed_live_balance_usd` floor into a fabricated "$60 / -$100" capital). The fixed-equity floor was removed from the report path.
- **Redemption watchdog** — resolved positions with real value still awaiting payout (`redeemable: true`, value ≥ $1) are listed as `💰 GAINS RÉSOLUS EN ATTENTE DE PAIEMENT`; Polymarket auto-redeems, so this section is empty in normal operation — anything persisting two reports means claim manually on polymarket.com/portfolio

No per-trade lists, no `💓 Bilan` heartbeat, no BUY/SELL alerts (all `TELEGRAM_ALERT_*=0` in the launchers). The all-time figure is equity-vs-baseline (not realized-from-entry), so a re-based account never shows phantom losses.

## Reset workflow — `scripts/fresh_start.py`

Run on a bot's own machine, **bot stopped**: backs up + wipes closed-trade history (journal + realized cache), writes a flat `paper_state.json` (open trades re-import on next start via live sync — **open trades are kept**), stamps `data/live_tracking_start`, and sets the per-machine baseline (`data/starting_cash.txt`). `--equity X` forces the baseline; otherwise it's computed from live cash + open positions.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`; `--yes` exists only for the launcher scripts / automation.
- No LLM call in the scanning or trade-selection path — the scanner stays deterministic Python over Polymarket APIs.
- No random/unfiltered live trades. `noise_fallback` is disabled.
- Preserve `data/paper_state.json`, `data/trade_journal.jsonl`, `data/realized_trade_cache.jsonl`, `data/starting_cash.txt` unless the user explicitly asks for a reset (use `fresh_start.py`).
- **Offline self-tuner (bounded, opt-in):** `scripts/auto_improve.py` + `.github/workflows/auto-improve.yml` use the Claude Code CLI to open PRs that tune **EXIT/SIZING knobs only** in the live profile, auto-merging once CI is green. The **entry/bet-selection filters are FROZEN** (`_audit_frozen` aborts if the price band, spread, hours, day-change, momentum, or liquidity/volume move) and a stop-loss can never be *introduced* by it. It runs **OFFLINE, never in the live trade loop** — the one sanctioned LLM exception. See `docs/AUTONOMY.md`.
- **Daily self-learning sidecar (`scripts/daily_self_improve.sh`, 2026-06-17):** the launchers spawn this as a process-group sidecar. Once per day after `DAILY_SELF_IMPROVE_HOUR` (23:00 local) it writes a deterministic **end-of-day analysis** (`auto_improve.py --analyze-only`: all-time/today/7-day P&L, win/loss asymmetry, per-category, worst trades) and then runs the fenced self-tuner above. It is **fully wrapped (`set +e` + try/catch) so it can NEVER crash or stall the live loop**, runs at most once/day (`data/.last_self_improve`), and **always restores the git branch** (the running bot keeps its loaded code; a tuned config only applies on the next manual restart). Toggle `DAILY_SELF_IMPROVE=0`. The tuner edits only `grinder.toml` (bot 1); mirror to `grinder_b.toml` by hand if a change should reach bots 2 & 3.
- The bot does not have the capability to write or push its own source code.

## Project map

- `polymarket_bot/race_strategies.py` — grinder engine: `select_grinder`, `_build_eligible_candidates`, `_execute_race_exits` (resolved-exit, confirmed SL, expiry/open-market check, winners-only sweep), `_lookup_open_market`.
- `polymarket_bot/trading.py` — authenticated BUY/SELL execution, stake computation, and the **never-sell-below-entry floor** (exempts `race_stop_loss_confirmed`).
- `polymarket_bot/models.py` — `is_excluded_market` (the ban list) + shared dataclasses/parsers.
- `polymarket_bot/categories.py` — v4 category classifier (+ `weather` as a first-class category since 2026-07-10, checked first, so the weather-only lane reports under its own bucket in the Telegram 🥇 line) + per-category ROI stats + data-driven `disabled_categories` auto-disable (the governance under `unban_all_markets`). While `weather_only` is on, the auto-disable can never drop `weather` (starvation guard, `_build_eligible_candidates`).
- `polymarket_bot/forecast.py` — v4 forecasting model (`predicted_probability` via empirical calibration, `edge`, `quality_score`) + dashboard analytics (`sharpe_ratio`, `profit_factor`, `max_drawdown`, `promotion_status`). The EV/quality gates (`race_min_edge`, `race_min_quality_score`) are OPT-IN (default 0/off).
- `polymarket_bot/config.py` — every `Settings` field and its env-var name.
- `polymarket_bot/main.py` — CLI commands and the strategy loop dispatch; tick orchestration; journal writer.
- `polymarket_bot/portfolio.py` — local ledger (cash, open positions, exits).
- `polymarket_bot/gamma.py` — Gamma market scan + reverse-lookup by clob_token_ids.
- `scripts/run_live_70.sh` / `run_live_b.sh` / `run_live_win.sh` — live launchers (bot 1 / bots 2 & 3 grinder / Windows). Do NOT reset the ledger.
- `scripts/live_analyst.py` — the Telegram RAPPORT LIVE (read-only sidecar).
- `scripts/fresh_start.py` — per-machine reset (keeps open trades).
- `configs/profiles/grinder.toml`, `grinder_b.toml` — grinder live profiles (bot 1 / bots 2 & 3).
- `docs/PROFILES.md` — exhaustive TOML key reference. `docs/STRATEGIES.md` — buy lanes + exit conditions. `docs/AUTONOMY.md` — offline self-tuner design.

## Development workflow

```bash
uv run python -B -m unittest discover -s tests   # full test suite
uv run pmbot status                              # mode, equity, open positions
uv run pmbot positions                           # open positions table
uv run pmbot journal-stats                       # per-bucket P&L / win rate
```

`status`/`positions` read the dry-run ledger with `--dry-run`. `NO_COLOR=1` disables ANSI; `POLYMARKET_FORCE_COLOR=1` forces it. Local dashboard: `uv run pmbot dashboard` (`http://127.0.0.1:8765`).

When changing strategy/filters/sizing/exits: edit **both** `grinder.toml` and `grinder_b.toml`, update tests if behavior changes, cherry-pick to `kzer_windows` if it should reach bot 3, and update `CHANGELOG.md`, `README.md`, this file, and the skill.

## Tick sequence (race/grinder)

1. Load short-expiry Gamma markets (out to the widest ladder rung).
2. Build eligible candidates (entry filters + exclusions); log a wide forward-observation net. Entries use the 4 h window (ladder disabled); same-game picks collapse to one (best bid wins).
3. Sync live Polymarket positions into the ledger; refresh live USDC cash. **Resolution reconcile (v4, `_sync_live_positions`):** a chain-`redeemable` holding is booked a resolved WIN at full value; one whose value collapsed below the dust floor AND whose endDate has passed is booked a resolved LOSS at ~$0 (not its stale mid-price). The past-endDate guard prevents mis-booking a mid-game gap; pending-oracle 15-min crypto (redeemable=false, curPrice≈0.50) stays open until the chain settles it.
4. Run exits: live-book bid probe + resolved-exit (≥0.99, v4), confirmed −30% SL, expiry/open-market handling, winners-only sweep.
5. (Daily drawdown halt — disabled.)
6. Place new grinder picks with percentage sizing toward the cash floor.
7. Persist portfolio + write journal entries for any closed positions.
8. Print JSON result, sleep `auto_interval_seconds`.
