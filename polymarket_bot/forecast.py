"""v4 forecasting model + quality scoring + performance analytics.

User 2026-06-21 ("Polymarket Bot v4", "build a real forecasting model too").

The model is a **deterministic empirical-calibration forecaster**: it learns,
from the bot's own realized trades, the win probability of a favorite in each
(category, price-bucket) cell and shrinks that toward a prior (the overall
realized win rate) for thin samples. The market's implied probability for an
outcome bought at ask ``p`` is ``p`` itself, so the model edge is
``predicted_probability − p``. A trade is +EV when the calibrated win
probability exceeds the price by at least ``minimum_edge`` — exactly the
spec's rule (Market 0.90, Model 0.94 → trade).

This is NOT an LLM and NOT in conflict with the deterministic live loop: it is
pure arithmetic over the realized ledger. It degrades gracefully — with no
history every cell returns the prior, so the grinder's structural edge governs.

Also exposes the dashboard analytics (price-bucket stats, Sharpe, profit
factor, max drawdown) and the promotion gate (>= N trades AND ROI >= floor).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .categories import _record_cost, _record_pnl, classify_category

# v4 price buckets (user 2026-06-21). The primary band is 0.80–0.94; the
# experimental band 0.94–0.96 is tracked as its own bucket.
PRICE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.94),
    (0.94, 0.96),
)

# Default prior win rate used before any history exists (the grinder's
# structural edge — favorites near resolution settle at 1.0 most of the time;
# the realized post-ban win rate has run ~0.95–0.97). The live wiring passes
# the ACTUAL overall realized win rate as the prior once history exists.
DEFAULT_PRIOR_WIN_RATE = 0.95
# Pseudo-count for shrinkage: how many "prior" observations a fresh cell
# carries. Larger = trust the prior longer before the empirical rate takes over.
DEFAULT_PSEUDO_COUNT = 20.0


def price_bucket(price: float) -> str | None:
    """Return the bucket label for a price, or None if outside all buckets."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    for lo, hi in PRICE_BUCKETS:
        # Upper bound inclusive only for the top bucket so a price sits in one.
        if lo <= p < hi or (hi == PRICE_BUCKETS[-1][1] and p == hi):
            return f"{lo:.2f}-{hi:.2f}"
    return None


def _is_win(record: dict[str, Any]) -> bool:
    return _record_pnl(record) > 0


def overall_win_rate(records: Iterable[dict[str, Any]], default: float = DEFAULT_PRIOR_WIN_RATE) -> float:
    recs = list(records)
    if not recs:
        return default
    wins = sum(1 for r in recs if _is_win(r))
    return wins / len(recs)


