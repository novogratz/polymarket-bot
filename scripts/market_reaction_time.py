"""Mesure du temps de réaction du marché Polymarket après un mouvement brutal.

Principe
========

Pour un marché donné, on récupère une série de prix à résolution ~1 minute via
l'endpoint CLOB ``prices-history``. On détecte tous les « jumps » — points
``t`` où le prix bouge d'au moins ``--jump-threshold-cents`` dans une fenêtre
de ``--jump-window-min`` minutes (et où le mouvement net est de même signe et
au moins moitié de l'amplitude, ce qui élimine les oscillations).

Pour chaque jump, ``T0`` est le premier tick du saut, et on mesure :

* la fraction du mouvement final déjà accomplie à T0+30s, +60s, +300s, +900s,
  +3600s, par rapport à ``final_move`` = ``price[T0 + post-jump-window] - price[T0]`` ;
* le ``convergence_time`` = premier instant après T0 où la volatilité mobile
  (std sur 5 min) tombe sous 1¢.

Le but est de calibrer la latence acceptable du tick d'auto-loop : à T+X
secondes après le début du move, quel pourcentage du gain reste à capturer ?

Endpoint utilisé
================

``GET https://clob.polymarket.com/prices-history?market=<clob_token_id>&startTs=<unix>&endTs=<unix>&fidelity=1``

Réponse : ``{"history": [{"t": <unix_sec>, "p": <prix>}, ...]}``. ``fidelity=1``
correspond à ~1 point par minute (espacement réel entre 50 s et 80 s). On
ré-échantillonne sur une grille déterministe d'1 minute par interpolation
forward-fill pour faciliter les calculs de fenêtres glissantes.

Usage
=====

::

    uv run python scripts/market_reaction_time.py \\
        --markets-limit 50 --lookback-hours 72 \\
        --output data/market_reaction_jumps.csv \\
        --report reports/market_reaction_time.md
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_bot.gamma import GammaClient  # noqa: E402
from polymarket_bot.smart_money import market_category  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_OUTPUT = "data/market_reaction_jumps.csv"
DEFAULT_REPORT = "reports/market_reaction_time.md"
SAMPLE_OFFSETS_SEC = (30, 60, 300, 900, 3600)
CONVERGENCE_STD_CENTS = 0.01  # seuil de volatilité (1 ¢) pour la stabilité
CONVERGENCE_WINDOW_MIN = 5


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JumpRow:
    market_id: str
    question: str
    token_id: str
    category: str
    liquidity_usd: float
    jump_ts: int  # T0 (unix sec)
    t0_price: float
    jump_size: float
    direction: str  # "UP" / "DOWN"
    pct_move_at_30s: float
    pct_move_at_60s: float
    pct_move_at_300s: float
    pct_move_at_900s: float
    pct_move_at_3600s: float
    final_move: float
    convergence_time_sec: float


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get_json(url: str, *, timeout: int = 20, retries: int = 5) -> Any:
    """GET avec retry exponentiel sur 429/5xx (rate-limit Cloudflare)."""
    backoff = 1.5
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "polymarket-bot/0.1"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    return None


def fetch_price_history(
    token_id: str,
    *,
    start_ts: int,
    end_ts: int,
    fidelity: int = 1,
    timeout: int = 20,
) -> list[tuple[int, float]]:
    """Retourne la série brute (unix_sec, prix) pour un token CLOB."""
    params = urllib.parse.urlencode(
        {
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        }
    )
    url = f"{CLOB_BASE}/prices-history?{params}"
    try:
        payload = _http_get_json(url, timeout=timeout)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    history = payload.get("history") or []
    out: list[tuple[int, float]] = []
    for entry in history:
        try:
            t = int(entry["t"])
            p = float(entry["p"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((t, p))
    out.sort(key=lambda row: row[0])
    return out


# ---------------------------------------------------------------------------
# Resampling à grille 1 min (forward-fill)
# ---------------------------------------------------------------------------


def resample_to_minute_grid(
    series: list[tuple[int, float]],
    *,
    start_ts: int,
    end_ts: int,
) -> list[tuple[int, float]]:
    """Ré-échantillonne par forward-fill toutes les 60 s entre start_ts et end_ts.

    Le serveur renvoie des points espacés de 50 à 80 s ; pour des fenêtres
    glissantes déterministes on aligne sur une grille exacte d'une minute.
    """
    if not series:
        return []
    grid: list[tuple[int, float]] = []
    i = 0
    last_price = series[0][1]
    for t in range(start_ts, end_ts + 1, 60):
        while i + 1 < len(series) and series[i + 1][0] <= t:
            i += 1
        if series[i][0] <= t:
            last_price = series[i][1]
        grid.append((t, last_price))
    return grid


# ---------------------------------------------------------------------------
# Détection de jump + convergence
# ---------------------------------------------------------------------------


def detect_jumps(
    grid: list[tuple[int, float]],
    *,
    jump_threshold_cents: float,
    jump_window_min: int,
    cooldown_min: int = 30,
) -> list[int]:
    """Retourne la liste des index ``t0`` (sur la grille 1 min) où démarre un jump.

    Critères :
      * amplitude max - min sur ``[t, t+window]`` >= ``jump_threshold``.
      * mouvement net |price[t+window] - price[t]| >= jump_threshold / 2
        et de même signe que (max - min) le long du chemin (élimine les
        oscillations symétriques).
      * cooldown : on garde un seul jump par grappe de ``cooldown_min``.

    On itère sur les minutes ; le seuil est exprimé en fraction de prix (0.05
    pour 5 ¢).
    """
    if len(grid) < jump_window_min + 1:
        return []
    threshold = jump_threshold_cents
    half = threshold / 2.0
    jumps: list[int] = []
    last_jump_idx = -10**9
    for i in range(len(grid) - jump_window_min):
        window = [grid[j][1] for j in range(i, i + jump_window_min + 1)]
        lo, hi = min(window), max(window)
        amp = hi - lo
        if amp < threshold:
            continue
        net = grid[i + jump_window_min][1] - grid[i][1]
        if abs(net) < half:
            continue
        # signe du net doit coller au sens de l'amplitude dominante
        if net > 0 and (hi - grid[i][1]) < (grid[i][1] - lo):
            continue
        if net < 0 and (grid[i][1] - lo) < (hi - grid[i][1]):
            continue
        if i - last_jump_idx < cooldown_min:
            continue
        jumps.append(i)
        last_jump_idx = i
    return jumps


def rolling_std(prices: list[float], window: int) -> list[float]:
    """std mobile par fenêtres glissantes (renvoie len(prices) valeurs, NaN au début)."""
    out: list[float] = []
    for i in range(len(prices)):
        if i + 1 < window:
            out.append(float("nan"))
            continue
        chunk = prices[i + 1 - window : i + 1]
        try:
            out.append(statistics.pstdev(chunk))
        except statistics.StatisticsError:
            out.append(float("nan"))
    return out


def convergence_time_sec(
    grid: list[tuple[int, float]],
    *,
    t0_idx: int,
    post_jump_window_min: int,
    std_threshold: float = CONVERGENCE_STD_CENTS,
    std_window_min: int = CONVERGENCE_WINDOW_MIN,
) -> float:
    """Premier delta (en secondes) après T0 où la std mobile retombe sous le seuil."""
    end = min(len(grid), t0_idx + post_jump_window_min + 1)
    if end - t0_idx < std_window_min + 1:
        return float("nan")
    prices = [grid[j][1] for j in range(t0_idx, end)]
    stds = rolling_std(prices, std_window_min)
    for offset, std in enumerate(stds):
        if offset < std_window_min:
            continue
        if std == std or not (std != std):  # pas NaN
            if std <= std_threshold:
                return float(offset * 60)
    return float("nan")


def pct_move_at(
    grid: list[tuple[int, float]],
    *,
    t0_idx: int,
    delta_sec: int,
    final_move: float,
) -> float:
    """Fraction du ``final_move`` déjà accomplie à T0 + delta_sec.

    Retourne ``nan`` si on n'a pas de donnée ou si ``final_move`` est nul.
    Peut dépasser 1.0 (overshoot) ou être négatif (retour en arrière).
    """
    if final_move == 0 or not _isfinite(final_move):
        return float("nan")
    minute_offset = round(delta_sec / 60)
    target = t0_idx + minute_offset
    if target >= len(grid):
        return float("nan")
    delta_price = grid[target][1] - grid[t0_idx][1]
    return delta_price / final_move


def _isfinite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


def analyse_jump(
    grid: list[tuple[int, float]],
    *,
    t0_idx: int,
    post_jump_window_min: int,
) -> dict[str, float] | None:
    """Calcule métriques pour un jump donné. None si pas assez de données après."""
    if t0_idx + post_jump_window_min >= len(grid):
        return None
    t0_price = grid[t0_idx][1]
    final_price = grid[t0_idx + post_jump_window_min][1]
    final_move = final_price - t0_price
    if abs(final_move) < 1e-6:
        # Pas de move net après la fenêtre — on garde quand même mais pct est nan
        return None
    # détermine jump_size = amplitude max - min dans la window de détection
    return {
        "t0_price": t0_price,
        "final_move": final_move,
        "pct_move_at_30s": pct_move_at(grid, t0_idx=t0_idx, delta_sec=30, final_move=final_move),
        "pct_move_at_60s": pct_move_at(grid, t0_idx=t0_idx, delta_sec=60, final_move=final_move),
        "pct_move_at_300s": pct_move_at(grid, t0_idx=t0_idx, delta_sec=300, final_move=final_move),
        "pct_move_at_900s": pct_move_at(grid, t0_idx=t0_idx, delta_sec=900, final_move=final_move),
        "pct_move_at_3600s": pct_move_at(
            grid, t0_idx=t0_idx, delta_sec=3600, final_move=final_move
        ),
        "convergence_time_sec": convergence_time_sec(
            grid,
            t0_idx=t0_idx,
            post_jump_window_min=post_jump_window_min,
        ),
    }


# ---------------------------------------------------------------------------
# Pool de marchés
# ---------------------------------------------------------------------------


def fetch_market_pool(
    *,
    markets_limit: int,
    liquidity_min: float,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    """Renvoie les marchés actifs les plus liquides au-dessus du seuil."""
    client = GammaClient(timeout=timeout)
    # On utilise directement l'API Gamma avec order=volume24hr (pas exposé dans
    # GammaClient.get_markets, donc requête manuelle).
    params = urllib.parse.urlencode(
        {
            "active": "true",
            "closed": "false",
            "limit": str(max(markets_limit * 3, 100)),
            "order": "volume24hr",
            "ascending": "false",
        }
    )
    url = f"{GAMMA_BASE}/markets?{params}"
    try:
        payload = _http_get_json(url, timeout=timeout)
    except Exception as exc:
        print(f"[ERR] fetch_market_pool: {exc}", flush=True)
        return []
    if not isinstance(payload, list):
        return []
    selected: list[dict[str, Any]] = []
    for market in payload:
        if not isinstance(market, dict):
            continue
        liquidity = market.get("liquidityNum") or market.get("liquidity") or 0
        try:
            liquidity = float(liquidity)
        except (TypeError, ValueError):
            liquidity = 0.0
        if liquidity < liquidity_min:
            continue
        if market.get("closed"):
            continue
        # On veut un marché qui n'est pas trop proche d'expiration : >= 1 h
        end_date = market.get("endDate")
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_to_close = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_to_close < 1:
                    continue
            except Exception:
                pass
        selected.append(market)
        if len(selected) >= markets_limit:
            break
    return selected


def extract_token_ids(market: dict[str, Any]) -> list[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [str(token) for token in raw if token]


# ---------------------------------------------------------------------------
# Pipeline par marché
# ---------------------------------------------------------------------------


def process_market(
    market: dict[str, Any],
    *,
    start_ts: int,
    end_ts: int,
    jump_threshold_cents: float,
    jump_window_min: int,
    post_jump_window_min: int,
    cooldown_min: int = 30,
) -> list[JumpRow]:
    """Détecte tous les jumps sur chaque token YES/NO d'un marché."""
    question = str(market.get("question") or "")
    slug = str(market.get("slug") or "")
    category = market_category(question, slug)
    market_id = str(market.get("id") or market.get("conditionId") or "")
    liquidity = float(market.get("liquidityNum") or market.get("liquidity") or 0)
    rows: list[JumpRow] = []
    for token_id in extract_token_ids(market):
        raw = fetch_price_history(token_id, start_ts=start_ts, end_ts=end_ts)
        if len(raw) < jump_window_min + post_jump_window_min:
            continue
        grid = resample_to_minute_grid(raw, start_ts=start_ts, end_ts=end_ts)
        if len(grid) < jump_window_min + post_jump_window_min:
            continue
        jump_indices = detect_jumps(
            grid,
            jump_threshold_cents=jump_threshold_cents,
            jump_window_min=jump_window_min,
            cooldown_min=cooldown_min,
        )
        for idx in jump_indices:
            metrics = analyse_jump(grid, t0_idx=idx, post_jump_window_min=post_jump_window_min)
            if metrics is None:
                continue
            t0_ts = grid[idx][0]
            # amplitude effective du jump = écart entre extrêmes pendant la
            # window de détection (sert pour le CSV)
            window_prices = [grid[j][1] for j in range(idx, idx + jump_window_min + 1)]
            jump_size = max(window_prices) - min(window_prices)
            direction = "UP" if metrics["final_move"] > 0 else "DOWN"
            rows.append(
                JumpRow(
                    market_id=market_id,
                    question=question,
                    token_id=token_id,
                    category=category,
                    liquidity_usd=liquidity,
                    jump_ts=t0_ts,
                    t0_price=metrics["t0_price"],
                    jump_size=jump_size,
                    direction=direction,
                    pct_move_at_30s=metrics["pct_move_at_30s"],
                    pct_move_at_60s=metrics["pct_move_at_60s"],
                    pct_move_at_300s=metrics["pct_move_at_300s"],
                    pct_move_at_900s=metrics["pct_move_at_900s"],
                    pct_move_at_3600s=metrics["pct_move_at_3600s"],
                    final_move=metrics["final_move"],
                    convergence_time_sec=metrics["convergence_time_sec"],
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Statistiques + rapport
# ---------------------------------------------------------------------------


def _quantile(values: Iterable[float], q: float) -> float:
    cleaned = [v for v in values if _isfinite(v)]
    if not cleaned:
        return float("nan")
    cleaned.sort()
    if len(cleaned) == 1:
        return cleaned[0]
    pos = (len(cleaned) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(cleaned) - 1)
    frac = pos - lo
    return cleaned[lo] * (1 - frac) + cleaned[hi] * frac


def _median(values: Iterable[float]) -> float:
    return _quantile(values, 0.5)


def slippage_table(rows: list[JumpRow]) -> dict[str, dict[str, float]]:
    """Pour chaque offset, renvoie médiane et p75 de pct_move_at."""
    table: dict[str, dict[str, float]] = {}
    field_for = {
        30: "pct_move_at_30s",
        60: "pct_move_at_60s",
        300: "pct_move_at_300s",
        900: "pct_move_at_900s",
        3600: "pct_move_at_3600s",
    }
    for offset_sec, field in field_for.items():
        values = [getattr(row, field) for row in rows]
        table[str(offset_sec)] = {
            "p50": _median(values),
            "p25": _quantile(values, 0.25),
            "p75": _quantile(values, 0.75),
            "n": float(len([v for v in values if _isfinite(v)])),
        }
    return table


def write_csv(rows: list[JumpRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        # CSV avec header seulement
        fields = list(JumpRow.__dataclass_fields__.keys())
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(JumpRow.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _fmt_pct(value: float) -> str:
    if not _isfinite(value):
        return "n/a"
    return f"{value * 100:+.1f}%"


def _fmt_sec(value: float) -> str:
    if not _isfinite(value):
        return "n/a"
    return f"{value:.0f}s"


def write_report(
    rows: list[JumpRow],
    *,
    path: Path,
    markets_scanned: int,
    args: argparse.Namespace,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = slippage_table(rows)
    conv_times = [r.convergence_time_sec for r in rows]

    # Catégories
    by_cat: dict[str, list[JumpRow]] = {}
    for row in rows:
        by_cat.setdefault(row.category, []).append(row)

    # Bandes de liquidité
    bands = {
        "low (5k-50k)": (5_000.0, 50_000.0),
        "mid (50k-500k)": (50_000.0, 500_000.0),
        "high (>500k)": (500_000.0, float("inf")),
    }

    lines: list[str] = []
    lines.append(f"# Temps de réaction Polymarket — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
    lines.append("## Paramètres\n")
    lines.append(f"- Marchés scannés : **{markets_scanned}**")
    lines.append(f"- Lookback : **{args.lookback_hours} h**")
    lines.append(f"- Seuil jump : **{args.jump_threshold_cents * 100:.1f} ¢** en ≤ **{args.jump_window_min} min**")
    lines.append(f"- Fenêtre post-jump : **{args.post_jump_window_min} min**")
    lines.append(f"- Jumps détectés : **{len(rows)}**\n")

    lines.append("## Courbe de slippage\n")
    lines.append("Part du mouvement final déjà accomplie à T0+X (1.0 = move complet, négatif = retour en arrière, >1.0 = overshoot).\n")
    lines.append("| Délai | médiane | p25 | p75 | n |")
    lines.append("|------:|--------:|----:|----:|--:|")
    for offset in (30, 60, 300, 900, 3600):
        stats = table[str(offset)]
        lines.append(
            f"| T+{offset}s | {_fmt_pct(stats['p50'])} | {_fmt_pct(stats['p25'])} | {_fmt_pct(stats['p75'])} | {int(stats['n'])} |"
        )
    lines.append("")

    lines.append("## Temps de convergence\n")
    if conv_times:
        lines.append(f"- médiane : **{_fmt_sec(_median(conv_times))}**")
        lines.append(f"- p25 : {_fmt_sec(_quantile(conv_times, 0.25))}")
        lines.append(f"- p75 : {_fmt_sec(_quantile(conv_times, 0.75))}")
        lines.append(f"- p90 : {_fmt_sec(_quantile(conv_times, 0.90))}")
    lines.append("")

    lines.append("## Breakdown par catégorie\n")
    lines.append("| Cat | n | p50 @60s | p50 @300s | p50 @900s |")
    lines.append("|:----|--:|---------:|----------:|----------:|")
    for cat, group in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        p60 = _median([r.pct_move_at_60s for r in group])
        p300 = _median([r.pct_move_at_300s for r in group])
        p900 = _median([r.pct_move_at_900s for r in group])
        lines.append(f"| {cat} | {len(group)} | {_fmt_pct(p60)} | {_fmt_pct(p300)} | {_fmt_pct(p900)} |")
    lines.append("")

    lines.append("## Breakdown par liquidité\n")
    lines.append("| Bande | n | p50 @60s | p50 @300s | p50 @900s |")
    lines.append("|:------|--:|---------:|----------:|----------:|")
    for label, (lo, hi) in bands.items():
        group = [r for r in rows if lo <= r.liquidity_usd < hi]
        if not group:
            lines.append(f"| {label} | 0 | n/a | n/a | n/a |")
            continue
        p60 = _median([r.pct_move_at_60s for r in group])
        p300 = _median([r.pct_move_at_300s for r in group])
        p900 = _median([r.pct_move_at_900s for r in group])
        lines.append(f"| {label} | {len(group)} | {_fmt_pct(p60)} | {_fmt_pct(p300)} | {_fmt_pct(p900)} |")
    lines.append("")

    # Recommandation
    lines.append("## Recommandation latence\n")
    lines.append(
        "⚠️ Limite méthodologique : l'endpoint `prices-history` a une résolution "
        "effective ≈ 1 point / minute. Les deltas T+30s sont donc dominés par 0% "
        "(même minute que T0) ; T+60s est le premier point réellement informatif.\n"
    )
    # On commence à T+60s pour la recommandation (T+30s sous-résolution).
    threshold_capture = 0.50
    delay_under_threshold: int | None = None
    for offset in (60, 300, 900, 3600):
        p50 = table[str(offset)]["p50"]
        if _isfinite(p50) and p50 <= threshold_capture:
            delay_under_threshold = offset
            break
    if delay_under_threshold is not None:
        remaining = 1.0 - table[str(delay_under_threshold)]["p50"]
        lines.append(
            f"À T+{delay_under_threshold}s la médiane du move déjà accompli est "
            f"{_fmt_pct(table[str(delay_under_threshold)]['p50'])}, "
            f"il reste donc **{_fmt_pct(remaining)}** du gain à capturer. "
            "C'est la borne au-delà de laquelle copier devient peu rentable."
        )
    else:
        lines.append(
            "Même à T+3600s la médiane reste sous 50% du move final — soit le seuil "
            "est trop strict, soit l'échantillon est dominé par des marchés qui "
            "continuent à tendanciel après le burst initial."
        )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--markets-limit", type=int, default=50)
    p.add_argument("--liquidity-min", type=float, default=5000.0)
    p.add_argument("--lookback-hours", type=float, default=72.0)
    p.add_argument(
        "--jump-threshold-cents",
        type=float,
        default=0.05,
        help="Amplitude minimale du saut, en fraction de prix (0.05 = 5 ¢).",
    )
    p.add_argument("--jump-window-min", type=int, default=5)
    p.add_argument("--post-jump-window-min", type=int, default=60)
    p.add_argument("--cooldown-min", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--output", default=DEFAULT_OUTPUT)
    p.add_argument("--report", default=DEFAULT_REPORT)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = int(time.time())
    start_ts = now - int(args.lookback_hours * 3600)

    print(
        f"[1/4] Récupération du pool de marchés (limit={args.markets_limit}, "
        f"liq≥${args.liquidity_min:.0f})...",
        flush=True,
    )
    pool = fetch_market_pool(markets_limit=args.markets_limit, liquidity_min=args.liquidity_min)
    print(f"      → {len(pool)} marchés retenus.", flush=True)
    if not pool:
        print("Aucun marché — arrêt.", flush=True)
        return 1

    print(
        f"[2/4] Détection des jumps (lookback={args.lookback_hours}h, "
        f"seuil={args.jump_threshold_cents * 100:.1f}¢/{args.jump_window_min}min, "
        f"concurrence={args.concurrency})...",
        flush=True,
    )

    all_rows: list[JumpRow] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool_exec:
        futures = {
            pool_exec.submit(
                process_market,
                market,
                start_ts=start_ts,
                end_ts=now,
                jump_threshold_cents=args.jump_threshold_cents,
                jump_window_min=args.jump_window_min,
                post_jump_window_min=args.post_jump_window_min,
                cooldown_min=args.cooldown_min,
            ): market
            for market in pool
        }
        for future in as_completed(futures):
            market = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                print(f"      [WARN] {market.get('id')}: {exc}", flush=True)
                rows = []
            all_rows.extend(rows)
            completed += 1
            if completed % 10 == 0 or completed == len(pool):
                print(
                    f"      [{completed}/{len(pool)}] {len(all_rows)} jumps cumulés.",
                    flush=True,
                )

    print(f"[3/4] Écriture CSV → {args.output}", flush=True)
    write_csv(all_rows, Path(args.output))

    print(f"[4/4] Rapport markdown → {args.report}", flush=True)
    write_report(
        all_rows,
        path=Path(args.report),
        markets_scanned=len(pool),
        args=args,
    )

    # Petite synthèse stdout
    if all_rows:
        table = slippage_table(all_rows)
        print("\n  Slippage médian :", flush=True)
        for offset in (30, 60, 300, 900, 3600):
            stats = table[str(offset)]
            print(f"    T+{offset:>4}s : {_fmt_pct(stats['p50'])} (n={int(stats['n'])})", flush=True)
    else:
        print("\n  Aucun jump détecté.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
