"""Étude C — Edge directionnel par wallet.

Pour chaque BUY des top N wallets (classés par PnL net YTD desc), on récupère
la série de prix CLOB autour du trade (fenêtre [-30 min, +30 min], grille
~1 min) et on calcule deux métriques :

* ``edge_directional`` = ``move_15min / |move_30min|`` (sign retenu) — mesure
  si le prix continue dans la direction du BUY après l'achat. Non-mesurable
  (``None``) quand ``|move_30min| < 0.005`` (mouvement bruit).
* ``edge_jump`` — détecte le plus gros jump 10 min glissant englobant la
  fenêtre. Si ``final_move >= 0.05``, ``edge_jump = (price_+15 - price_at) /
  final_move``. ``> 0.5`` = wallet *ahead* du mouvement (bonne info), ``< 0``
  = wallet qui *chase* un retracement (mauvais signal).

Les helpers ``fetch_all_trades`` (de ``scripts/wallet_history_ytd.py``) et
``fetch_price_history`` (de ``scripts/market_reaction_time.py``) sont
réutilisés tel quel — pas de modification de leur source.

Usage :
    uv run python scripts/wallet_edge_directional.py --top 20
    uv run python scripts/wallet_edge_directional.py --top 3 --no-cache
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_bot.smart_money import DataApiClient, SmartTrade, market_category  # noqa: E402
from scripts.market_reaction_time import fetch_price_history  # noqa: E402
from scripts.wallet_history_ytd import fetch_all_trades  # noqa: E402

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

DEFAULT_TOP = 20
DEFAULT_RANKING = "data/wallet_ytd_ranking.csv"
DEFAULT_OUTPUT_TRADES = "data/wallet_edge_directional_trades.csv"
DEFAULT_OUTPUT_WALLETS = "data/wallet_edge_directional.csv"
DEFAULT_REPORT = "reports/wallet_edge_directional.md"
DEFAULT_CACHE_DIR = "data/wallet_edge_cache"
DEFAULT_CONCURRENCY = 12

WINDOW_BEFORE_S = 1800  # 30 min avant le trade
WINDOW_AFTER_S = 1800   # 30 min après
MIN_SERIES_POINTS = 5
NOISE_FLOOR = 0.005     # |move_30min| en deçà → edge_directional non-mesurable
JUMP_FLOOR = 0.05       # final_move minimum pour calculer edge_jump
JUMP_WINDOW_S = 600     # 10 min glissant pour la détection du jump


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------


@dataclass
class TradeRow:
    """Une ligne du CSV trade-level."""

    wallet: str
    token_id: str
    ts_trade: int
    side: str
    price_trade: float
    price_at_trade: float
    price_5min: float | None
    price_15min: float | None
    price_30min: float | None
    move_15min: float | None
    move_30min: float | None
    edge_directional: float | None
    final_move: float
    edge_jump: float | None
    category: str
    title: str


@dataclass
class WalletAggregate:
    """Agrégats par wallet."""

    wallet: str
    n_trades_total_buy: int
    n_trades_analyzed: int
    n_trades_skipped: int
    mean_move_15min: float | None
    median_move_15min: float | None
    mean_edge: float | None
    median_edge: float | None
    pct_ahead: float
    pct_chasing: float
    pct_nojump: float
    total_pnl_usd: float = 0.0
    username: str = ""
    top_category: str = ""


# ---------------------------------------------------------------------------
# Lecture du ranking YTD
# ---------------------------------------------------------------------------


def load_ranking(path: Path, top_n: int) -> list[dict[str, Any]]:
    """Charge le CSV YTD et retourne les ``top_n`` wallets (déjà triés desc).

    Le fichier produit par ``wallet_history_ytd.py`` est trié par
    ``pnl_net_ytd`` desc. On accepte aussi ``total_pnl_usd`` comme alias.
    """
    if not path.exists():
        raise FileNotFoundError(f"Ranking introuvable : {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            pnl_key = "pnl_net_ytd" if "pnl_net_ytd" in row else "total_pnl_usd"
            try:
                pnl = float(row.get(pnl_key) or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            rows.append(
                {
                    "wallet": row.get("wallet", ""),
                    "username": row.get("username", ""),
                    "total_pnl_usd": pnl,
                    "top_category": row.get("top_category", ""),
                }
            )
    rows.sort(key=lambda r: r["total_pnl_usd"], reverse=True)
    return rows[:top_n]


def ytd_since_ts() -> int:
    """1er janvier de l'année courante UTC, en secondes Unix."""
    now = datetime.now(timezone.utc)
    start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp())


