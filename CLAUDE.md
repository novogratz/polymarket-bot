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

## Strategy — `grinder` (race mode)

Buy a heavily-favored binary outcome near its resolution and **ride it to resolution**. The edge is the implied-probability gap between the entry price and a near-certain outcome settling at 1.0. Source of truth: `configs/profiles/grinder.toml` (bot 1) and `configs/profiles/grinder_b.toml` (bots 2 & 3) — keep their strategy keys in sync. Selector `select_grinder` in `polymarket_bot/race_strategies.py`.

**Entry** (`_build_eligible_candidates`):
- price (ask) ∈ **[0.85, 0.97]**, ≤ **6 h** to close
- spread ≤ 4¢, liquidity ≥ $500, 24 h volume ≥ $300
- `max_day_change_pct = 0.10` — skip markets that moved >10% today (live-game gap risk)
- `min_outcome_momentum = -0.05` — skip outcomes that fell >5% today (trending away from resolution)

**Sizing:** percentage of equity (`position_pct`), capped by `max_position_ceiling_pct` (no fixed USD cap), `max_orders_per_tick`. Scales automatically with the bankroll.

**Exits** (`_execute_race_exits`):
- **Resolved-exit** — sell at bid ≥ `resolved_exit_threshold` (0.99; raised from 0.97 on 2026-06-10 — drop to 0.98 if 0.99 rarely fills before resolution).
- **Controlled stop-loss — −25%, confirmed over 3 consecutive ticks** (`sl_pct=0.25`, `sl_confirm_ticks=3`, min age 5 min). A one-tick thin-book phantom bid can never trigger it; the loss must persist. Tagged `race_stop_loss_confirmed`.
- **Never sell below entry** — hard floor in `trading.execute_live_sell`. The confirmed stop-loss is the **only** exempt path; every other path holds a losing position to natural on-chain resolution.
- **Expiry** never force-closes a market that is still `acceptingOrders` — it confirms via a live lookup and uses `gameStartTime` (Gamma `endDate` is frequently set *before* kickoff for sports). A genuinely-resolved loser is written off locally ~8 h after expiry, no order.
- No EOD flatten, no blanket stop-loss, no loss-sweep (the universal sweep realizes **winners** ≥ `resolved_exit_threshold` (0.99) only).
- **Daily drawdown halt: disabled** (`POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0`). The per-trade confirmed SL is the risk control.

**Excluded markets** (`models.py:is_excluded_market`, blanket across every lane):
- **All crypto** — bitcoin/btc/ethereum/solana/dogecoin/xrp/cardano/litecoin/"crypto" + Up/Down binaries
- **Esports** — counter-strike/valorant/league of legends/dota/cs2/csgo/rainbow six/rocket league/overwatch + BO1/BO3/BO5
- temperature/weather (°C + °F), exact-score, O/U low-line (0.5/1.5/2.5/3.5) + high-line (5.5/6.5/7.5), Asian-handicap "Spread:", draw markets, halftime leading/score
- `btc_edge` lane and `noise_fallback` are **disabled**.

## Multi-bot layout

Three independent live bots, each with its own wallet, `.env`, and ledger.

- **Profiles:** `grinder.toml` (bot 1), `grinder_b.toml` (bots 2 & 3). Live data (`paper_state.json`, journals, `starting_cash.txt`) is **gitignored = per-machine**; only code + profiles are shared.
- **Launchers:** `run_live_70.sh` (bot 1), `run_live_b.sh` (bots 2 & 3), `run_live_win.sh` (Windows). Branches: `main` + `kzer_windows`.
- **Per-machine baseline:** `data/starting_cash.txt` (gitignored) sets each bot's report baseline independently of the shared profile. Written by `fresh_start.py`. Both `live_analyst._starting_cash` and `notifications._total_pnl_vs_start` prefer it.

## Launch

```bash
bash scripts/run_live_b.sh        # bots 2 & 3 (or run_live_70.sh for bot 1)
```

Boots the live grinder (`--profile grinder_b`, 10 s tick) + a dry paper twin + the read-only `live_analyst` sidecar. Live position sync is on (`POLYMARKET_SYNC_LIVE_POSITIONS=1`). `Ctrl+C` cleans up the process group.

> **Do not use `run_all.sh` for live** — it resets the ledger on startup and runs a retired dry race.

## Reporting — `scripts/live_analyst.py`

The **only** Telegram message the live stack sends — deterministic, no AI. A French "RAPPORT LIVE" that fires **on startup**, then every `LIVE_ANALYST_CYCLE_SECONDS`, plus a daily 10:00 US/Eastern. Shows:
- **Capital:** equity + **P&L since start = equity − baseline** ("depuis le début")
- **Total trades + win rate** (count + %, V/D)
- **Open positions**

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
- The bot does not have the capability to write or push its own source code.

## Project map

- `polymarket_bot/race_strategies.py` — grinder engine: `select_grinder`, `_build_eligible_candidates`, `_execute_race_exits` (resolved-exit, confirmed SL, expiry/open-market check, winners-only sweep), `_lookup_open_market`.
- `polymarket_bot/trading.py` — authenticated BUY/SELL execution, stake computation, and the **never-sell-below-entry floor** (exempts `race_stop_loss_confirmed`).
- `polymarket_bot/models.py` — `is_excluded_market` (the ban list) + shared dataclasses/parsers.
- `polymarket_bot/config.py` — every `Settings` field and its env-var name.
- `polymarket_bot/main.py` — CLI commands and the strategy loop dispatch; tick orchestration; journal writer.
- `polymarket_bot/portfolio.py` — local ledger (cash, open positions, exits).
- `polymarket_bot/gamma.py` — Gamma market scan + reverse-lookup by clob_token_ids.
- `scripts/run_live_70.sh` / `run_live_b.sh` / `run_live_win.sh` — live launchers (bot 1 / bots 2-3 / Windows). Do NOT reset the ledger.
- `scripts/live_analyst.py` — the Telegram RAPPORT LIVE (read-only sidecar).
- `scripts/fresh_start.py` — per-machine reset (keeps open trades).
- `configs/profiles/grinder.toml`, `grinder_b.toml` — live profiles.
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

1. Load short-expiry Gamma markets (within `max_hours`).
2. Build eligible candidates (entry filters + exclusions); log a wide forward-observation net.
3. Sync live Polymarket positions into the ledger; refresh live USDC cash.
4. Run exits: resolved-exit (≥0.99), confirmed −25% SL, expiry/open-market handling, winners-only sweep.
5. (Daily drawdown halt — disabled.)
6. Place new grinder picks with percentage sizing toward the cash floor.
7. Persist portfolio + write journal entries for any closed positions.
8. Print JSON result, sleep `auto_interval_seconds`.
