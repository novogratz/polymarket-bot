"""Pricing-refresh helper.

Shared utility that augments a scan-derived candidate list with the
markets of every currently open position so that ``mark_to_market`` and
exit detection cover positions whose token has drifted out of the scan.

Without this step, an open position whose token leaves the Gamma scan
(volume dropped, horizon glided past, no longer in the top of the
sorted batch) sees its ``unrealized_pnl`` freeze, and the sell-strategy
exit checks — which look candidates up by token — silently skip it.
"""

from __future__ import annotations

from .config import Settings
from .gamma import GammaClient
from .portfolio import Portfolio
from .strategy import build_pricing_candidates


def ensure_open_positions_in_pool(
    settings: Settings,
    portfolio: Portfolio,
    candidates: list,
) -> list:
    """Return a superset of ``candidates`` covering every open position.

    The extra candidates are built with score 0.0 and no horizon /
    liquidity filter — they exist purely to keep prices fresh, never to
    seed new entries. Callers must still filter their entry pool from
    the original ``candidates`` list, not from this superset.
    """
    open_tokens = {
        str(position["token_id"])
        for position in portfolio.positions
        if position.get("status") == "open" and position.get("token_id")
    }
    if not open_tokens:
        return list(candidates)
    scan_tokens = {c.token_id for c in candidates if c.token_id}
    missing = sorted(open_tokens - scan_tokens)
    if not missing:
        return list(candidates)
    if not settings.quiet:
        print(
            f"   pricing-refresh: {len(missing)} held position(s) missing from scan",
            flush=True,
        )
    try:
        extra_markets = GammaClient(settings.gamma_base_url).get_markets_by_clob_token_ids(missing)
    except Exception as exc:
        print(f"   pricing-refresh failed: {type(exc).__name__}: {exc}")
        return list(candidates)
    return list(candidates) + build_pricing_candidates(extra_markets)
