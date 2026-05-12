"""Analyse du CSV produit par scripts/wallet_history_ytd.py.

Produit :
  - Stats globales (N, distribution PnL, % winners).
  - Distribution PnL en déciles + concentration top X%.
  - Stats par ``top_category`` (n, win rate, PnL moyen/médian, hold-time).
  - Identification de cohortes comportementales (sports HF vs politics long
    vs autres) et leurs caractéristiques.
  - Suggestions de filtres concrets pour la stratégie smart-money.
  - Optionnel : rapport Markdown via ``--output``.

Usage :
    uv run python scripts/analyze_top_wallets.py data/wallet_ytd_ranking.csv
    uv run python scripts/analyze_top_wallets.py data/wallet_ytd_ranking.csv \\
        --top 50 --output reports/top_wallets_ytd.md
"""

from __future__ import annotations

import argparse
import csv
import io
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WalletRow:
    rank: int
    wallet: str
    username: str
    pnl_net: float
    pnl_realized: float
    pnl_unrealized: float
    volume_buy: float
    n_trades: int
    n_matched_sells: int
    win_rate: float
    hold_time_median_min: float
    top_category: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path, help="CSV produit par wallet_history_ytd.py")
    parser.add_argument("--top", type=int, default=50, help="Taille de l'échantillon top (défaut 50)")
    parser.add_argument("--output", type=Path, help="Chemin du rapport markdown (sinon stdout seul)")
    parser.add_argument(
        "--min-trades-cohort",
        type=int,
        default=5,
        help="Min trades pour entrer dans l'analyse cohortes (défaut 5)",
    )
    return parser.parse_args(argv)