def calibration_table(records: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Per-(category, price-bucket) realized win stats."""
    table: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        bucket = price_bucket(record.get("entry_price"))
        if bucket is None:
            continue
        cat = classify_category(str(record.get("question") or ""), str(record.get("slug") or ""))
        cell = table.setdefault((cat, bucket), {"trades": 0, "wins": 0})
        cell["trades"] += 1
        if _is_win(record):
            cell["wins"] += 1
    for cell in table.values():
        cell["win_rate"] = cell["wins"] / cell["trades"] if cell["trades"] else 0.0
    return table


def predicted_probability(
    category: str,
    ask: float,
    table: dict[tuple[str, str], dict[str, Any]] | None = None,
    *,
    prior: float = DEFAULT_PRIOR_WIN_RATE,
    pseudo_count: float = DEFAULT_PSEUDO_COUNT,
) -> float:
    """Calibrated win probability for a favorite at ``ask`` in ``category``.

    Bayesian shrinkage of the empirical cell win rate toward ``prior``:
        phat = (wins + pseudo_count * prior) / (trades + pseudo_count)
    With no history the result is ``prior`` (so the grinder still trades);
    as the cell fills, it converges to the realized win rate. Never below the
    price itself for a favorite is NOT assumed — a cell that historically
    underperforms its price yields phat < ask (negative edge → filtered)."""
    bucket = price_bucket(ask)
    cell = (table or {}).get((category, bucket)) if bucket else None
    trades = float(cell["trades"]) if cell else 0.0
    wins = float(cell["wins"]) if cell else 0.0
    phat = (wins + pseudo_count * prior) / (trades + pseudo_count)
    return max(0.0, min(1.0, phat))


def edge(
    category: str,
    ask: float,
    table: dict[tuple[str, str], dict[str, Any]] | None = None,
    *,
    prior: float = DEFAULT_PRIOR_WIN_RATE,
    pseudo_count: float = DEFAULT_PSEUDO_COUNT,
) -> float:
    """Model edge = predicted_probability − market price (the ask)."""
    return predicted_probability(category, ask, table, prior=prior, pseudo_count=pseudo_count) - float(ask)


# ── Quality score ────────────────────────────────────────────────────────
_AMBIGUOUS_MARKERS = (
    "considered", "deemed", "in the opinion", "subjective", "approximately",
    "around ", "roughly", "or similar", "etc.", "tbd",
)


def resolution_clarity(question: str) -> float:
    """0–100 heuristic — penalize vague / subjective resolution wording."""
    text = str(question or "").lower()
    if not text:
        return 50.0
    penalty = sum(20 for m in _AMBIGUOUS_MARKERS if m in text)
    return float(max(0.0, 100.0 - penalty))


def _roi_to_score(roi: float) -> float:
    """Map a category/bucket ROI to 0–100 (0 ROI → 50, ±10% → 0/100)."""
    return float(max(0.0, min(100.0, 50.0 + roi * 500.0)))


def quality_score(
    *,
    edge_value: float,
    volume_usd: float,
    category_roi: float = 0.0,
    bucket_roi: float = 0.0,
    clarity: float = 100.0,
    preferred_volume_usd: float = 5000.0,
    target_edge: float = 0.10,
) -> float:
    """Composite 0–100 quality score (user 2026-06-21).

    Factors, each normalized to 0–100 then weighted:
      - edge (35%): edge / target_edge
      - liquidity/volume (20%): volume / preferred_volume
      - resolution clarity (15%)
      - historical category ROI (15%)
      - historical price-bucket ROI (15%)
    A floor grinder favorite (positive edge, adequate volume, clear
    resolution, neutral history) lands around 70.
    """
    edge_score = max(0.0, min(100.0, (edge_value / target_edge) * 100.0)) if target_edge > 0 else 0.0
    vol_score = max(0.0, min(100.0, (volume_usd / preferred_volume_usd) * 100.0)) if preferred_volume_usd > 0 else 100.0
    cat_score = _roi_to_score(category_roi)
    bucket_score = _roi_to_score(bucket_roi)
    score = (
        0.35 * edge_score
        + 0.20 * vol_score
        + 0.15 * float(max(0.0, min(100.0, clarity)))
        + 0.15 * cat_score
        + 0.15 * bucket_score
    )
    return round(score, 1)


# ── Performance analytics (dashboard) ──────────────────────────────────────
def _returns(records: Sequence[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for r in records:
        cost = _record_cost(r)
        if cost > 0:
            out.append(_record_pnl(r) / cost)
    return out


def sharpe_ratio(records: Sequence[dict[str, Any]]) -> float:
    """Per-trade Sharpe = mean(return) / stdev(return). 0 if < 2 trades."""
    rets = _returns(records)
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    sd = var ** 0.5
    return round(mean / sd, 3) if sd > 0 else 0.0


def profit_factor(records: Sequence[dict[str, Any]]) -> float:
    """Gross profit / gross loss. inf-safe (returns gross profit when no loss)."""
    gains = sum(p for p in (_record_pnl(r) for r in records) if p > 0)
    losses = -sum(p for p in (_record_pnl(r) for r in records) if p < 0)
    if losses <= 0:
        return round(gains, 2) if gains > 0 else 0.0
    return round(gains / losses, 3)


def max_drawdown(records: Sequence[dict[str, Any]]) -> float:
    """Largest peak-to-trough drop in cumulative realized P&L (<= 0)."""
    ordered = sorted(records, key=lambda r: str(r.get("closed_at") or ""))
    running = peak = mdd = 0.0
    for r in ordered:
        running += _record_pnl(r)
        peak = max(peak, running)
        mdd = min(mdd, running - peak)
    return round(mdd, 2)


def roi(records: Sequence[dict[str, Any]]) -> float:
    total_pnl = sum(_record_pnl(r) for r in records)
    total_cost = sum(_record_cost(r) for r in records)
    return round(total_pnl / total_cost, 4) if total_cost > 0 else 0.0


def bucket_roi_table(records: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Per price-bucket ROI (= pnl / cost)."""
    agg: dict[str, list[float]] = {}
    for r in records:
        b = price_bucket(r.get("entry_price"))
        if b is None:
            continue
        cell = agg.setdefault(b, [0.0, 0.0])
        cell[0] += _record_pnl(r)
        cell[1] += _record_cost(r)
    return {b: (pnl / cost if cost > 0 else 0.0) for b, (pnl, cost) in agg.items()}


def build_context(
    records: Iterable[dict[str, Any]],
    *,
    prior_default: float = DEFAULT_PRIOR_WIN_RATE,
    pseudo_count: float = DEFAULT_PSEUDO_COUNT,
) -> dict[str, Any]:
    """Precompute everything the per-market EV/quality gate needs, once per
    tick: the calibration table, the prior (overall realized win rate), and
    the per-category / per-bucket ROI maps."""
    from .categories import category_stats

    recs = list(records)
    return {
        "table": calibration_table(recs),
        "prior": overall_win_rate(recs, prior_default),
        "pseudo_count": pseudo_count,
        "category_roi": {c: s["roi"] for c, s in category_stats(recs).items()},
        "bucket_roi": bucket_roi_table(recs),
    }


def evaluate_market(
    *,
    category: str,
    ask: float,
    volume_usd: float,
    question: str,
    ctx: dict[str, Any],
    preferred_volume_usd: float = 5000.0,
) -> dict[str, float]:
    """Edge / predicted_probability / quality_score for one market outcome."""
    table = ctx.get("table") or {}
    prior = float(ctx.get("prior", DEFAULT_PRIOR_WIN_RATE))
    pseudo = float(ctx.get("pseudo_count", DEFAULT_PSEUDO_COUNT))
    pred = predicted_probability(category, ask, table, prior=prior, pseudo_count=pseudo)
    e = pred - float(ask)
    bucket = price_bucket(ask) or ""
    q = quality_score(
        edge_value=e,
        volume_usd=volume_usd,
        category_roi=float((ctx.get("category_roi") or {}).get(category, 0.0)),
        bucket_roi=float((ctx.get("bucket_roi") or {}).get(bucket, 0.0)),
        clarity=resolution_clarity(question),
        preferred_volume_usd=preferred_volume_usd,
    )
    return {"edge": round(e, 4), "predicted": round(pred, 4), "quality": q}


def promotion_status(
    records: Sequence[dict[str, Any]],
    *,
    min_trades: int = 500,
    min_roi: float = 0.05,
) -> dict[str, Any]:
    """Promotion gate (user 2026-06-21): a strategy may only be promoted
    (scaled) after >= min_trades realized trades AND ROI >= min_roi. No
    scaling decisions before sufficient data."""
    recs = list(records)
    r = roi(recs)
    eligible = len(recs) >= min_trades and r >= min_roi
    return {
        "eligible": eligible,
        "trades": len(recs),
        "roi": r,
        "sharpe": sharpe_ratio(recs),
        "min_trades": min_trades,
        "min_roi": min_roi,
    }
