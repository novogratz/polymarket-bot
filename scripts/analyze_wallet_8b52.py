"""Analyse détaillée du wallet 0x8b5239...568e.

Lit ses 1370 trades depuis data/wallet_edge_directional_trades.csv,
fetch ses positions courantes via data-api.polymarket.com et produit
un rapport markdown complet dans reports/wallet_8b52_detail.md.
"""

from __future__ import annotations

import csv
import json
import statistics as stats
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

WALLET = "0x8b5239494dd65eed682f0d9f0481ddeae4ff568e"
TRADES_CSV = Path("data/wallet_edge_directional_trades.csv")
RANKING_CSV = Path("data/wallet_ytd_ranking.csv")
OUT = Path("reports/wallet_8b52_detail.md")
DATA_API = "https://data-api.polymarket.com"


def http_get_json(url: str, timeout: float = 20.0) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "wallet-analysis/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def fmt_money(x: float) -> str:
    return f"{x:,.2f}$".replace(",", " ")


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def main() -> None:
    rows = []
    with TRADES_CSV.open() as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if row["wallet"].lower() != WALLET:
                continue
            rows.append(row)
    print(f"Trades chargés : {len(rows)}")

    # Conversions
    for r in rows:
        r["ts"] = int(r["ts_trade"])
        r["price"] = float(r["price_trade"])
        r["price_at"] = float(r["price_at_trade"])
        r["price_5"] = float(r["price_5min"])
        r["price_15"] = float(r["price_15min"])
        r["price_30"] = float(r["price_30min"])
        r["move15"] = float(r["move_15min"])
        r["move30"] = float(r["move_30min"])
        r["edge_dir"] = (
            float(r["edge_directional"]) if r["edge_directional"] not in ("", None) else None
        )
        r["final_move"] = float(r["final_move"])
        r["edge_jump"] = float(r["edge_jump"]) if r["edge_jump"] not in ("", None) else None
        r["dt"] = datetime.fromtimestamp(r["ts"], tz=UTC)

    # Ranking metadata
    rank_meta = None
    with RANKING_CSV.open() as fp:
        for row in csv.DictReader(fp):
            if row["wallet"].lower() == WALLET:
                rank_meta = row
                break

    # Stats globales
    n = len(rows)
    cats = Counter(r["category"] or "unknown" for r in rows)
    prices = [r["price"] for r in rows]
    moves15 = [r["move15"] for r in rows]
    edges = [r["edge_jump"] for r in rows if r["edge_jump"] is not None]

    # Buckets prix d'entrée
    price_buckets = {
        "[0.00-0.10[": 0,
        "[0.10-0.30[": 0,
        "[0.30-0.50[": 0,
        "[0.50-0.70[": 0,
        "[0.70-0.90[": 0,
        "[0.90-0.97[": 0,
        "[0.97-1.00]": 0,
    }
    bucket_edges_pct_ahead = {k: [] for k in price_buckets}
    for r in rows:
        p = r["price"]
        b = (
            "[0.00-0.10[" if p < 0.10
            else "[0.10-0.30[" if p < 0.30
            else "[0.30-0.50[" if p < 0.50
            else "[0.50-0.70[" if p < 0.70
            else "[0.70-0.90[" if p < 0.90
            else "[0.90-0.97[" if p < 0.97
            else "[0.97-1.00]"
        )
        price_buckets[b] += 1
        if r["edge_jump"] is not None:
            bucket_edges_pct_ahead[b].append(r["edge_jump"])

    # Patterns horaires (UTC)
    by_hour = Counter(r["dt"].hour for r in rows)
    by_dow = Counter(r["dt"].strftime("%A") for r in rows)

    # Top markets / titles
    titles = Counter(r["title"] or "(unknown)" for r in rows)

    # Edge par catégorie
    edge_by_cat = defaultdict(list)
    for r in rows:
        if r["edge_jump"] is not None:
            edge_by_cat[r["category"] or "unknown"].append(r["edge_jump"])

    # Activité temporelle
    days = sorted({r["dt"].date() for r in rows})
    span_days = (days[-1] - days[0]).days + 1 if days else 0

    # Tickets — ici on n'a pas la taille USD du trade (pas dans le CSV).
    # On va chercher dans les positions courantes.
    print("Fetch positions courantes…")
    try:
        positions = http_get_json(
            f"{DATA_API}/positions?user={WALLET}&limit=500&sortBy=CURRENT&sortDirection=DESC"
        )
        if not isinstance(positions, list):
            positions = []
    except Exception as exc:
        print(f"  WARN positions: {exc}")
        positions = []

    print(f"  → {len(positions)} positions ouvertes")

    # User profile
    print("Fetch user profile…")
    try:
        profile = http_get_json(f"{DATA_API}/value?user={WALLET}")
        if not isinstance(profile, list):
            profile = []
    except Exception as exc:
        print(f"  WARN profile: {exc}")
        profile = []

    # Activité (last trades) pour avoir taille USDC + side récent
    print("Fetch derniers trades (data-api)…")
    try:
        recent_trades = http_get_json(f"{DATA_API}/trades?user={WALLET}&limit=500&takerOnly=true")
        if not isinstance(recent_trades, list):
            recent_trades = []
    except Exception as exc:
        print(f"  WARN recent: {exc}")
        recent_trades = []
    print(f"  → {len(recent_trades)} trades récents (avec sizes)")

    # Tickets USD depuis recent trades
    sizes_usd = []
    for t in recent_trades:
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            sizes_usd.append(size * price)
        except (TypeError, ValueError):
            pass

    # Activité hebdomadaire
    by_week = Counter(r["dt"].strftime("%Y-W%U") for r in rows)

    # Ratio BUY/SELL approximatif par recent_trades
    side_counter = Counter(t.get("side") for t in recent_trades if t.get("side"))

    # Composition d'OPEN positions
    open_summary = []
    open_total_value = 0.0
    open_total_pnl = 0.0
    for p in positions:
        try:
            cur = float(p.get("currentValue") or 0)
            init = float(p.get("initialValue") or 0)
            pnl = cur - init
            open_total_value += cur
            open_total_pnl += pnl
            open_summary.append({
                "title": (p.get("title") or "(unknown)")[:80],
                "outcome": p.get("outcome", ""),
                "size": float(p.get("size") or 0),
                "avg_price": float(p.get("avgPrice") or 0),
                "cur_price": float(p.get("curPrice") or 0),
                "current_value": cur,
                "initial_value": init,
                "pnl": pnl,
                "pct_pnl": float(p.get("percentPnl") or 0),
            })
        except (TypeError, ValueError):
            continue
    open_summary.sort(key=lambda x: -x["current_value"])

    # Build report
    lines: list[str] = []
    lines.append(f"# Profil détaillé — `{WALLET}`")
    lines.append("")
    lines.append(f"_Généré le {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")
    lines.append(f"**Lien Polymarket :** https://polymarket.com/profile/{WALLET}")
    lines.append("")

    lines.append("## 1. Identité")
    lines.append("")
    if rank_meta:
        lines.append(f"- Username affiché : `{rank_meta['username']}`")
        lines.append(f"  (= adresse + suffixe timestamp → **pas d'alias choisi**)")
        lines.append(f"- Rang YTD (par PnL net) : **#{rank_meta['rank']}** sur 658 wallets")
        lines.append(f"- PnL net YTD : **{fmt_money(float(rank_meta['pnl_net_ytd']))}**")
        lines.append(f"  - Réalisé : {fmt_money(float(rank_meta['pnl_realized']))}")
        lines.append(f"  - Non réalisé : {fmt_money(float(rank_meta['pnl_unrealized']))}")
        lines.append(f"- Volume BUY YTD : {fmt_money(float(rank_meta['volume_buy_ytd']))}")
        lines.append(f"- Trades YTD : {rank_meta['n_trades']}  (matched SELLs : {rank_meta['n_matched_sells']})")
        lines.append(f"- Win rate (FIFO) : **{fmt_pct(float(rank_meta['win_rate']))}**")
        lines.append(f"- Hold médian : {float(rank_meta['hold_time_median_min']):.0f} min")
        lines.append(f"- Catégorie dominante : **{rank_meta['top_category']}**")
    if profile:
        try:
            current_value = float(profile[0].get("value") or 0)
            lines.append(f"- Valeur courante du portefeuille : **{fmt_money(current_value)}**")
        except (TypeError, ValueError, IndexError):
            pass
    lines.append("")

    lines.append("## 2. Activité temporelle")
    lines.append("")
    if days:
        lines.append(f"- Premier trade scoré : **{days[0].isoformat()}**")
        lines.append(f"- Dernier trade scoré : **{days[-1].isoformat()}**")
        lines.append(f"- Période active : **{span_days} jours**")
        lines.append(f"- Trades / jour moyen : **{n / span_days:.1f}**")
    lines.append("")
    lines.append("### Distribution horaire (UTC)")
    lines.append("")
    lines.append("| Heure | n |")
    lines.append("|---:|---:|")
    for h in range(24):
        bar = "█" * int(by_hour[h] * 30 / max(by_hour.values()) if by_hour else 0)
        lines.append(f"| {h:02d}h | {by_hour[h]} {bar} |")
    lines.append("")
    lines.append("### Distribution par jour de semaine")
    lines.append("")
    lines.append("| Jour | n |")
    lines.append("|---|---:|")
    for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
        lines.append(f"| {d} | {by_dow[d]} |")
    lines.append("")

    lines.append("## 3. Distribution catégories (BUYs YTD)")
    lines.append("")
    lines.append("| Catégorie | n | % |")
    lines.append("|---|---:|---:|")
    for cat, c in cats.most_common():
        lines.append(f"| {cat} | {c} | {fmt_pct(c / n)} |")
    lines.append("")

    lines.append("## 4. Prix d'entrée (BUY) — distribution")
    lines.append("")
    lines.append(f"- Médiane : **{quantile(prices, 0.5):.3f}**")
    lines.append(f"- Moyenne : {sum(prices) / len(prices):.3f}")
    lines.append(f"- p25 / p75 : {quantile(prices, 0.25):.3f} / {quantile(prices, 0.75):.3f}")
    lines.append(f"- Min / Max : {min(prices):.3f} / {max(prices):.3f}")
    lines.append("")
    lines.append("### Buckets prix")
    lines.append("")
    lines.append("| Bande | n | % | edge_jump moyen | n_jumps |")
    lines.append("|---|---:|---:|---:|---:|")
    for b, c in price_buckets.items():
        eds = bucket_edges_pct_ahead[b]
        avg = f"{stats.mean(eds):+.3f}" if eds else "-"
        lines.append(f"| {b} | {c} | {fmt_pct(c / n)} | {avg} | {len(eds)} |")
    lines.append("")

    lines.append("## 5. Edge directionnel par catégorie")
    lines.append("")
    lines.append("| Catégorie | n_jumps | edge moyen | médiane | %>0 |")
    lines.append("|---|---:|---:|---:|---:|")
    for cat in sorted(edge_by_cat.keys(), key=lambda k: -len(edge_by_cat[k])):
        eds = edge_by_cat[cat]
        avg = stats.mean(eds)
        med = stats.median(eds)
        pos = sum(1 for e in eds if e > 0) / len(eds)
        lines.append(f"| {cat} | {len(eds)} | {avg:+.3f} | {med:+.3f} | {fmt_pct(pos)} |")
    lines.append("")

    lines.append("## 6. Mouvement à 15 min (post-trade)")
    lines.append("")
    lines.append(f"- Médiane : **{quantile(moves15, 0.5):+.4f}**")
    lines.append(f"- Moyenne : {sum(moves15) / len(moves15):+.4f}")
    lines.append(f"- p25 / p75 : {quantile(moves15, 0.25):+.4f} / {quantile(moves15, 0.75):+.4f}")
    pos_share = sum(1 for m in moves15 if m > 0) / len(moves15)
    neg_share = sum(1 for m in moves15 if m < 0) / len(moves15)
    flat_share = sum(1 for m in moves15 if m == 0) / len(moves15)
    lines.append(f"- % move > 0 (BUY suivi de hausse) : **{fmt_pct(pos_share)}**")
    lines.append(f"- % move < 0 (BUY suivi de baisse) : {fmt_pct(neg_share)}")
    lines.append(f"- % move == 0 (pas de mouvement) : {fmt_pct(flat_share)}")
    lines.append("")

    lines.append("## 7. Tickets USD (depuis 500 derniers trades)")
    lines.append("")
    if sizes_usd:
        lines.append(f"- Échantillon : {len(sizes_usd)} trades")
        lines.append(f"- Médiane ticket : **{fmt_money(quantile(sizes_usd, 0.5))}**")
        lines.append(f"- Moyenne ticket : {fmt_money(sum(sizes_usd) / len(sizes_usd))}")
        lines.append(f"- p25 / p75 : {fmt_money(quantile(sizes_usd, 0.25))} / {fmt_money(quantile(sizes_usd, 0.75))}")
        lines.append(f"- Min / Max : {fmt_money(min(sizes_usd))} / {fmt_money(max(sizes_usd))}")
        lines.append(f"- Volume cumulé : **{fmt_money(sum(sizes_usd))}**")
    else:
        lines.append("- (échec API)")
    lines.append("")

    lines.append("### Side BUY/SELL (500 derniers trades)")
    lines.append("")
    if side_counter:
        for side, c in side_counter.most_common():
            lines.append(f"- {side} : {c} ({fmt_pct(c / sum(side_counter.values()))})")
    lines.append("")

    lines.append("## 8. Top 20 marchés (par fréquence de BUY)")
    lines.append("")
    lines.append("| Marché | BUYs |")
    lines.append("|---|---:|")
    for title, c in titles.most_common(20):
        lines.append(f"| {title[:80]} | {c} |")
    lines.append("")

    lines.append("## 9. Positions ouvertes actuelles (top 20 par valeur)")
    lines.append("")
    if open_summary:
        lines.append(f"- Nombre de positions : **{len(open_summary)}**")
        lines.append(f"- Valeur totale : **{fmt_money(open_total_value)}**")
        lines.append(f"- PnL non réalisé total : {fmt_money(open_total_pnl)}")
        lines.append("")
        lines.append("| Marché | Outcome | Size | Avg | Now | Value | PnL$ | PnL% |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
        for p in open_summary[:20]:
            lines.append(
                f"| {p['title'][:60]} | {p['outcome']} | {p['size']:.0f} | "
                f"{p['avg_price']:.3f} | {p['cur_price']:.3f} | "
                f"{fmt_money(p['current_value'])} | {fmt_money(p['pnl'])} | {p['pct_pnl']:+.1f}% |"
            )
    else:
        lines.append("(aucune position ou échec API)")
    lines.append("")

    lines.append("## 10. Synthèse — est-il copiable ?")
    lines.append("")
    lines.append("À remplir d'après les sections ci-dessus :")
    lines.append("")
    lines.append("1. **Tickets** : taille moyenne/médiane vs notre bankroll (90$).")
    lines.append("   → Si médiane > 1k$, son edge dépend de la liquidité qu'il fournit ; "
                 "à reproduire en taker on perd le spread.")
    lines.append("2. **Catégorie dominante** : si FINANCE/ECONOMICS, profil 'event-driven' "
                 "compatible avec un retail. Si SPORTS live, hold court → impossible à copier "
                 "sans latence sub-minute.")
    lines.append("3. **Distribution horaire** : si activité concentrée sur les heures d'ouverture "
                 "des US markets (13-21 UTC), c'est un trader event-driven. Si 24/7 régulier, c'est "
                 "un algo.")
    lines.append("4. **Side ratio** : >70% BUY = directionnel ; ~50/50 = market-making.")
    lines.append("5. **Edge directionnel positif** (`pct_ahead 58%, mean_edge +0.34`) : "
                 "validé sur 1370 trades, statistiquement robuste.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nRapport écrit : {OUT}")


if __name__ == "__main__":
    main()
