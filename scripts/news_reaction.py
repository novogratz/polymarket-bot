"""Mesure de la latence de reaction Polymarket apres une news a timestamp connu.

Contrairement au script `market_reaction_time.py` (detection endogene des jumps
sans hypothese sur la cause), ici l'utilisateur fournit une liste d'evenements
news -> marche dans `data/news_events.json`, chacun avec :

* `news_ts_utc`  -- timestamp ISO 8601 UTC de la publication de la news ;
* `market_token_id` -- token CLOB du cote YES du marche cible ;
* `expected_direction` -- "UP" ou "DOWN" (indicatif, pas utilise pour le calcul) ;
* `source`, `source_url`, `headline`, `notes`.

Pour chaque event on recupere la serie de prix sur la fenetre
[T_news - lookback, T_news + post_window], on aligne sur une grille d'une
minute (reutilise `resample_to_minute_grid` du module endogene), puis on
calcule :

* `latency_to_first_move_sec` = premier instant >= T_news ou
  |price - price[T_news]| >= 0.02 (2 centimes) ;
* `move_at_X` pour X dans {60s, 5min, 15min, 60min} = delta de prix par
  rapport a T_news (signe conserve) ;
* `final_move` = price[T_news + post_window] - price[T_news] ;
* `pct_at_X` = move_at_X / final_move (peut etre >1 si overshoot, <0 si
  retour, NaN si final_move ~= 0).

Le rapport markdown contient :

* tableau des events avec sources et latence par event ;
* courbe de slippage agregee (mediane, p25, p75) ;
* comparaison avec la version endogene (`reports/market_reaction_time.md`) ;
* recommandation de fenetre operationnelle pour le bot.

Usage::

    uv run python scripts/news_reaction.py
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# On reutilise les helpers du script endogene (pas de copier-coller).
from scripts.market_reaction_time import (  # noqa: E402
    fetch_price_history,
    resample_to_minute_grid,
    _http_get_json,  # type: ignore[attr-defined]
)


DEFAULT_EVENTS = "data/news_events.json"
DEFAULT_OUTPUT = "data/news_reaction_events.csv"
DEFAULT_REPORT = "reports/news_reaction.md"
MOVE_THRESHOLD = 0.02  # 2 centimes pour considerer qu'un "premier move" est detectable
SAMPLE_OFFSETS_SEC = (60, 300, 900, 3600)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventResult:
    event_id: str
    news_ts_utc: str
    source: str
    source_url: str
    headline: str
    market_question: str
    market_token_id: str
    expected_direction: str
    t0_price: float            # prix a T_news (apres alignement minute)
    final_move: float          # move sur la fenetre complete
    final_abs_move: float
    latency_to_first_move_sec: float  # NaN si jamais detectable
    move_at_60s: float
    move_at_300s: float
    move_at_900s: float
    move_at_3600s: float
    pct_at_60s: float          # fraction du final accompli
    pct_at_300s: float
    pct_at_900s: float
    pct_at_3600s: float
    n_points_pre: int
    n_points_post: int
    notes: str = ""


# ---------------------------------------------------------------------------
# Helpers numeriques
# ---------------------------------------------------------------------------


def _isfinite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


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


def parse_news_ts(value: str) -> int:
    """Convertit 'YYYY-MM-DDTHH:MM:SSZ' (UTC) en unix seconds."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def latency_to_first_move(
    grid: list[tuple[int, float]],
    *,
    t0_idx: int,
    threshold: float,
) -> float:
    """Premier offset (en secondes) >= T0 ou |price - price[T0]| >= threshold.

    Retourne NaN si on ne franchit jamais le seuil sur la fenetre disponible.
    """
    if t0_idx >= len(grid):
        return float("nan")
    p0 = grid[t0_idx][1]
    t0 = grid[t0_idx][0]
    for i in range(t0_idx, len(grid)):
        if abs(grid[i][1] - p0) >= threshold - 1e-9:
            return float(grid[i][0] - t0)
    return float("nan")


