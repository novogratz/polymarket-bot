"""Defensive auto-tuner driven by the trade journal.

Reads ``data/trade_journal.jsonl`` once per tick and computes a small set of
bounded parameter overrides that tighten the strategy when historical
outcomes show a clear weakness (high stop-loss share, losing 2-wallet trades,
underperforming sports, low win rate, negative average PnL). The result is
persisted to ``data/strategy_overrides.json`` and applied via
``dataclasses.replace`` on top of the env-var :class:`Settings`.

The tuner is intentionally one-directional: it only tightens after losses.
Loosening based on a small biased sample would amplify noise. It also pauses
entirely until enough closed trades have accumulated
(``smart_auto_tune_min_trades``).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings
from .models import utc_now


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def _pnl(record: dict[str, Any]) -> float:
    try:
        return float(record.get("realized_pnl") or 0)
    except (TypeError, ValueError):
        return 0.0


def compute_overrides(records: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if len(records) < settings.smart_auto_tune_min_trades:
        return overrides

    stop_loss_count = sum(1 for r in records if r.get("exit_reason") == "stop_loss")
    if stop_loss_count / len(records) > 0.40:
        new_chase = max(0.04, round(settings.smart_max_chase_premium * 0.80, 3))
        if new_chase < settings.smart_max_chase_premium:
            overrides["smart_max_chase_premium"] = new_chase
        new_relative = max(0.20, round(settings.smart_max_relative_spread * 0.85, 3))
        if new_relative < settings.smart_max_relative_spread:
            overrides["smart_max_relative_spread"] = new_relative

    consensus_two = [r for r in records if r.get("consensus") == 2]
    if len(consensus_two) >= 20:
        avg = sum(_pnl(r) for r in consensus_two) / len(consensus_two)
        if avg < -0.30:
            cur = settings.smart_min_consensus
            new = max(cur, 3)
            if new > cur:
                overrides["smart_min_consensus"] = new

    by_category: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        category = str(record.get("category") or "OTHER")
        by_category.setdefault(category, []).append(record)
    sports = by_category.get("SPORTS", [])
    if len(sports) >= 15:
        avg = sum(_pnl(r) for r in sports) / len(sports)
        if avg < -0.30:
            cur = settings.smart_sports_score_penalty
            new = min(30.0, round(cur * 1.5, 2))
            if new > cur:
                overrides["smart_sports_score_penalty"] = new

    wins = sum(1 for r in records if _pnl(r) > 0)
    win_rate = wins / len(records)
    if win_rate < 0.30:
        cur = settings.smart_min_copied_usdc
        new = round(cur * 1.5, 2)
        if new > cur:
            overrides["smart_min_copied_usdc"] = new

    avg_pnl = sum(_pnl(r) for r in records) / len(records)
    if avg_pnl < -0.20 and settings.smart_position_pct > 0:
        new_pct = max(0.04, round(settings.smart_position_pct * 0.75, 3))
        if new_pct < settings.smart_position_pct:
            overrides["smart_position_pct"] = new_pct

    return overrides


def maybe_tune(settings: Settings) -> tuple[dict[str, Any], int]:
    if not settings.smart_auto_tune_enabled:
        return {}, 0
    records = _read_journal(settings.trade_journal_path)
    overrides = compute_overrides(records, settings)
    payload = {
        "generated_at": utc_now().isoformat(),
        "records_observed": len(records),
        "min_trades_required": settings.smart_auto_tune_min_trades,
        "overrides": overrides,
    }
    path = settings.strategy_overrides_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass
    return overrides, len(records)


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> Settings:
    if not overrides:
        return settings
    safe = {k: v for k, v in overrides.items() if hasattr(settings, k)}
    if not safe:
        return settings
    return replace(settings, **safe)
