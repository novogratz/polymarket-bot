"""Reconstruit le ranking YTD des wallets Polymarket à partir de la Data API.

Pipeline :
  1. Pool de candidats = union des leaderboards (period × category) dédupés.
  2. Pour chaque wallet : pagination des trades depuis ``--since`` (BUY+SELL,
     takerOnly=true).
  3. PnL réalisé YTD = FIFO matching par ``asset`` (token_id).
  4. PnL non-réalisé = somme des ``cashPnl`` des positions courantes.
  5. CSV trié par PnL net YTD desc, top 20 affiché sur stdout.

Usage :
    uv run python scripts/wallet_history_ytd.py
    uv run python scripts/wallet_history_ytd.py \\
        --leaderboard-limit 20 --periods MONTH --categories OVERALL \\
        --output /tmp/test_ranking.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_bot.smart_money import (  # noqa: E402
    DataApiClient,
    SmartTrade,
    _float,
    market_category,
)


DEFAULT_SINCE = "2026-01-01T00:00:00Z"
DEFAULT_PERIODS = "WEEK,MONTH,ALL"
DEFAULT_CATEGORIES = "OVERALL,FINANCE,POLITICS,SPORTS,CULTURE,ECONOMICS,TECH,WEATHER"
DEFAULT_LEADERBOARD_LIMIT = 100
DEFAULT_CONCURRENCY = 24
DEFAULT_OUTPUT = "data/wallet_ytd_ranking.csv"
DEFAULT_MIN_TRADES = 5
MAX_TRADES_PER_WALLET = 5000
TRADES_PAGE_LIMIT = 500  # well below the 1000 cap observed on /trades


@dataclass(frozen=True)
class WalletRow:
    wallet: str
    username: str
    pnl_realized: float
    pnl_unrealized: float
    pnl_net_ytd: float
    volume_buy_ytd: float
    n_trades: int
    n_matched: int
    n_winning_trades: int
    win_rate: float
    hold_time_median_min: float
    top_category: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=DEFAULT_SINCE, help=f"Date début ISO (défaut {DEFAULT_SINCE})")
    parser.add_argument("--leaderboard-limit", type=int, default=DEFAULT_LEADERBOARD_LIMIT)
    parser.add_argument("--periods", default=DEFAULT_PERIODS)
    parser.add_argument("--categories", default=DEFAULT_CATEGORIES)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--data-api-url", default="https://data-api.polymarket.com")
    parser.add_argument("--max-pages", type=int, default=MAX_TRADES_PER_WALLET // TRADES_PAGE_LIMIT)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def iso_to_unix(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def build_candidate_pool(
    client: DataApiClient,
    *,
    periods: list[str],
    categories: list[str],
    leaderboard_limit: int,
    verbose: bool = False,
) -> dict[str, dict[str, Any]]:
    """Retourne {wallet_lower: {wallet, username, leaderboard_hits, categories}}."""
    pool: dict[str, dict[str, Any]] = {}
    combos = [(p, c) for p in periods for c in categories]
    for index, (period, category) in enumerate(combos, 1):
        try:
            traders = client.leaderboard(category=category, time_period=period, limit=leaderboard_limit)
        except Exception as exc:
            print(f"  ⚠️  leaderboard {period}/{category} skipped: {type(exc).__name__}: {exc}", flush=True)
            continue
        added = 0
        for t in traders:
            key = t.wallet.lower()
            entry = pool.get(key)
            if entry is None:
                entry = {
                    "wallet": t.wallet,
                    "username": t.username,
                    "hits": 0,
                    "categories": Counter(),
                }
                pool[key] = entry
                added += 1
            entry["hits"] += 1
            entry["categories"][category] += 1
        if verbose:
            print(f"  [{index}/{len(combos)}] {period}/{category}: {len(traders)} traders (+{added} new, pool={len(pool)})", flush=True)
    return pool


def _http_get_json(url: str, *, timeout: int, retries: int = 4) -> Any:
    """GET avec retry exponentiel sur 429/5xx (Cloudflare rate-limit)."""
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


def _fetch_trades_page(
    base_url: str,
    *,
    wallet: str,
    since_ts: int,
    limit: int,
    offset: int,
    timeout: int = 15,
) -> list[SmartTrade]:
    """Appel direct à ``/trades`` avec offset pagination.

    ``DataApiClient.trades`` ne supporte pas ``offset`` et force ``side=BUY``
    par défaut. Pour reconstituer l'historique YTD complet (BUY+SELL) on
    contourne via ``urllib``. ``takerOnly=true`` est conservé pour rester
    cohérent avec la stratégie smart-money (les makers gagnent par le
    spread, peu d'edge directionnel à apprendre).
    """
    # NOTE: le param ``start`` est accepté mais **ne filtre pas** côté serveur
    # (vérifié empiriquement : walletmobile renvoyait des trades de nov 2024
    # avec start=2026-01-01). On le retire et on filtre côté client dans
    # ``fetch_all_trades``.
    params = {
        "user": wallet,
        "limit": str(limit),
        "offset": str(offset),
        "takerOnly": "true",
    }
    url = f"{base_url.rstrip('/')}/trades?{urllib.parse.urlencode(params)}"
    payload = _http_get_json(url, timeout=timeout)
    if not isinstance(payload, list):
        return []
    trades: list[SmartTrade] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset") or "")
        if not asset:
            continue
        trades.append(
            SmartTrade(
                wallet=str(item.get("proxyWallet") or wallet),
                asset=asset,
                side=str(item.get("side") or ""),
                price=_float(item.get("price")),
                size=_float(item.get("size")),
                usdc_size=_float(item.get("usdcSize"), _float(item.get("size")) * _float(item.get("price"))),
                timestamp=int(_float(item.get("timestamp"))),
                title=str(item.get("title") or ""),
                outcome=str(item.get("outcome") or ""),
                slug=str(item.get("slug") or item.get("eventSlug") or ""),
            )
        )
    return trades


def _fetch_positions(base_url: str, *, wallet: str, timeout: int = 15) -> list[dict[str, Any]]:
    params = {"user": wallet, "limit": "500", "sizeThreshold": "0"}
    url = f"{base_url.rstrip('/')}/positions?{urllib.parse.urlencode(params)}"
    payload = _http_get_json(url, timeout=timeout)
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def fetch_all_trades(
    client: DataApiClient,
    *,
    wallet: str,
    since_ts: int,
    max_pages: int = MAX_TRADES_PER_WALLET // TRADES_PAGE_LIMIT,
) -> list[SmartTrade]:
    """Pagination offset des trades BUY+SELL, filtrés à ``ts >= since_ts``.

    L'API ``/trades`` renvoie les trades en ordre décroissant de timestamp
    et accepte ``offset`` pour la pagination. Le param ``start`` ne filtre
    pas côté serveur — on filtre côté client ici. Comme l'ordre est DESC,
    on stoppe la pagination dès qu'on rencontre un trade < ``since_ts``
    (tous les suivants seront encore plus anciens).
    """
    out: list[SmartTrade] = []
    seen: set[tuple[str, int, str, float, float]] = set()
    for page in range(max_pages):
        try:
            batch = _fetch_trades_page(
                client.base_url,
                wallet=wallet,
                since_ts=since_ts,
                limit=TRADES_PAGE_LIMIT,
                offset=page * TRADES_PAGE_LIMIT,
                timeout=client.timeout,
            )
        except urllib.error.HTTPError as exc:
            # 400 = offset hors plage côté API → fin de pagination.
            if exc.code == 400 and page > 0:
                break
            raise
        if not batch:
            break
        new_added = 0
        hit_pre_window = False
        for tr in batch:
            if tr.timestamp < since_ts:
                hit_pre_window = True
                continue
            key = (tr.asset, tr.timestamp, tr.side, round(tr.price, 6), round(tr.size, 6))
            if key in seen:
                continue
            seen.add(key)
            out.append(tr)
            new_added += 1
        if hit_pre_window:
            # Ordre DESC → tout ce qui suit sera < since_ts.
            break
        if new_added == 0:
            break
        if len(batch) < TRADES_PAGE_LIMIT:
            break
    return out


def compute_realized_pnl_fifo(
    trades: Iterable[SmartTrade],
) -> tuple[float, float, int, int, list[float], dict[str, deque[tuple[float, float, int]]]]:
    """FIFO matching par ``asset``.

    Renvoie ``(realized_pnl, buy_volume, n_matched_sells, n_winning_sells,
    hold_times_minutes, residual_buys)``.

    Un SELL est compté ``winning`` si le PnL réalisé sur ce SELL est > 0.
    ``hold_times_minutes`` contient la durée (en minutes) entre chaque BUY
    et le SELL qui l'a consommé (un SELL peut générer plusieurs hold-times).
    """
    sorted_trades = sorted(trades, key=lambda t: t.timestamp)
    queues: dict[str, deque[tuple[float, float, int]]] = {}
    realized = 0.0
    buy_volume = 0.0
    n_matched = 0
    n_winning = 0
    hold_times: list[float] = []
    for tr in sorted_trades:
        side = tr.side.upper()
        if side == "BUY":
            buy_volume += tr.usdc_size
            queues.setdefault(tr.asset, deque()).append((tr.price, tr.size, tr.timestamp))
        elif side == "SELL":
            queue = queues.get(tr.asset)
            if not queue:
                continue
            remaining = tr.size
            sell_pnl = 0.0
            matched_any = False
            while remaining > 1e-9 and queue:
                buy_price, buy_size, buy_ts = queue[0]
                matched = min(remaining, buy_size)
                sell_pnl += matched * (tr.price - buy_price)
                hold_times.append((tr.timestamp - buy_ts) / 60.0)
                matched_any = True
                remaining -= matched
                if matched + 1e-9 < buy_size:
                    queue[0] = (buy_price, buy_size - matched, buy_ts)
                else:
                    queue.popleft()
            if matched_any:
                n_matched += 1
                if sell_pnl > 0:
                    n_winning += 1
                realized += sell_pnl
    return realized, buy_volume, n_matched, n_winning, hold_times, queues


def compute_unrealized_pnl(positions: list[dict[str, Any]]) -> float:
    total = 0.0
    for pos in positions:
        pnl = pos.get("cashPnl")
        if pnl is None:
            size = float(pos.get("size") or 0.0)
            avg = float(pos.get("avgPrice") or 0.0)
            cur = float(pos.get("curPrice") or 0.0)
            pnl = size * (cur - avg)
        try:
            total += float(pnl)
        except (TypeError, ValueError):
            continue
    return total


def top_category_for(trades: list[SmartTrade]) -> str:
    counts: Counter[str] = Counter()
    for tr in trades:
        cat = market_category(tr.title or "", tr.slug or "")
        counts[cat] += 1
    if not counts:
        return "OTHER"
    return counts.most_common(1)[0][0]


def aggregate_wallet_stats(
    *,
    wallet: str,
    username: str,
    trades: list[SmartTrade],
    positions: list[dict[str, Any]],
) -> WalletRow:
    realized, buy_volume, n_matched, n_winning, hold_times, _residual = compute_realized_pnl_fifo(trades)
    unrealized = compute_unrealized_pnl(positions)
    win_rate = (n_winning / n_matched) if n_matched > 0 else 0.0
    hold_med = statistics.median(hold_times) if hold_times else 0.0
    return WalletRow(
        wallet=wallet,
        username=username,
        pnl_realized=realized,
        pnl_unrealized=unrealized,
        pnl_net_ytd=realized + unrealized,
        volume_buy_ytd=buy_volume,
        n_trades=len(trades),
        n_matched=n_matched,
        n_winning_trades=n_winning,
        win_rate=win_rate,
        hold_time_median_min=hold_med,
        top_category=top_category_for(trades),
    )


def process_wallet(
    client: DataApiClient,
    *,
    wallet: str,
    username: str,
    since_ts: int,
    max_pages: int,
) -> WalletRow | None:
    try:
        trades = fetch_all_trades(client, wallet=wallet, since_ts=since_ts, max_pages=max_pages)
    except Exception as exc:
        print(f"  ⚠️  trades fetch failed for {wallet[:10]}…: {type(exc).__name__}: {exc}", flush=True)
        return None
    try:
        positions = _fetch_positions(client.base_url, wallet=wallet, timeout=client.timeout)
    except Exception as exc:
        print(f"  ⚠️  positions fetch failed for {wallet[:10]}…: {type(exc).__name__}: {exc}", flush=True)
        positions = []
    return aggregate_wallet_stats(wallet=wallet, username=username, trades=trades, positions=positions)


def write_csv(rows: list[WalletRow], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "rank",
                "wallet",
                "username",
                "pnl_net_ytd",
                "pnl_realized",
                "pnl_unrealized",
                "volume_buy_ytd",
                "n_trades",
                "n_matched_sells",
                "win_rate",
                "hold_time_median_min",
                "top_category",
            ]
        )
        for rank, row in enumerate(rows, 1):
            writer.writerow(
                [
                    rank,
                    row.wallet,
                    row.username,
                    f"{row.pnl_net_ytd:.2f}",
                    f"{row.pnl_realized:.2f}",
                    f"{row.pnl_unrealized:.2f}",
                    f"{row.volume_buy_ytd:.2f}",
                    row.n_trades,
                    row.n_matched,
                    f"{row.win_rate:.4f}",
                    f"{row.hold_time_median_min:.1f}",
                    row.top_category,
                ]
            )


def print_top(rows: list[WalletRow], n: int = 20) -> None:
    if not rows:
        print("\nAucun wallet retenu après filtrage.")
        return
    header = f"{'#':>3}  {'wallet':<14}  {'username':<22}  {'pnl_net':>10}  {'real':>10}  {'unreal':>9}  {'vol_buy':>10}  {'n':>4}  {'win%':>5}  {'hold_med':>8}  {'cat':<10}"
    print("\n" + header)
    print("-" * len(header))
    for rank, row in enumerate(rows[:n], 1):
        short_wallet = (row.wallet[:6] + "…" + row.wallet[-4:]) if len(row.wallet) > 12 else row.wallet
        short_user = (row.username[:20] + "…") if len(row.username) > 21 else row.username
        print(
            f"{rank:>3}  {short_wallet:<14}  {short_user:<22}  "
            f"{row.pnl_net_ytd:>10.2f}  {row.pnl_realized:>10.2f}  {row.pnl_unrealized:>9.2f}  "
            f"{row.volume_buy_ytd:>10.2f}  {row.n_trades:>4d}  {row.win_rate * 100:>5.1f}  "
            f"{row.hold_time_median_min:>8.1f}  {row.top_category:<10}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    since_ts = iso_to_unix(args.since)
    periods = [p.strip().upper() for p in args.periods.split(",") if p.strip()]
    categories = [c.strip().upper() for c in args.categories.split(",") if c.strip()]
    output = Path(args.output)
    client = DataApiClient(args.data_api_url)

    print(
        f"== wallet_history_ytd ==\n"
        f"  since      : {args.since}  (unix={since_ts})\n"
        f"  periods    : {periods}\n"
        f"  categories : {categories}\n"
        f"  top_n      : {args.leaderboard_limit}\n"
        f"  concurrency: {args.concurrency}\n"
        f"  output     : {output}\n"
        f"  min_trades : {args.min_trades}\n",
        flush=True,
    )

    t0 = time.time()
    print("Step 1/3 — build candidate pool (leaderboards)…", flush=True)
    pool = build_candidate_pool(
        client,
        periods=periods,
        categories=categories,
        leaderboard_limit=args.leaderboard_limit,
        verbose=args.verbose,
    )
    print(f"  → pool: {len(pool)} wallets uniques ({time.time() - t0:.1f}s)", flush=True)

    print(f"\nStep 2/3 — fetch trades+positions YTD (concurrency={args.concurrency})…", flush=True)
    t1 = time.time()
    rows: list[WalletRow] = []
    done = 0
    total = len(pool)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool_ex:
        futures = {
            pool_ex.submit(
                process_wallet,
                client,
                wallet=entry["wallet"],
                username=entry["username"],
                since_ts=since_ts,
                max_pages=args.max_pages,
            ): key
            for key, entry in pool.items()
        }
        for fut in as_completed(futures):
            done += 1
            row = fut.result()
            if row is not None:
                rows.append(row)
            if done % 25 == 0 or done == total:
                print(f"  [{done}/{total}] processed ({time.time() - t1:.1f}s)", flush=True)
    print(f"  → {len(rows)} wallets traités ({time.time() - t1:.1f}s)", flush=True)

    print(f"\nStep 3/3 — filter (n_trades ≥ {args.min_trades}), sort, write CSV…", flush=True)
    filtered = [r for r in rows if r.n_trades >= args.min_trades]
    filtered.sort(key=lambda r: r.pnl_net_ytd, reverse=True)
    write_csv(filtered, output)
    print(f"  → {len(filtered)} wallets retenus → {output} ({time.time() - t0:.1f}s total)")

    print_top(filtered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
