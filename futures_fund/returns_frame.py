"""Per-symbol return frame for the optimizer's covariance inputs (HRP shaping + cluster cap).

`optimize_book` builds its Ledoit-Wolf covariance — and the cross-correlation snapshot the cluster
cap binds on — ONLY when handed a non-empty ``returns`` DataFrame (``neutrality.optimize_book`` step
3). The live control loop was calling it with ``returns=None``, so HRP fell back to the merged split
and the cluster cap could never bind (empty corr map). This module turns the per-symbol close series
cycle-prep already reads (``cycle_prep._marks_frame``) into that DataFrame — columns = symbols, rows
= per-period returns — and json helpers persist it as a cycle artifact the control loop reloads.

Alignment is on the MOST-RECENT common window: each symbol's returns are trimmed to the shortest
symbol's length so the covariance is computed over an overlapping recent period (not a positional
mix of different histories). A symbol with fewer than ``min_obs`` returns is dropped (too short to
contribute a stable covariance row) rather than poisoning the matrix with NaNs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def build_returns_frame(
    marks_by_symbol: dict[str, pd.Series], *, min_obs: int = 20
) -> pd.DataFrame:
    """Build a (rows=returns, columns=symbols) DataFrame from per-symbol close series.

    Each series -> simple returns (``pct_change``); symbols with < ``min_obs`` returns are dropped;
    the survivors are aligned on the most-recent common length. Returns an EMPTY DataFrame when no
    symbol qualifies (the optimizer then degrades to the merged split, exactly as before)."""
    rets_by_sym: dict[str, np.ndarray] = {}
    for sym, series in marks_by_symbol.items():
        s = pd.Series(series, dtype=float).reset_index(drop=True)
        r = s.pct_change().dropna().to_numpy()
        if len(r) >= min_obs:
            rets_by_sym[sym] = r
    if not rets_by_sym:
        return pd.DataFrame()
    common = min(len(r) for r in rets_by_sym.values())
    # align on the most-recent `common` observations (tail), preserving column order.
    return pd.DataFrame({sym: r[-common:] for sym, r in rets_by_sym.items()})


def frame_to_json(df: pd.DataFrame) -> dict:
    """Serialize a returns frame to a JSON-safe dict (``{columns, data}``)."""
    return {"columns": list(df.columns), "data": df.to_numpy().tolist()}


def frame_from_json(payload: dict) -> pd.DataFrame:
    """Reconstruct a returns frame from ``frame_to_json`` output (empty/None -> empty frame)."""
    if not payload or not payload.get("columns"):
        return pd.DataFrame()
    return pd.DataFrame(payload.get("data") or [], columns=payload["columns"])
