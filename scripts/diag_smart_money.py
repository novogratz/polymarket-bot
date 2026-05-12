"""Diagnostic one-shot du pipeline smart-money.

Usage: uv run python scripts/diag_smart_money.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Force la conf "aggressive" (intersect+persistence ON), large fetch
os.environ.setdefault("POLYMARKET_SMART_TIME_PERIODS", "WEEK,MONTH,ALL")
os.environ.setdefault("POLYMARKET_SMART_LEADERBOARD_LIMIT", "100")
os.environ.setdefault("POLYMARKET_SMART_MIN_TRADER_PNL", "500")
os.environ.setdefault("POLYMARKET_SMART_MIN_TRADER_VOLUME", "1000")
os.environ.setdefault("POLYMARKET_SMART_MIN_TRADER_ROI", "0.02")
os.environ.setdefault("POLYMARKET_SMART_LOOKBACK_MINUTES", "240")
os.environ.setdefault("POLYMARKET_PERSISTENCE_CACHE_PATH", "data/diag_wallet_history.json")
os.environ.setdefault("POLYMARKET_QUIET", "0")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_bot.config import Settings  # noqa: E402
from polymarket_bot.gamma import GammaClient  # noqa: E402
from polymarket_bot.smart_money import (  # noqa: E402
    DataApiClient,
    _top_traders,
    fetch_smart_money_data,
    smart_money_signals,
)
from polymarket_bot.main import load_smart_candidates  # noqa: E402


def _qualifies(t, s):
    if t.pnl < s.smart_min_trader_pnl:
        return False
    if s.smart_min_trader_volume > 0 and t.volume < s.smart_min_trader_volume:
        return False
    if s.smart_min_trader_roi > 0:
        roi = t.pnl / t.volume if t.volume > 0 else 0.0
        if roi < s.smart_min_trader_roi:
            return False
    return True


def run(persistence_enabled: bool) -> None:
    os.environ["POLYMARKET_PERSISTENCE_ENABLED"] = "1" if persistence_enabled else "0"
    settings = Settings()
    print(f"\n=========== persistence_enabled={persistence_enabled} ===========")
    print(f"periods = {settings.smart_time_periods}  top_n={settings.smart_leaderboard_limit}")
    print(f"PnL>={settings.smart_min_trader_pnl} Vol>={settings.smart_min_trader_volume} "
          f"ROI>={settings.smart_min_trader_roi}")
    print(f"min_consensus={settings.smart_min_consensus} min_copied=${settings.smart_min_copied_usdc} "
          f"lookback={settings.smart_trade_lookback_minutes}m")

    client = DataApiClient(settings.data_api_base_url)
    by_period = _top_traders(client, settings)
    sets = {p: {t.wallet.lower() for t in lst} for p, lst in by_period.items()}
    print(f"\nLeaderboards bruts :")
    for p, s in sets.items():
        print(f"  {p}: {len(s)} wallets")

    # Intersections
    week, month, all_ = sets.get("WEEK", set()), sets.get("MONTH", set()), sets.get("ALL", set())
    inter_wm = week & month
    inter_wa = week & all_
    inter_ma = month & all_
    inter_wma = week & month & all_
    inter_2plus = (inter_wm | inter_wa | inter_ma)
    print(f"\nIntersections :")
    print(f"  WEEK∩MONTH      = {len(inter_wm)}")
    print(f"  WEEK∩ALL        = {len(inter_wa)}")
    print(f"  MONTH∩ALL       = {len(inter_ma)}")
    print(f"  WEEK∩MONTH∩ALL  = {len(inter_wma)}")
    print(f"  ≥2 listes (union des paires) = {len(inter_2plus)}")

    # PnL / Vol / ROI pré-filtre par période
    print(f"\nPré-filtre PnL/Vol/ROI par période :")
    for p, lst in by_period.items():
        q = [t for t in lst if _qualifies(t, settings)]
        print(f"  {p}: {len(lst)} → {len(q)} après PnL/Vol/ROI")

    # Pipeline complet
    data = fetch_smart_money_data(settings, client=client)
    print(f"\nfetch_smart_money_data : traders_used={data.traders_used} trades={len(data.trades)} "
          f"cohort_before={data.cohort_before_persistence} after={data.cohort_after_persistence}")

    # Décomposition des BUY récents par wallet de la cohorte finale
    buys = [t for t in data.trades if t.side.upper() == "BUY"]
    print(f"  BUYs récents collectés : {len(buys)} ({len({t.wallet for t in buys})} wallets distincts)")

    # Vérifie combien de BUYs passent le filtre min_trade_usd
    eligible = [t for t in buys if t.usdc_size >= settings.smart_min_trade_usd]
    print(f"  BUYs ≥ ${settings.smart_min_trade_usd} (min_trade_usd) : {len(eligible)}")

    # Pipeline réel : scan smart-money standard (3 passes Gamma)
    ranked = load_smart_candidates(settings)
    print(f"  candidates Gamma (smart scan) : {len(ranked)}")
    tokens_in_candidates = {c.token_id for c in ranked if c.token_id}
    print(f"  tokens distincts dans candidates : {len(tokens_in_candidates)}")

    # Combien de tokens de BUYs apparaissent dans candidates ?
    buy_tokens = {t.asset for t in data.trades if t.side.upper() == "BUY"}
    inter = buy_tokens & tokens_in_candidates
    print(f"  tokens distincts dans BUYs : {len(buy_tokens)}")
    print(f"  intersection (BUYs ∩ candidates) : {len(inter)}  ← signaux possibles avant filtres")

    signals, details = smart_money_signals(
        ranked, data.trades, settings, pnl_by_wallet=data.pnl_by_wallet, include_details=True
    )
    print(f"\nsmart_money_signals → {len(signals)} signaux")
    print(f"  eligible_trade_count : {details.get('eligible_trade_count')}")
    print(f"  matched_tokens (BUY token in candidate set) : {details.get('matched_tokens')}")
    print(f"  rejected breakdown :")
    for k, v in sorted(details.get("rejected", {}).items(), key=lambda x: -x[1]):
        print(f"    {k:35s} {v}")


if __name__ == "__main__":
    run(persistence_enabled=True)
    run(persistence_enabled=False)