def move_at(
    grid: list[tuple[int, float]],
    *,
    t0_idx: int,
    delta_sec: int,
) -> float:
    """Delta de prix (signe conserve) a T0 + delta_sec ; NaN si hors fenetre."""
    minute_offset = round(delta_sec / 60)
    target = t0_idx + minute_offset
    if target >= len(grid) or target < 0:
        return float("nan")
    return grid[target][1] - grid[t0_idx][1]


def pct_of_final(move: float, final_move: float) -> float:
    """Fraction du `final_move` deja accomplie. NaN si final ~ 0."""
    if not _isfinite(move) or not _isfinite(final_move):
        return float("nan")
    if abs(final_move) < 1e-6:
        return float("nan")
    return move / final_move


# ---------------------------------------------------------------------------
# Pipeline par event
# ---------------------------------------------------------------------------


def process_event(
    event: dict,
    *,
    lookback_min: int,
    post_window_hours: float,
) -> EventResult | None:
    try:
        news_ts = parse_news_ts(event["news_ts_utc"])
    except Exception as exc:  # pragma: no cover - format-checked manually
        print(f"[WARN] {event.get('event_id')}: parse_news_ts -> {exc}", flush=True)
        return None
    token_id = str(event["market_token_id"])
    start_ts = news_ts - lookback_min * 60
    end_ts = news_ts + int(post_window_hours * 3600)

    raw = fetch_price_history(token_id, start_ts=start_ts, end_ts=end_ts)
    # Filtrer aux points reellement dans la fenetre (l'API renvoie parfois
    # des points jusqu'a now, hors fenetre demandee).
    raw = [(t, p) for t, p in raw if start_ts <= t <= end_ts]
    if len(raw) < 5:
        print(
            f"[WARN] {event.get('event_id')}: serie insuffisante ({len(raw)} points)",
            flush=True,
        )
        return None
    grid = resample_to_minute_grid(raw, start_ts=start_ts, end_ts=end_ts)
    if len(grid) < 5:
        return None

    # T0 = index dans la grille correspondant a news_ts (arrondi a la minute).
    t0_idx = max(0, (news_ts - grid[0][0]) // 60)
    t0_idx = min(t0_idx, len(grid) - 1)
    t0_price = grid[t0_idx][1]
    final_price = grid[-1][1]
    final_move = final_price - t0_price

    latency = latency_to_first_move(grid, t0_idx=t0_idx, threshold=MOVE_THRESHOLD)
    m60 = move_at(grid, t0_idx=t0_idx, delta_sec=60)
    m300 = move_at(grid, t0_idx=t0_idx, delta_sec=300)
    m900 = move_at(grid, t0_idx=t0_idx, delta_sec=900)
    m3600 = move_at(grid, t0_idx=t0_idx, delta_sec=3600)

    return EventResult(
        event_id=event.get("event_id", ""),
        news_ts_utc=event["news_ts_utc"],
        source=event.get("source", ""),
        source_url=event.get("source_url", ""),
        headline=event.get("headline", ""),
        market_question=event.get("market_question", ""),
        market_token_id=token_id,
        expected_direction=event.get("expected_direction", ""),
        t0_price=t0_price,
        final_move=final_move,
        final_abs_move=abs(final_move),
        latency_to_first_move_sec=latency,
        move_at_60s=m60,
        move_at_300s=m300,
        move_at_900s=m900,
        move_at_3600s=m3600,
        pct_at_60s=pct_of_final(m60, final_move),
        pct_at_300s=pct_of_final(m300, final_move),
        pct_at_900s=pct_of_final(m900, final_move),
        pct_at_3600s=pct_of_final(m3600, final_move),
        n_points_pre=t0_idx,
        n_points_post=len(grid) - t0_idx,
        notes=event.get("notes", ""),
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def write_csv(rows: list[EventResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(EventResult.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


# ---------------------------------------------------------------------------
# Rapport markdown
# ---------------------------------------------------------------------------


def _fmt_pct(value: float) -> str:
    if not _isfinite(value):
        return "n/a"
    return f"{value * 100:+.1f}%"


def _fmt_cents(value: float) -> str:
    if not _isfinite(value):
        return "n/a"
    return f"{value * 100:+.2f}c"


def _fmt_sec(value: float) -> str:
    if not _isfinite(value):
        return "n/a"
    if value >= 3600:
        return f"{value / 3600:.1f}h"
    if value >= 60:
        return f"{value / 60:.1f}min"
    return f"{value:.0f}s"


def write_report(
    rows: list[EventResult],
    *,
    path: Path,
    lookback_min: int,
    post_window_hours: float,
    n_input_events: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append(
        f"# Latence Polymarket news-ancree -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
    )
    lines.append("## Methodologie\n")
    lines.append(
        f"- T0 = timestamp UTC publie d'une news verifiee (BLS, FOMC, Truth Social, etc.).\n"
        f"- Serie de prix CLOB sur [T0 - {lookback_min} min, T0 + {post_window_hours:.1f} h], "
        f"resolution effective ~1 point/minute.\n"
        f"- Premier move = premier instant t >= T0 ou |price(t) - price(T0)| >= "
        f"{MOVE_THRESHOLD*100:.0f}c.\n"
        f"- Move final = price(T0 + post_window) - price(T0). pct_at_X = move_at_X / final.\n"
    )
    lines.append(
        f"- Events ingerez : **{n_input_events}** -- events exploitables (>=5 points) : **{len(rows)}**.\n"
    )
    lines.append(
        "- Limite : l'endpoint CLOB est echantillonne ~1/min, donc tout move < 60s "
        "est invisible par construction. La latence renvoyee est une **borne superieure** "
        "arrondie a la minute superieure.\n"
    )

    if not rows:
        lines.append("\n_Aucun event exploitable._\n")
        path.write_text("\n".join(lines), encoding="utf-8")
        return

    lines.append("## Events retenus\n")
    lines.append(
        "| event | T0 (UTC) | source | direction | t0_price | final | latency 1er move |"
    )
    lines.append("|:------|:--------|:------|:----------|---------:|------:|----------------:|")
    for r in rows:
        lines.append(
            f"| {r.event_id} | {r.news_ts_utc} | [{r.source}]({r.source_url}) | "
            f"{r.expected_direction} | {r.t0_price:.3f} | {_fmt_cents(r.final_move)} | "
            f"{_fmt_sec(r.latency_to_first_move_sec)} |"
        )
    lines.append("")

    lines.append("### Detail par event\n")
    for r in rows:
        lines.append(f"**{r.event_id}** -- {r.headline}")
        lines.append(
            f"- Marche : `{r.market_question}` (token `{r.market_token_id[:18]}...`)"
        )
        lines.append(
            f"- T0 price = {r.t0_price:.3f}, final move = {_fmt_cents(r.final_move)} "
            f"(direction observee : {'UP' if r.final_move > 0 else 'DOWN' if r.final_move < 0 else 'flat'})"
        )
        lines.append(
            f"- Latence 1er move >= 2c : **{_fmt_sec(r.latency_to_first_move_sec)}**"
        )
        lines.append(
            f"- Move a T+60s : {_fmt_cents(r.move_at_60s)} ({_fmt_pct(r.pct_at_60s)} du final)"
        )
        lines.append(
            f"- Move a T+5min : {_fmt_cents(r.move_at_300s)} ({_fmt_pct(r.pct_at_300s)})"
        )
        lines.append(
            f"- Move a T+15min : {_fmt_cents(r.move_at_900s)} ({_fmt_pct(r.pct_at_900s)})"
        )
        lines.append(
            f"- Move a T+1h : {_fmt_cents(r.move_at_3600s)} ({_fmt_pct(r.pct_at_3600s)})"
        )
        if r.notes:
            lines.append(f"- Note : {r.notes}")
        lines.append("")

    # Pour les agregats on ne retient que les events avec un signal exploitable
    # (|final_move| >= 3c). Sinon les pct sont domines par le bruit autour de 0.
    MIN_SIGNAL = 0.03
    signal_rows = [r for r in rows if r.final_abs_move >= MIN_SIGNAL]
    rejected = len(rows) - len(signal_rows)
    lines.append("## Filtrage agregat\n")
    lines.append(
        f"Pour eviter de moyenner sur des marches a final_abs_move < 3c (signal noye "
        f"dans le bruit ~1c de la grille minute), on agrege uniquement sur **{len(signal_rows)}"
        f" events** (rejet de {rejected} a faible move).\n"
    )

    # Aggregats
    lines.append("## Courbe de slippage news-ancree (agregee)\n")
    lines.append(
        "Fraction du move final deja accomplie a T0+X. 0% = rien n'a bouge ; 100% = "
        "tout le move est deja la ; >100% = overshoot puis retour ; <0% = mouvement "
        "initial en sens contraire.\n"
    )
    lines.append("| Delai | n | p25 | mediane | p75 |")
    lines.append("|------:|--:|----:|--------:|----:|")
    fields = [
        (60, "pct_at_60s"),
        (300, "pct_at_300s"),
        (900, "pct_at_900s"),
        (3600, "pct_at_3600s"),
    ]
    for offset, field in fields:
        values = [getattr(r, field) for r in signal_rows]
        n = sum(1 for v in values if _isfinite(v))
        lines.append(
            f"| T+{offset}s | {n} | {_fmt_pct(_quantile(values, 0.25))} | "
            f"{_fmt_pct(_median(values))} | {_fmt_pct(_quantile(values, 0.75))} |"
        )
    lines.append("")

    lines.append("## Latence news -> premier move (>= 2c)\n")
    # La latence est calculee sur TOUS les events exploitables : meme un marche
    # a faible bouge final peut produire un tick observable a T+X.
    latencies = [r.latency_to_first_move_sec for r in rows]
    n_detect = sum(1 for v in latencies if _isfinite(v))
    lines.append(f"- events avec premier move detectable : **{n_detect} / {len(rows)}**")
    lines.append(f"- p25 : {_fmt_sec(_quantile(latencies, 0.25))}")
    lines.append(f"- **mediane : {_fmt_sec(_median(latencies))}**")
    lines.append(f"- p75 : {_fmt_sec(_quantile(latencies, 0.75))}")
    lines.append("")

    lines.append("## Comparaison endogene vs news-ancree\n")
    lines.append(
        "Le script `market_reaction_time.py` ancre T0 sur le **debut endogene** du saut "
        "(premier tick ou l'amplitude depasse le seuil sur 5 min) ; il observe donc, "
        "par construction, ~0% deja accompli a T+60s.\n"
    )
    lines.append(
        "Le script news-ancree ancre T0 sur le **timestamp publie** ; la fraction deja "
        "accomplie a T+60s reflete la latence humaine + propagation order-flow.\n"
    )
    p50_60 = _median([r.pct_at_60s for r in signal_rows])
    p50_300 = _median([r.pct_at_300s for r in signal_rows])
    p50_900 = _median([r.pct_at_900s for r in signal_rows])
    lines.append("| Delai | endogene (rapport precedent) | news-ancree (ici) |")
    lines.append("|------:|-----------------------------:|------------------:|")
    lines.append(f"| T+60s  | +0.0% | {_fmt_pct(p50_60)} |")
    lines.append(f"| T+5min | +115.4% | {_fmt_pct(p50_300)} |")
    lines.append(f"| T+15min | +100.0% | {_fmt_pct(p50_900)} |")
    lines.append("")
    lines.append(
        "Si la valeur news-ancree a T+60s est > 0%, cela signifie qu'**une partie du "
        "move s'est faite avant que T0 ne soit declenche** -- typique d'une fuite ou "
        "d'une diffusion par etapes (cas elections : la news se diffuse par paliers).\n"
    )

    lines.append("## Verdict -- fenetre operationnelle du bot\n")
    median_latency = _median(latencies)
    if not _isfinite(median_latency):
        lines.append(
            "Echantillon insuffisant pour conclure -- aucun premier move detectable.\n"
        )
    else:
        # On veut capter au moins 50% du move restant -> il faut etre en marche
        # avant que p50_pct n'atteigne 50%.
        threshold_capture = 0.5
        delay_under_threshold: int | None = None
        for offset, field in fields:
            p50 = _median([getattr(r, field) for r in signal_rows])
            if _isfinite(p50) and p50 >= threshold_capture:
                delay_under_threshold = offset
                break
        if delay_under_threshold is not None:
            lines.append(
                f"- A T+{delay_under_threshold}s la mediane du move accompli atteint "
                f"~{threshold_capture*100:.0f}%. **Le bot doit reagir avant cette borne** "
                f"pour capter la moitie restante du move.\n"
            )
        else:
            lines.append(
                "- Meme a T+1h la mediane reste sous 50% -- soit l'echantillon n'a pas "
                "encore converge, soit les news selectionnees ont des effets diffuses.\n"
            )
        lines.append(
            f"- Latence humaine mediane observee (news -> premier tick >= 2c sur Polymarket) : "
            f"**~{_fmt_sec(median_latency)}**. C'est le delai minimal entre la "
            f"publication et l'apparition d'un signal exploitable sur la chain. "
            f"Tout pipeline bot-side qui depasse cette enveloppe (fetch leaderboard + "
            f"scan + reverse-lookup + placement d'ordre) capte deja un marche en mouvement.\n"
        )
    lines.append(
        f"**Attention echantillon** : {len(rows)} events exploitables (apres rejet des "
        "marches sans serie disponible ou crees apres la news). Echantillon trop petit "
        "pour des conclusions statistiquement robustes -- chiffres indicatifs.\n"
    )

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--events-file", default=DEFAULT_EVENTS)
    p.add_argument("--lookback-min", type=int, default=30)
    p.add_argument("--post-window-hours", type=float, default=6.0)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--output", default=DEFAULT_OUTPUT)
    p.add_argument("--report", default=DEFAULT_REPORT)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    events_path = Path(args.events_file)
    if not events_path.exists():
        print(f"[ERR] events file missing: {events_path}", flush=True)
        return 1
    events = json.loads(events_path.read_text(encoding="utf-8"))
    if not isinstance(events, list):
        print("[ERR] events file must be a JSON list", flush=True)
        return 1
    print(f"[1/3] {len(events)} events charges depuis {events_path}", flush=True)

    rows: list[EventResult] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {
            pool.submit(
                process_event,
                ev,
                lookback_min=args.lookback_min,
                post_window_hours=args.post_window_hours,
            ): ev
            for ev in events
        }
        for fut in as_completed(futures):
            ev = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                print(f"[WARN] {ev.get('event_id')}: {exc}", flush=True)
                res = None
            if res is not None:
                rows.append(res)
                print(
                    f"      {res.event_id}: t0={res.t0_price:.3f} final={_fmt_cents(res.final_move)} "
                    f"lat={_fmt_sec(res.latency_to_first_move_sec)}",
                    flush=True,
                )

    # Conserver l'ordre du fichier d'entree pour le rapport.
    order = {ev.get("event_id", ""): i for i, ev in enumerate(events)}
    rows.sort(key=lambda r: order.get(r.event_id, 999))

    print(f"[2/3] CSV -> {args.output}", flush=True)
    write_csv(rows, Path(args.output))
    print(f"[3/3] Report -> {args.report}", flush=True)
    write_report(
        rows,
        path=Path(args.report),
        lookback_min=args.lookback_min,
        post_window_hours=args.post_window_hours,
        n_input_events=len(events),
    )

    if rows:
        median_lat = _median([r.latency_to_first_move_sec for r in rows])
        print(
            f"\n  Latence news -> 1er move >= 2c (mediane) : "
            f"{_fmt_sec(median_lat)}  (n={sum(1 for r in rows if _isfinite(r.latency_to_first_move_sec))}/{len(rows)})",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