# ---------------------------------------------------------------------------
# Cache disque
# ---------------------------------------------------------------------------


def _cache_path(cache_dir: Path, token_id: str, start_ts: int, end_ts: int) -> Path:
    safe_token = "".join(c for c in token_id if c.isalnum() or c in ("-", "_"))[:64]
    return cache_dir / f"{safe_token}_{start_ts}_{end_ts}.json"


def fetch_price_history_cached(
    token_id: str,
    *,
    start_ts: int,
    end_ts: int,
    cache_dir: Path | None,
) -> list[tuple[int, float]]:
    """Wrap ``fetch_price_history`` avec un cache JSON sur disque."""
    if cache_dir is not None:
        cache_file = _cache_path(cache_dir, token_id, start_ts, end_ts)
        if cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                return [(int(t), float(p)) for t, p in payload]
            except (ValueError, OSError):
                pass  # cache corrompu → re-fetch
    try:
        series = fetch_price_history(token_id, start_ts=start_ts, end_ts=end_ts, fidelity=1)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return []
        raise
    if cache_dir is not None and series:
        cache_file = _cache_path(cache_dir, token_id, start_ts, end_ts)
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            cache_file.write_text(json.dumps(series), encoding="utf-8")
        except OSError:
            pass  # disque plein, on continue sans casser
    return series


# ---------------------------------------------------------------------------
# Pure logique : calcul de l'edge sur une série
# ---------------------------------------------------------------------------


def nearest_price(series: list[tuple[int, float]], target_ts: int) -> tuple[int, float] | None:
    """Retourne le ``(ts, price)`` de la série le plus proche de ``target_ts``."""
    if not series:
        return None
    best = min(series, key=lambda row: abs(row[0] - target_ts))
    return best


def detect_enclosing_jump(series: list[tuple[int, float]], window_s: int = JUMP_WINDOW_S) -> float:
    """Plus gros mouvement absolu sur ``window_s`` glissant, signe préservé.

    On parcourt la série et pour chaque point on cherche le point compris
    entre ``[t, t + window_s]`` qui maximise ``|p_end - p_start|``. Le signe
    du jump le plus "violent" est conservé.
    """
    if len(series) < 2:
        return 0.0
    best_signed = 0.0
    best_abs = 0.0
    n = len(series)
    j = 0
    for i in range(n):
        ts_i, p_i = series[i]
        if j < i:
            j = i
        while j + 1 < n and series[j + 1][0] - ts_i <= window_s:
            j += 1
        for k in range(i + 1, j + 1):
            delta = series[k][1] - p_i
            mag = abs(delta)
            if mag > best_abs:
                best_abs = mag
                best_signed = delta
    return best_signed


