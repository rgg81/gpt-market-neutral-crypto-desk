from __future__ import annotations

import math

from futures_fund.costs import slippage_cost, vwap_fill

DEFAULT_K: float = 0.1   # sqrt-impact coefficient for the ADV fallback (config.yaml slippage.k)


def depth_slippage(
    levels: list[tuple[float, float]], qty: float, reference_price: float
) -> float:
    """Thin wrapper over costs.slippage_cost against an L2 depth snapshot (USDT cost).

    Direction-symmetric: `levels` are the crossing side of the book (asks to buy, bids to sell).
    """
    return slippage_cost(levels, qty, reference_price)


def fallback_slippage(
    notional: float, adv_usd: float, half_spread_bps: float, *, k: float = DEFAULT_K
) -> float:
    """half_spread + k*sqrt(notional/ADV) impact model in USDT when no depth snapshot.

    Strictly increasing in notional (a larger clip costs more bps); never returns a flat 2 bps.
    The k*sqrt term is a √-impact law, so it grows ~sqrt(notional); the per-bp cost is therefore
    monotone in size. (Spec §11's two approximate anchors are not both satisfiable by a pure
    √-law; see test_slippage.py — the $1M anchor is pinned, the $5M point is pinned for
    monotonicity, and no 'calibrated to both anchors' property is claimed here.)
    """
    notional = abs(notional)
    if adv_usd <= 0:
        impact_bps = 0.0
    else:
        impact_bps = k * math.sqrt(notional / adv_usd) * 1e4
    cost_bps = half_spread_bps + impact_bps
    return cost_bps / 1e4 * notional


def estimate_slippage(
    symbol: str, qty: float, reference_price: float, *,
    depth: list[tuple[float, float]] | None, adv_usd: float,
    half_spread_bps: float, k: float = DEFAULT_K,
) -> float:
    """Slippage cost in USDT for filling `qty` at `reference_price`. NEVER flat 2 bps.

    With a depth snapshot: charge the visible-book VWAP cost on the portion that fits
    (`depth_slippage`) PLUS the √-impact remainder on the portion of the clip that EXCEEDS visible
    depth (`fallback_slippage` on the over-depth notional). This closes the thin-name under-count —
    a clip larger than the book no longer looks artificially cheap (it used to be priced only on the
    partial fill). Strengthen-only: for a clip that fits the book the remainder is 0 (unchanged).
    No depth -> the pure ADV √-impact fallback.
    """
    if depth:
        cost = depth_slippage(depth, qty, reference_price)
        filled_qty, _ = vwap_fill(depth, abs(qty))
        over_qty = abs(qty) - filled_qty
        if over_qty > 1e-12:
            cost += fallback_slippage(over_qty * reference_price, adv_usd, half_spread_bps, k=k)
        return cost
    notional = abs(qty) * reference_price
    return fallback_slippage(notional, adv_usd, half_spread_bps, k=k)


def slippage_bps(cost_usdt: float, notional: float) -> float:
    """Convenience: cost in bps of notional (for the §11 calibration / monotonicity assertions)."""
    if notional <= 0:
        return 0.0
    return cost_usdt / notional * 1e4
