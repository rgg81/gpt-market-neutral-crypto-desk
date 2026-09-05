"""Shared descriptive correlation-cluster arithmetic for alpha portfolios.

The evidence risk model intentionally reports both large positive and large negative residual
correlations.  Portfolio concentration depends on the *position PnL* correlation, not the
absolute asset-return correlation: same-side positive and opposite-side negative pairs amplify
one another, while same-side negative and opposite-side positive pairs diversify.
"""

from __future__ import annotations

import math

CORRELATION_CONCENTRATION_THRESHOLD = 0.60


def _clusters(
    adjacency: dict[str, set[str]],
    *,
    side_by_symbol: dict[str, str],
    standalone_share: dict[str, float],
    variance_contribution: dict[str, float],
) -> list[dict]:
    clusters: list[dict] = []
    visited: set[str] = set()
    for seed in sorted(adjacency):
        if seed in visited or not adjacency[seed]:
            continue
        stack = [seed]
        component: set[str] = set()
        while stack:
            symbol = stack.pop()
            if symbol in component:
                continue
            component.add(symbol)
            stack.extend(adjacency[symbol] - component)
        visited.update(component)
        members = sorted(component)
        clusters.append(
            {
                "side": (
                    side_by_symbol[seed]
                    if len({side_by_symbol[symbol] for symbol in members}) == 1
                    else "mixed"
                ),
                "symbols": members,
                "standalone_risk_share": sum(standalone_share[symbol] for symbol in members),
                "variance_contribution_frac": sum(
                    variance_contribution[symbol] for symbol in members
                ),
            }
        )
    return clusters


def position_correlation_context(
    high_correlation_pairs: list[dict],
    *,
    side_by_symbol: dict[str, str],
    standalone_risk: dict[str, float],
    variance_contribution_frac: dict[str, float],
) -> dict:
    """Annotate held pairs and build economically correct concentration components.

    ``same_side_high_correlation_clusters`` is deliberately narrow and contains only same-side,
    positively correlated seats. ``position_co_risk_clusters`` is the complete signed-position
    view and also catches opposite-side seats whose residual returns are strongly negatively
    correlated.  Both are evidence for GPT judgment; neither is a deterministic trade veto.
    """
    selected = set(side_by_symbol)
    standalone_total = sum(max(float(value), 0.0) for value in standalone_risk.values())
    standalone_share = {
        symbol: (
            max(float(standalone_risk.get(symbol, 0.0)), 0.0) / standalone_total
            if standalone_total > 0.0
            else 0.0
        )
        for symbol in selected
    }
    same_side_adjacency = {symbol: set() for symbol in selected}
    co_risk_adjacency = {symbol: set() for symbol in selected}
    held_pairs: list[dict] = []
    for raw_pair in high_correlation_pairs:
        left = str(raw_pair.get("left") or "")
        right = str(raw_pair.get("right") or "")
        if left not in selected or right not in selected or left == right:
            continue
        try:
            correlation = float(raw_pair["correlation"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(correlation):
            continue
        same_side = side_by_symbol[left] == side_by_symbol[right]
        position_pnl_correlation = correlation if same_side else -correlation
        pair = dict(raw_pair)
        pair.update(
            {
                "left_side": side_by_symbol[left],
                "right_side": side_by_symbol[right],
                "same_side": same_side,
                "position_pnl_correlation": position_pnl_correlation,
                "co_risk": position_pnl_correlation >= CORRELATION_CONCENTRATION_THRESHOLD,
                "combined_standalone_risk_share": (
                    standalone_share[left] + standalone_share[right]
                ),
            }
        )
        held_pairs.append(pair)
        if same_side and correlation >= CORRELATION_CONCENTRATION_THRESHOLD:
            same_side_adjacency[left].add(right)
            same_side_adjacency[right].add(left)
        if position_pnl_correlation >= CORRELATION_CONCENTRATION_THRESHOLD:
            co_risk_adjacency[left].add(right)
            co_risk_adjacency[right].add(left)

    cluster_kwargs = {
        "side_by_symbol": side_by_symbol,
        "standalone_share": standalone_share,
        "variance_contribution": {
            symbol: float(variance_contribution_frac.get(symbol, 0.0)) for symbol in selected
        },
    }
    same_side_clusters = _clusters(same_side_adjacency, **cluster_kwargs)
    co_risk_clusters = _clusters(co_risk_adjacency, **cluster_kwargs)
    return {
        "held_high_correlation_pairs": held_pairs,
        "same_side_high_correlation_clusters": same_side_clusters,
        "max_same_side_high_correlation_cluster_risk_share": max(
            (float(row["standalone_risk_share"]) for row in same_side_clusters),
            default=0.0,
        ),
        "position_co_risk_clusters": co_risk_clusters,
        "max_position_co_risk_cluster_risk_share": max(
            (float(row["standalone_risk_share"]) for row in co_risk_clusters),
            default=0.0,
        ),
        "correlation_concentration_threshold": CORRELATION_CONCENTRATION_THRESHOLD,
    }