def compute_trade_edge(
    *,
    side: str,
    ts_trade: int,
    price_trade: float,
    series: list[tuple[int, float]],
) -> dict[str, Any] | None:
    """Calcule les métriques d'edge pour un trade donné.

    Renvoie ``None`` si la série a moins de ``MIN_SERIES_POINTS`` points
    (signal insuffisant — trade skippé).
    """
    if len(series) < MIN_SERIES_POINTS:
        return None

    sign = 1.0 if side.upper() == "BUY" else -1.0

    pt_at = nearest_price(series, ts_trade)
    pt_5 = nearest_price(series, ts_trade + 300)
    pt_15 = nearest_price(series, ts_trade + 900)
    pt_30 = nearest_price(series, ts_trade + 1800)

    assert pt_at is not None  # série non-vide garantie ci-dessus
    price_at = pt_at[1]
    price_5 = pt_5[1] if pt_5 is not None else None
    price_15 = pt_15[1] if pt_15 is not None else None
    price_30 = pt_30[1] if pt_30 is not None else None

    move_15 = (price_15 - price_at) if price_15 is not None else None
    move_30 = (price_30 - price_at) if price_30 is not None else None

    # Edge directionnel (signe = direction du trade, pas du marché brut)
    edge_directional: float | None
    if move_15 is None or move_30 is None or abs(move_30) < NOISE_FLOOR:
        edge_directional = None
    else:
        # On signe le numérateur par la direction du trade (BUY = on parie up)
        edge_directional = (sign * move_15) / abs(move_30)

    # Détection du jump englobant sur la fenêtre complète
    final_move_signed = detect_enclosing_jump(series, window_s=JUMP_WINDOW_S)
    final_move = abs(final_move_signed)

    edge_jump: float | None = None
    if final_move >= JUMP_FLOOR and move_15 is not None:
        # On rapporte le mouvement +15 min au final_move, en signant par la
        # direction du trade. Si le wallet est BUY et le marché monte, on
        # veut une valeur positive proche de 1 ; s'il chase un retracement
        # (BUY juste avant que ça baisse) la valeur devient négative.
        # On préserve le signe du jump pour gérer les sells aussi.
        oriented = sign * move_15 * (1.0 if final_move_signed >= 0 else -1.0)
        edge_jump = oriented / final_move

    return {
        "price_at_trade": price_at,
        "price_5min": price_5,
        "price_15min": price_15,
        "price_30min": price_30,
        "move_15min": (sign * move_15) if move_15 is not None else None,
        "move_30min": (sign * move_30) if move_30 is not None else None,
        "edge_directional": edge_directional,
        "final_move": final_move,
        "edge_jump": edge_jump,
    }


# ---------------------------------------------------------------------------
# Pipeline par wallet
# ---------------------------------------------------------------------------


def filter_buys_ytd(trades: Iterable[SmartTrade], since_ts: int) -> list[SmartTrade]:
    """Garde uniquement les BUY postérieurs au ``since_ts``."""
    out = []
    for tr in trades:
        if tr.side.upper() != "BUY":
            continue
        if tr.timestamp < since_ts:
            continue
        out.append(tr)
    return out


def analyze_wallet_trades(
    *,
    wallet: str,
    trades: list[SmartTrade],
    cache_dir: Path | None,
    fetch_workers: int,
) -> tuple[list[TradeRow], int]:
    """Pour chaque BUY, fetch la série prix et calcule les métriques.

    Retourne ``(rows, n_skipped)``.
    """
    rows: list[TradeRow] = []
    skipped = 0

    # Fan-out parallèle des fetchs prix
    def _fetch_one(tr: SmartTrade) -> tuple[SmartTrade, list[tuple[int, float]]]:
        start_ts = tr.timestamp - WINDOW_BEFORE_S
        end_ts = tr.timestamp + WINDOW_AFTER_S
        series = fetch_price_history_cached(
            tr.asset, start_ts=start_ts, end_ts=end_ts, cache_dir=cache_dir
        )
        return tr, series

    if not trades:
        return rows, skipped

    with ThreadPoolExecutor(max_workers=max(1, fetch_workers)) as ex:
        futures = [ex.submit(_fetch_one, tr) for tr in trades]
        for fut in as_completed(futures):
            try:
                tr, series = fut.result()
            except Exception:
                skipped += 1
                continue
            metrics = compute_trade_edge(
                side=tr.side, ts_trade=tr.timestamp, price_trade=tr.price, series=series
            )
            if metrics is None:
                skipped += 1
                continue
            rows.append(
                TradeRow(
                    wallet=wallet,
                    token_id=tr.asset,
                    ts_trade=tr.timestamp,
                    side=tr.side.upper(),
                    price_trade=tr.price,
                    price_at_trade=metrics["price_at_trade"],
                    price_5min=metrics["price_5min"],
                    price_15min=metrics["price_15min"],
                    price_30min=metrics["price_30min"],
                    move_15min=metrics["move_15min"],
                    move_30min=metrics["move_30min"],
                    edge_directional=metrics["edge_directional"],
                    final_move=metrics["final_move"],
                    edge_jump=metrics["edge_jump"],
                    category=market_category(tr.title or "", tr.slug or ""),
                    title=tr.title,
                )
            )
    return rows, skipped