def load_csv(path: Path) -> list[WalletRow]:
    rows: list[WalletRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(
                WalletRow(
                    rank=int(r["rank"]),
                    wallet=r["wallet"],
                    username=r["username"],
                    pnl_net=float(r["pnl_net_ytd"]),
                    pnl_realized=float(r["pnl_realized"]),
                    pnl_unrealized=float(r["pnl_unrealized"]),
                    volume_buy=float(r["volume_buy_ytd"]),
                    n_trades=int(r["n_trades"]),
                    n_matched_sells=int(r["n_matched_sells"]),
                    win_rate=float(r["win_rate"]),
                    hold_time_median_min=float(r["hold_time_median_min"]),
                    top_category=r["top_category"],
                )
            )
    return rows


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _safe_mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _safe_median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def section_global(out: list[str], rows: list[WalletRow]) -> None:
    pnl_all = [r.pnl_net for r in rows]
    winners = [r for r in rows if r.pnl_net > 0]
    total = len(rows)
    out.append("## Vue globale")
    out.append("")
    out.append(f"- Échantillon         : **{total} wallets** (filtre n_trades ≥ 5 appliqué amont)")
    out.append(f"- Total PnL net YTD   : ${sum(pnl_all):>14,.0f}")
    out.append(f"- Médiane PnL net     : ${_safe_median(pnl_all):>14,.2f}")
    out.append(f"- Moyenne PnL net     : ${_safe_mean(pnl_all):>14,.2f}")
    out.append(f"- Winners (PnL > 0)   : {len(winners)} ({len(winners) / total:.1%})")
    out.append(f"- Losers  (PnL < 0)   : {total - len(winners)} ({(total - len(winners)) / total:.1%})")
    out.append("")


def section_distribution(out: list[str], rows: list[WalletRow]) -> None:
    pnl_all = sorted((r.pnl_net for r in rows), reverse=True)
    total_pnl = sum(p for p in pnl_all if p > 0)  # somme des gains uniquement (concentration)
    out.append("## Distribution PnL net")
    out.append("")
    out.append("**Déciles** (PnL net YTD trié desc)")
    out.append("")
    out.append("| Décile | Borne sup ($) | Médiane ($) | Borne inf ($) |")
    out.append("|---:|---:|---:|---:|")
    n = len(pnl_all)
    for d in range(10):
        lo = d * n // 10
        hi = (d + 1) * n // 10
        chunk = pnl_all[lo:hi]
        if not chunk:
            continue
        out.append(f"| D{d + 1} | {chunk[0]:>12,.0f} | {statistics.median(chunk):>12,.0f} | {chunk[-1]:>12,.0f} |")
    out.append("")

    out.append("**Concentration des gains** (somme PnL positif uniquement)")
    out.append("")
    out.append(f"- Somme totale des gains : ${total_pnl:,.0f}")
    for top_pct in (0.01, 0.05, 0.10, 0.25):
        top_n = max(1, int(n * top_pct))
        top_sum = sum(pnl_all[:top_n])
        share = top_sum / total_pnl if total_pnl > 0 else 0.0
        out.append(f"- Top {top_pct * 100:>4.0f}% ({top_n:>3d} wallets) capture **{share:>5.1%}** des gains (${top_sum:,.0f})")
    out.append("")


def section_by_category(out: list[str], rows: list[WalletRow]) -> None:
    by_cat: dict[str, list[WalletRow]] = defaultdict(list)
    for r in rows:
        by_cat[r.top_category].append(r)

    out.append("## Par top_category (wallet's dominant category)")
    out.append("")
    out.append("| Catégorie | N | % | PnL méd | PnL moy | Winners % | WinRate méd | Hold méd (min) | n_trades méd |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    cat_rows = sorted(by_cat.items(), key=lambda kv: -len(kv[1]))
    total = len(rows)
    for cat, items in cat_rows:
        pnls = [r.pnl_net for r in items]
        winners = sum(1 for p in pnls if p > 0)
        out.append(
            f"| {cat} | {len(items)} | {len(items) / total:.0%} | "
            f"${_safe_median(pnls):,.0f} | ${_safe_mean(pnls):,.0f} | "
            f"{winners / len(items):.0%} | "
            f"{_safe_median([r.win_rate for r in items]):.0%} | "
            f"{_safe_median([r.hold_time_median_min for r in items]):,.0f} | "
            f"{_safe_median([r.n_trades for r in items]):,.0f} |"
        )
    out.append("")


def section_cohorts(out: list[str], rows: list[WalletRow]) -> None:
    """Cohortes heuristiques :
    - sports_hf  : SPORTS + n_trades >= 1000 + hold_med < 60 min
    - sports_swing : SPORTS + n_trades < 1000
    - politics_long : POLITICS + hold_med >= 1000 min
    - politics_quick : POLITICS + hold_med < 1000 min
    - other  : tout le reste
    """
    cohorts: dict[str, list[WalletRow]] = defaultdict(list)
    for r in rows:
        cat = r.top_category
        if cat == "SPORTS":
            if r.n_trades >= 1000 and r.hold_time_median_min < 60.0:
                cohorts["sports_hf"].append(r)
            else:
                cohorts["sports_swing"].append(r)
        elif cat == "POLITICS":
            if r.hold_time_median_min >= 1000.0:
                cohorts["politics_long"].append(r)
            else:
                cohorts["politics_quick"].append(r)
        else:
            cohorts["other"].append(r)

    out.append("## Cohortes comportementales")
    out.append("")
    out.append("Heuristique :")
    out.append("- **sports_hf**       : SPORTS + n_trades ≥ 1000 + hold méd < 60 min (arbitrage live)")
    out.append("- **sports_swing**    : SPORTS + le reste")
    out.append("- **politics_long**   : POLITICS + hold méd ≥ 1000 min (~16h+)")
    out.append("- **politics_quick**  : POLITICS + hold méd < 1000 min")
    out.append("- **other**           : autres catégories")
    out.append("")
    out.append("| Cohorte | N | PnL méd | PnL moy | Winners % | WinRate méd | Vol méd | n_trades méd | Hold méd |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    order = ["sports_hf", "sports_swing", "politics_long", "politics_quick", "other"]
    for name in order:
        items = cohorts.get(name, [])
        if not items:
            continue
        pnls = [r.pnl_net for r in items]
        winners = sum(1 for p in pnls if p > 0)
        out.append(
            f"| {name} | {len(items)} | "
            f"${_safe_median(pnls):,.0f} | ${_safe_mean(pnls):,.0f} | "
            f"{winners / len(items):.0%} | "
            f"{_safe_median([r.win_rate for r in items]):.0%} | "
            f"${_safe_median([r.volume_buy for r in items]):,.0f} | "
            f"{_safe_median([r.n_trades for r in items]):,.0f} | "
            f"{_safe_median([r.hold_time_median_min for r in items]):,.0f}m |"
        )
    out.append("")


def section_winner_profile(out: list[str], rows: list[WalletRow]) -> None:
    winners = [r for r in rows if r.pnl_net > 0 and r.n_matched_sells >= 5]
    losers = [r for r in rows if r.pnl_net < 0 and r.n_matched_sells >= 5]
    if not winners or not losers:
        return

    out.append("## Profil winner vs loser (≥ 5 SELLs matchés)")
    out.append("")
    out.append("| Métrique | Winners (n) | Losers (n) | Spread |")
    out.append("|---|---:|---:|---:|")

    def row(name: str, w_val: float, l_val: float, fmt: str = ",.2f", as_pct: bool = False) -> str:
        if as_pct:
            return f"| {name} | {w_val:.1%} | {l_val:.1%} | {(w_val - l_val) * 100:+.1f}pp |"
        return f"| {name} | {w_val:{fmt}} | {l_val:{fmt}} | {w_val - l_val:+{fmt}} |"

    out.append(f"| Échantillon | {len(winners)} | {len(losers)} | — |")
    out.append(row("Win rate médian", _safe_median([r.win_rate for r in winners]), _safe_median([r.win_rate for r in losers]), as_pct=True))
    out.append(row("Hold méd (min)", _safe_median([r.hold_time_median_min for r in winners]), _safe_median([r.hold_time_median_min for r in losers]), fmt=",.0f"))
    out.append(row("Volume BUY méd", _safe_median([r.volume_buy for r in winners]), _safe_median([r.volume_buy for r in losers]), fmt=",.0f"))
    out.append(row("n_trades méd", _safe_median([float(r.n_trades) for r in winners]), _safe_median([float(r.n_trades) for r in losers]), fmt=",.0f"))
    out.append(row("n_matched_sells méd", _safe_median([float(r.n_matched_sells) for r in winners]), _safe_median([float(r.n_matched_sells) for r in losers]), fmt=",.0f"))
    out.append("")


def section_filter_suggestions(out: list[str], rows: list[WalletRow], top_n: int) -> None:
    top = sorted(rows, key=lambda r: -r.pnl_net)[:top_n]
    if not top:
        return

    win_rates = sorted(r.win_rate for r in top if r.n_matched_sells >= 5)
    holds = sorted(r.hold_time_median_min for r in top)
    vols = sorted(r.volume_buy for r in top)
    n_tr = sorted(r.n_trades for r in top)

    out.append(f"## Suggestions de filtres (basé sur le top {top_n})")
    out.append("")
    out.append("Plages observées chez les meilleurs wallets — à considérer comme **plancher** pour qualifier")
    out.append("un wallet en cohorte smart-money (sélection plus stricte que le simple top leaderboard) :")
    out.append("")
    if win_rates:
        out.append(f"- **Win rate** (≥ 5 SELLs) : p25 = {_pct(win_rates, 0.25):.0%}, médiane = {_pct(win_rates, 0.50):.0%}, p75 = {_pct(win_rates, 0.75):.0%}")
        out.append(f"  → suggéré : `MIN_TRADER_WIN_RATE` ≈ **{_pct(win_rates, 0.25):.0%}** (p25 du top)")
    out.append(f"- **Volume BUY YTD** : p25 = ${_pct(vols, 0.25):,.0f}, médiane = ${_pct(vols, 0.50):,.0f}")
    out.append(f"  → suggéré : `MIN_TRADER_VOLUME_YTD` ≈ **${_pct(vols, 0.25):,.0f}**")
    out.append(f"- **n_trades YTD** : p25 = {_pct(n_tr, 0.25):,.0f}, médiane = {_pct(n_tr, 0.50):,.0f}")
    out.append(f"- **Hold time médian** : p25 = {_pct(holds, 0.25):,.0f} min, médiane = {_pct(holds, 0.50):,.0f} min")
    out.append("")

    cat_counts = Counter(r.top_category for r in top)
    out.append(f"**Distribution catégorie dans le top {top_n} :**")
    for cat, count in cat_counts.most_common():
        out.append(f"  - {cat:<10} : {count:>3d} ({count / len(top):>4.0%})")
    out.append("")


def section_top_table(out: list[str], rows: list[WalletRow], top_n: int) -> None:
    top = sorted(rows, key=lambda r: -r.pnl_net)[:top_n]
    out.append(f"## Top {top_n} (par PnL net YTD)")
    out.append("")
    out.append("| # | User | PnL net | Real | Unreal | Vol BUY | n | Win% | Hold méd | Cat |")
    out.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for i, r in enumerate(top, 1):
        user = (r.username[:22] + "…") if len(r.username) > 23 else r.username
        out.append(
            f"| {i} | {user} | ${r.pnl_net:,.0f} | ${r.pnl_realized:,.0f} | ${r.pnl_unrealized:,.0f} | "
            f"${r.volume_buy:,.0f} | {r.n_trades} | {r.win_rate:.0%} | "
            f"{r.hold_time_median_min:,.0f}m | {r.top_category} |"
        )
    out.append("")


def build_report(rows: list[WalletRow], *, top_n: int) -> str:
    out: list[str] = []
    out.append(f"# Analyse YTD — {len(rows)} wallets Polymarket")
    out.append("")
    section_global(out, rows)
    section_distribution(out, rows)
    section_by_category(out, rows)
    section_cohorts(out, rows)
    section_winner_profile(out, rows)
    section_filter_suggestions(out, rows, top_n)
    section_top_table(out, rows, top_n=min(top_n, 20))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.csv.exists():
        print(f"❌ CSV introuvable : {args.csv}", file=sys.stderr)
        return 1
    rows = load_csv(args.csv)
    if not rows:
        print("❌ CSV vide.", file=sys.stderr)
        return 1
    report = build_report(rows, top_n=args.top)
    sys.stdout.write(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"\n→ rapport écrit dans {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
