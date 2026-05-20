#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil auto_fresh_qe_persist_stack.
# Variante analyst-spawned de claude_baseline_tight : MONTH top 30,
# min_consensus=3, min_copied_usdc=\$150, microladder TP qui lock les
# gains tôt. Dry race: +64% ROI sur 8 closed (50% wr) — meilleur
# expected value du board (sample raisonnable + ratio PnL/trade élevé).
# Sizing 10%/trade, cap 30% equity. assumed_live_balance_usd=\$29.90.
# Toute la config vit dans configs/profiles/auto_fresh_qe_persist_stack.toml.
#
# Ce script passe --yes : la confirmation interactive est skipée, donc aucun
# besoin de TTY. Pour une exécution sans --yes (auto-loop --live tout court),
# l'opérateur DOIT être attaché à un TTY ; sans cela, prompt_live_confirmation
# refuse et abort proprement (cf. live_confirm.py:48).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Live tick interval — fast (10s) even though kzerlepgm_baseline TOML
# now uses 60s for the dry-race rate-limit fix. Env var override wins.
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-10}

# Telegram: tout pousser en live (override .env qui a TELEGRAM_ALERT_TRADES=0
# pour rester silencieux en dry-run).
export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

# Profile label exported BEFORE the live_analyst spawns, so the
# sidecar inherits it (else it logs "(unknown)" in reports).
export POLYMARKET_PROFILE_LABEL=auto_fresh_qe_persist_stack

# ─── Live analyst sidecar (read-only, posts to TELEGRAM_CHAT_ID_LIVE) ──
# Every 30 min: reads paper_state + trade_journal, compares vs dry race
# leaders, calls `claude` CLI for insights. NEVER touches the live bot;
# pure observability. Kill via Ctrl+C (same process group).
cleanup() {
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT
python3 scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' &

uv run pmbot auto-loop --live --profile auto_fresh_qe_persist_stack --yes