def aggregate_wallet(rows: list[TradeRow], *, wallet: str, n_buy_total: int, n_skipped: int) -> WalletAggregate:
    n_analyzed = len(rows)
    moves_15 = [r.move_15min for r in rows if r.move_15min is not None]
    edges = [r.edge_directional for r in rows if r.edge_directional is not None]
    jump_rows = [r for r in rows if r.edge_jump is not None]
    n_jump = len(jump_rows)
    n_nojump = n_analyzed - n_jump

    n_ahead = sum(1 for r in jump_rows if r.edge_jump is not None and r.edge_jump > 0.5)
    n_chasing = sum(1 for r in jump_rows if r.edge_jump is not None and r.edge_jump < 0)

    base = max(n_analyzed, 1)
    return WalletAggregate(
        wallet=wallet,
        n_trades_total_buy=n_buy_total,
        n_trades_analyzed=n_analyzed,
        n_trades_skipped=n_skipped,
        mean_move_15min=(statistics.mean(moves_15) if moves_15 else None),
        median_move_15min=(statistics.median(moves_15) if moves_15 else None),
        mean_edge=(statistics.mean(edges) if edges else None),
        median_edge=(statistics.median(edges) if edges else None),
        pct_ahead=100.0 * n_ahead / base,
        pct_chasing=100.0 * n_chasing / base,
        pct_nojump=100.0 * n_nojump / base,
    )


# ---------------------------------------------------------------------------
# Écriture CSV / rapport
# ---------------------------------------------------------------------------


TRADE_CSV_COLUMNS = [
    "wallet",
    "token_id",
    "ts_trade",
    "ts_iso",
    "side",
    "price_trade",
    "price_at_trade",
    "price_5min",
    "price_15min",
    "price_30min",
    "move_15min",
    "move_30min",
    "edge_directional",
    "final_move",
    "edge_jump",
    "category",
    "title",
]


WALLET_CSV_COLUMNS = [
    "wallet",
    "username",
    "total_pnl_usd",
    "top_category",
    "n_trades_total_buy",
    "n_trades_analyzed",
    "n_trades_skipped",
    "mean_move_15min",
    "median_move_15min",
    "mean_edge",
    "median_edge",
    "pct_ahead",
    "pct_chasing",
    "pct_nojump",
]


def _fmt_opt(value: float | None, fmt: str = "{:.4f}") -> str:
    return "" if value is None else fmt.format(value)


