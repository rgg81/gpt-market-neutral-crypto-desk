"""Per-cycle artifact persistence (lifted from the weekly desk, extended for cadence segmentation).

CADENCE-ROOT INVARIANT (binding, §14 + canonical contract): when `cadence` is set, artifacts live
under `state/<cadence>/cycle/<N>/` — the SAME root `scheduling.cycle_due(loop=cadence)` scans (it
builds `state/<loop>/cycle/*`). So `cycle_dir(cadence=...)` MUST resolve to
`state/<cadence>/cycle/<n>` (NOT `state/cycle/<cadence>/n`): the due-gate reader and the writer
agree on one root and can never diverge. With no `cadence`, the legacy `state/cycle/<n>` is kept.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from futures_fund.models import Cadence

M = TypeVar("M", bound=BaseModel)


def cycle_dir(state_dir, cycle_no: int, *, cadence: Cadence | None = None) -> Path:
    """Resolve the cycle-<n> artifact directory.

    `cadence` set  -> `state/<cadence>/cycle/<n>` (matches `scheduling.cycle_due(loop=cadence)`).
    `cadence` None -> `state/cycle/<n>` (legacy single-loop back-compat).
    """
    base = Path(state_dir)
    root = (base / cadence / "cycle") if cadence else (base / "cycle")
    return root / str(cycle_no)


def save_output(
    state_dir,
    cycle_no: int,
    name: str,
    data: dict | BaseModel,
    *,
    cadence: Cadence | None = None,
) -> Path:
    """Persist an agent's output JSON under `<cycle_dir>/<name>.json`.

    The write is ATOMIC (temp file in the same dir + os.replace) so a concurrent reader — notably
    the due-gate scanning report.json — never sees a half-written file: it finds either the prior
    contents or the complete new contents, never a truncated middle.
    """
    d = cycle_dir(state_dir, cycle_no, cadence=cadence)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    text = data.model_dump_json(indent=2) if isinstance(data, BaseModel) \
        else json.dumps(data, indent=2, default=str)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)
    return p


def load_output(
    state_dir,
    cycle_no: int,
    name: str,
    *,
    cadence: Cadence | None = None,
) -> dict:
    p = cycle_dir(state_dir, cycle_no, cadence=cadence) / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"no cycle output: {p}")
    return json.loads(p.read_text())


def validate_output(data: dict, model: type[M]) -> M:
    """Validate a raw agent output dict against its contract.

    Raises ValidationError if malformed.
    """
    return model.model_validate(data)
