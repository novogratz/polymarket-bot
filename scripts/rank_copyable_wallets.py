"""Classement des wallets copiables — combine ``pct_ahead`` et ``mean_edge``.

Lit ``data/wallet_edge_directional.csv`` (sortie de Étude C), applique des
filtres de qualité (sample minimum, mean_edge minimum, exclusion des
``pct_nojump`` trop élevés) et produit un classement par score Z combiné
``pct_ahead`` + ``mean_edge``.

Optionnellement joint ``data/wallet_ytd_ranking.csv`` pour ajouter les
métadonnées (catégorie, PnL YTD, win_rate, hold_time).
"""

from __future__ import annotations

import argparse
import csv
import statistics as stats
from pathlib import Path
from typing import Any

DEFAULT_EDGE_CSV = Path("data/wallet_edge_directional.csv")
DEFAULT_RANKING_CSV = Path("data/wallet_ytd_ranking.csv")
DEFAULT_OUTPUT = Path("data/copyable_wallets_ranked.csv")


def load_edge(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as fp:
        for row in csv.DictReader(fp):
            try:
                row["mean_edge"] = float(row["mean_edge"]) if row["mean_edge"] else 0.0
                row["pct_ahead"] = float(row["pct_ahead"])
                row["pct_chasing"] = float(row["pct_chasing"])
                row["pct_nojump"] = float(row["pct_nojump"])
                row["n_trades_analyzed"] = int(row["n_trades_analyzed"])
                row["total_pnl_usd"] = float(row["total_pnl_usd"])
            except (KeyError, ValueError):
                continue
            rows.append(row)
    return rows


def load_ranking(path: Path) -> dict[str, dict[str, Any]]:
    by_wallet: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return by_wallet
    with path.open() as fp:
        for row in csv.DictReader(fp):
            by_wallet[row["wallet"].lower()] = row
    return by_wallet


def zscore(xs: list[float], x: float) -> float:
    if len(xs) < 2:
        return 0.0
    mu = stats.mean(xs)
    sigma = stats.pstdev(xs)
    if sigma == 0:
        return 0.0
    return (x - mu) / sigma


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge-csv", type=Path, default=DEFAULT_EDGE_CSV)
    parser.add_argument("--ranking-csv", type=Path, default=DEFAULT_RANKING_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top", type=int, default=20, help="Lignes affichées")
    parser.add_argument(
        "--min-trades",
        type=int,
        default=50,
        help="Filtre : n_trades_analyzed >= N (taille d'échantillon)",
    )
    parser.add_argument(
        "--min-pct-ahead",
        type=float,
        default=0.0,
        help="Filtre : pct_ahead >= X (%)",
    )
    parser.add_argument(
        "--min-mean-edge",
        type=float,
        default=0.0,
        help="Filtre : mean_edge >= X",
    )
    parser.add_argument(
        "--max-pct-nojump",
        type=float,
        default=85.0,
        help="Filtre : pct_nojump <= X (%) — exclut les marchés résolus à 99¢",
    )
    parser.add_argument(
        "--sort",
        choices=["score", "pct_ahead", "mean_edge", "pnl"],
        default="score",
        help="Critère de tri",
    )
    parser.add_argument(
        "--weight-ahead",
        type=float,
        default=0.5,
        help="Poids de pct_ahead_z dans le score combiné (0-1, complément = mean_edge_z)",
    )
    args = parser.parse_args()

    rows = load_edge(args.edge_csv)
    rank_meta = load_ranking(args.ranking_csv)

    # Filtres qualité
    filtered = [
        r for r in rows
        if r["n_trades_analyzed"] >= args.min_trades
        and r["pct_ahead"] >= args.min_pct_ahead
        and r["mean_edge"] >= args.min_mean_edge
        and r["pct_nojump"] <= args.max_pct_nojump
    ]

    if not filtered:
        print("Aucun wallet ne passe les filtres.")
        return

    # Score Z combiné
    ahead_vals = [r["pct_ahead"] for r in filtered]
    edge_vals = [r["mean_edge"] for r in filtered]
    wa = max(0.0, min(1.0, args.weight_ahead))
    we = 1.0 - wa
    for r in filtered:
        r["pct_ahead_z"] = zscore(ahead_vals, r["pct_ahead"])
        r["mean_edge_z"] = zscore(edge_vals, r["mean_edge"])
        r["score"] = wa * r["pct_ahead_z"] + we * r["mean_edge_z"]
        meta = rank_meta.get(r["wallet"].lower(), {})
        r["win_rate"] = float(meta.get("win_rate") or 0)
        r["hold_med_min"] = float(meta.get("hold_time_median_min") or 0)
        r["category"] = meta.get("top_category") or r.get("top_category", "")

    # Tri
    sort_key = {
        "score": lambda r: r["score"],
        "pct_ahead": lambda r: r["pct_ahead"],
        "mean_edge": lambda r: r["mean_edge"],
        "pnl": lambda r: r["total_pnl_usd"],
    }[args.sort]
    filtered.sort(key=sort_key, reverse=True)

    # Affichage console
    print(
        f"\nFiltres : n_trades >= {args.min_trades}, pct_ahead >= {args.min_pct_ahead}, "
        f"mean_edge >= {args.min_mean_edge}, pct_nojump <= {args.max_pct_nojump}"
    )
    print(f"Tri : {args.sort} desc  |  Poids score : ahead={wa:.2f} / edge={we:.2f}")
    print(f"Wallets retenus : {len(filtered)} / {len(rows)} dans le CSV\n")

    header = (
        f"{'#':>3} {'wallet':<14} {'username':<28} {'cat':<10} "
        f"{'n':>5} {'%ahead':>7} {'%chase':>7} {'%nojmp':>7} "
        f"{'mean_e':>7} {'WR':>5} {'hold':>6} {'pnl_ytd':>12} {'score':>6}"
    )
    print(header)
    print("-" * len(header))
    for i, r in enumerate(filtered[: args.top], 1):
        short = r["wallet"][:6] + "…" + r["wallet"][-4:]
        username = (r["username"][:26] + "..") if len(r["username"]) > 28 else r["username"]
        print(
            f"{i:>3} {short:<14} {username:<28} {r['category'][:10]:<10} "
            f"{r['n_trades_analyzed']:>5} {r['pct_ahead']:>6.1f}% {r['pct_chasing']:>6.1f}% "
            f"{r['pct_nojump']:>6.1f}% {r['mean_edge']:>+7.3f} {r['win_rate']:>4.0%} "
            f"{r['hold_med_min']:>5.0f}m {r['total_pnl_usd']:>11,.0f}$ {r['score']:>+6.2f}"
        )

    # Écriture CSV complet (tous les filtrés, pas juste top)
    out_cols = [
        "rank", "wallet", "username", "category", "n_trades_analyzed",
        "pct_ahead", "pct_chasing", "pct_nojump", "mean_edge", "median_edge",
        "win_rate", "hold_med_min", "total_pnl_usd",
        "pct_ahead_z", "mean_edge_z", "score",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=out_cols, extrasaction="ignore")
        w.writeheader()
        for i, r in enumerate(filtered, 1):
            r["rank"] = i
            r["category"] = r.get("category", "")
            w.writerow(r)
    print(f"\n→ CSV complet : {args.output} ({len(filtered)} lignes)")


if __name__ == "__main__":
    main()