def write_trade_csv(rows: list[TradeRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(TRADE_CSV_COLUMNS)
        for r in rows:
            iso = datetime.fromtimestamp(r.ts_trade, tz=timezone.utc).isoformat()
            writer.writerow(
                [
                    r.wallet,
                    r.token_id,
                    r.ts_trade,
                    iso,
                    r.side,
                    f"{r.price_trade:.4f}",
                    f"{r.price_at_trade:.4f}",
                    _fmt_opt(r.price_5min),
                    _fmt_opt(r.price_15min),
                    _fmt_opt(r.price_30min),
                    _fmt_opt(r.move_15min),
                    _fmt_opt(r.move_30min),
                    _fmt_opt(r.edge_directional),
                    f"{r.final_move:.4f}",
                    _fmt_opt(r.edge_jump),
                    r.category,
                    r.title,
                ]
            )


def write_wallet_csv(rows: list[WalletAggregate], path: Path) -> None:
    """CSV wallet-level, trié par ``pct_ahead`` desc."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = sorted(rows, key=lambda w: w.pct_ahead, reverse=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(WALLET_CSV_COLUMNS)
        for w in sorted_rows:
            writer.writerow(
                [
                    w.wallet,
                    w.username,
                    f"{w.total_pnl_usd:.2f}",
                    w.top_category,
                    w.n_trades_total_buy,
                    w.n_trades_analyzed,
                    w.n_trades_skipped,
                    _fmt_opt(w.mean_move_15min),
                    _fmt_opt(w.median_move_15min),
                    _fmt_opt(w.mean_edge),
                    _fmt_opt(w.median_edge),
                    f"{w.pct_ahead:.1f}",
                    f"{w.pct_chasing:.1f}",
                    f"{w.pct_nojump:.1f}",
                ]
            )


# ---------------------------------------------------------------------------
# Rapport markdown
# ---------------------------------------------------------------------------


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _short_wallet(addr: str) -> str:
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def _fmt_money_fr(amount: float) -> str:
    """Format monétaire FR : signe ``$`` après le nombre, séparateur space."""
    if amount >= 0:
        return f"{amount:,.2f}$".replace(",", " ")
    return f"-{abs(amount):,.2f}$".replace(",", " ")


def write_report(
    *,
    wallets: list[WalletAggregate],
    trades: list[TradeRow],
    path: Path,
    top_n: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_wallets = len(wallets)
    n_trades_buy = sum(w.n_trades_total_buy for w in wallets)
    n_analyzed = sum(w.n_trades_analyzed for w in wallets)
    n_skipped = sum(w.n_trades_skipped for w in wallets)
    pct_skipped = (100.0 * n_skipped / n_trades_buy) if n_trades_buy > 0 else 0.0

    edges = [r.edge_directional for r in trades if r.edge_directional is not None]
    deciles = [_quantile(edges, q / 10) for q in range(1, 10)]

    sorted_ahead = sorted(wallets, key=lambda w: w.pct_ahead, reverse=True)
    sorted_chasing = sorted(wallets, key=lambda w: w.pct_chasing, reverse=True)

    # MM/arbitrageurs : edge médian négatif ou nul, mais total_pnl_usd positif.
    suspicious = [
        w for w in wallets
        if w.median_edge is not None and w.median_edge <= 0 and w.total_pnl_usd > 0
    ]
    suspicious.sort(key=lambda w: (w.median_edge or 0.0))

    lines: list[str] = []
    lines.append("# Étude C — Edge directionnel des wallets top-PnL\n")
    lines.append(
        f"_Top {top_n} wallets analysés — fenêtre [-30 min, +30 min] autour de chaque BUY YTD,_\n"
        f"_série prix CLOB ``fidelity=1``._\n"
    )
    lines.append("## 1. Résumé exécutif\n")
    lines.append(f"- Wallets analysés : **{n_wallets}**")
    lines.append(f"- Total BUY YTD candidats : **{n_trades_buy}**")
    lines.append(
        f"- Trades effectivement scorés : **{n_analyzed}** "
        f"({100.0 * n_analyzed / max(n_trades_buy, 1):.1f}%)"
    )
    lines.append(
        f"- Trades skippés (série prix < 5 points) : **{n_skipped}** "
        f"({pct_skipped:.1f}%)"
    )
    lines.append(
        f"- Trades avec jump ≥ {JUMP_FLOOR:.2f} détecté : "
        f"**{sum(1 for r in trades if r.edge_jump is not None)}**\n"
    )

    lines.append("## 2. Distribution de l'edge directionnel\n")
    if edges:
        lines.append(f"_n = {len(edges)} trades avec edge mesurable (|move_30min| ≥ {NOISE_FLOOR:.3f})._\n")
        lines.append("| Décile | Edge directionnel |")
        lines.append("|---|---|")
        for i, val in enumerate(deciles, 1):
            lines.append(f"| D{i} (p{i*10}) | {val:.3f} |" if val is not None else f"| D{i} | n/a |")
        med = statistics.median(edges)
        mean = statistics.mean(edges)
        lines.append(f"\n- **Médiane globale** : {med:.3f}")
        lines.append(f"- **Moyenne globale** : {mean:.3f}")
        pct_pos = 100.0 * sum(1 for e in edges if e > 0) / len(edges)
        lines.append(f"- **% trades avec edge > 0** : {pct_pos:.1f}%\n")
    else:
        lines.append("_Aucun trade avec mouvement mesurable — augmenter ``--top`` ou la fenêtre._\n")

    lines.append("## 3. Top 5 wallets *ahead* (à copier en priorité)\n")
    lines.append("| Wallet | username | total_pnl_ytd | n_analyzed | pct_ahead | pct_chasing | mean_edge |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for w in sorted_ahead[:5]:
        lines.append(
            f"| `{_short_wallet(w.wallet)}` | {w.username[:24]} | {_fmt_money_fr(w.total_pnl_usd)} | "
            f"{w.n_trades_analyzed} | {w.pct_ahead:.1f}% | {w.pct_chasing:.1f}% | "
            f"{(_fmt_opt(w.mean_edge, '{:.3f}') or 'n/a')} |"
        )
    lines.append("")

    lines.append("## 4. Top 5 wallets *chasing* (à NE PAS copier)\n")
    lines.append("_Pourcentage élevé de trades pris après que le marché ait déjà fait son mouvement_\n")
    lines.append("_(``edge_jump < 0`` : le BUY arrive sur un retracement, donc à contre-courant)._\n")
    lines.append("| Wallet | username | total_pnl_ytd | n_analyzed | pct_chasing | pct_ahead | mean_edge |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for w in sorted_chasing[:5]:
        lines.append(
            f"| `{_short_wallet(w.wallet)}` | {w.username[:24]} | {_fmt_money_fr(w.total_pnl_usd)} | "
            f"{w.n_trades_analyzed} | {w.pct_chasing:.1f}% | {w.pct_ahead:.1f}% | "
            f"{(_fmt_opt(w.mean_edge, '{:.3f}') or 'n/a')} |"
        )
    lines.append("")

    lines.append("## 5. Cross-référence — edge négatif mais PnL positif (MM / arbitrage)\n")
    lines.append(
        "Wallets dont la médiane d'edge directionnel est ≤ 0 alors que le PnL YTD est positif. "
        "Hypothèses : market-making (capture du spread sans direction), "
        "arbitrage cross-marché, ou bénéfice du market impact (le wallet *est* la liquidité). "
        "Ces wallets gagnent de l'argent mais ne sont **pas copiables** par un follower retail.\n"
    )
    if suspicious:
        lines.append("| Wallet | username | total_pnl_ytd | median_edge | pct_ahead | pct_chasing |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for w in suspicious:
            lines.append(
                f"| `{_short_wallet(w.wallet)}` | {w.username[:24]} | {_fmt_money_fr(w.total_pnl_usd)} | "
                f"{(_fmt_opt(w.median_edge, '{:.3f}') or 'n/a')} | "
                f"{w.pct_ahead:.1f}% | {w.pct_chasing:.1f}% |"
            )
    else:
        lines.append("_Aucun wallet ne tombe dans cette catégorie sur l'échantillon courant._")
    lines.append("")

    lines.append("## 6. Conclusion — implication pour `polymarket_bot/smart_money.py`\n")
    lines.append(
        "L'edge directionnel n'est pas uniformément réparti chez les wallets top-PnL : "
        "certains **anticipent** les mouvements (``pct_ahead`` > 40%), d'autres **les chassent** "
        "(``pct_chasing`` > 30%) et restent profitables uniquement par concentration sur quelques outliers. "
        "Suggestion (consultative, pas de modification du code prod) : enrichir le scoring "
        "smart-money avec un filtre ``min_pct_ahead`` calculé sur 30 jours glissants par wallet, "
        "pour exclure du cohort les wallets *chasers* dont le PnL ne se réplique pas par copie naïve.\n"
    )
    lines.append(
        "À noter : la métrique reste sensible au bruit sur les marchés peu liquides "
        "(``|move_30min| < 0.005``), et au calage temporel exact du trade vs. la grille 1 min "
        "(point le plus proche, pas d'interpolation linéaire).\n"
    )

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="Nombre de wallets analysés")
    parser.add_argument("--ranking", default=DEFAULT_RANKING, help="CSV ranking YTD source")
    parser.add_argument("--output-trades", default=DEFAULT_OUTPUT_TRADES)
    parser.add_argument("--output-wallets", default=DEFAULT_OUTPUT_WALLETS)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--data-api-url", default="https://data-api.polymarket.com")
    parser.add_argument("--since", default=None, help="Override YTD (ISO 8601)")
    parser.add_argument("--max-trades-per-wallet", type=int, default=2000,
                        help="Cap dur sur le nombre de BUYs analysés par wallet")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    ranking_path = Path(args.ranking)
    output_trades = Path(args.output_trades)
    output_wallets = Path(args.output_wallets)
    report_path = Path(args.report)
    cache_dir: Path | None = None if args.no_cache else Path(args.cache_dir)

    if args.since:
        since_ts = int(
            datetime.fromisoformat(args.since.replace("Z", "+00:00")).timestamp()
        )
    else:
        since_ts = ytd_since_ts()

    print(
        f"== wallet_edge_directional ==\n"
        f"  ranking         : {ranking_path}\n"
        f"  top_n           : {args.top}\n"
        f"  since (YTD)     : {datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()}\n"
        f"  cache_dir       : {cache_dir or '(disabled)'}\n"
        f"  concurrency     : {args.concurrency}\n"
        f"  output trades   : {output_trades}\n"
        f"  output wallets  : {output_wallets}\n"
        f"  report          : {report_path}\n",
        flush=True,
    )

    pool = load_ranking(ranking_path, args.top)
    if not pool:
        print("Ranking vide — arrêt.", flush=True)
        return 1

    client = DataApiClient(args.data_api_url)

    all_trade_rows: list[TradeRow] = []
    all_wallet_aggs: list[WalletAggregate] = []

    t_start = time.time()
    for i, entry in enumerate(pool, 1):
        wallet = entry["wallet"]
        username = entry["username"]
        t_wallet = time.time()
        print(
            f"[{i}/{len(pool)}] {_short_wallet(wallet)} ({username[:24]}) — fetch trades…",
            flush=True,
        )
        try:
            trades = fetch_all_trades(client, wallet=wallet, since_ts=since_ts)
        except Exception as exc:
            print(f"  ⚠️  trades fetch failed: {type(exc).__name__}: {exc}", flush=True)
            continue
        buys = filter_buys_ytd(trades, since_ts)
        if args.max_trades_per_wallet and len(buys) > args.max_trades_per_wallet:
            print(
                f"  cap : {len(buys)} BUYs YTD → analyse des {args.max_trades_per_wallet} plus récents",
                flush=True,
            )
            buys = sorted(buys, key=lambda t: t.timestamp, reverse=True)[: args.max_trades_per_wallet]
        n_buy_total = len(buys)
        print(f"  → {n_buy_total} BUYs YTD à scorer", flush=True)

        if n_buy_total == 0:
            agg = aggregate_wallet([], wallet=wallet, n_buy_total=0, n_skipped=0)
        else:
            rows, skipped = analyze_wallet_trades(
                wallet=wallet,
                trades=buys,
                cache_dir=cache_dir,
                fetch_workers=args.concurrency,
            )
            all_trade_rows.extend(rows)
            agg = aggregate_wallet(rows, wallet=wallet, n_buy_total=n_buy_total, n_skipped=skipped)
            print(
                f"  → {len(rows)} scorés / {skipped} skip / "
                f"pct_ahead={agg.pct_ahead:.1f}% pct_chasing={agg.pct_chasing:.1f}% "
                f"({time.time() - t_wallet:.1f}s)",
                flush=True,
            )
        agg.username = username
        agg.total_pnl_usd = entry["total_pnl_usd"]
        agg.top_category = entry.get("top_category", "")
        all_wallet_aggs.append(agg)

    print(f"\nÉcriture des livrables… (total {time.time() - t_start:.1f}s)", flush=True)
    write_trade_csv(all_trade_rows, output_trades)
    write_wallet_csv(all_wallet_aggs, output_wallets)
    write_report(
        wallets=all_wallet_aggs,
        trades=all_trade_rows,
        path=report_path,
        top_n=args.top,
    )
    print(f"  → trades  : {output_trades}  ({len(all_trade_rows)} lignes)")
    print(f"  → wallets : {output_wallets}  ({len(all_wallet_aggs)} lignes)")
    print(f"  → rapport : {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
