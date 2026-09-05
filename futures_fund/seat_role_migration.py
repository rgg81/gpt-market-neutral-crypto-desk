"""One-time, provenance-bound migration for legacy PAPER position seat roles."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from futures_fund.account import PaperAccount, save_account
from futures_fund.cycle_io import cycle_dir
from futures_fund.reconcile_commit import (
    completed_artifact_is_bound,
    completed_cycle_numbers,
)


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str) + "\n")
    os.replace(tmp, path)


def migrate_legacy_position_seat_roles(state_dir, *, cadence: str = "rebal") -> dict:
    """Recover missing current-position roles from the latest bound completed Book.

    Quantity, direction, entry price, PnL, funding, and frictions are never changed. A durable
    intent makes a crash between the metadata write and audit marker recoverable.
    """
    state = Path(state_dir)
    account_path = state / "account.json"
    if not account_path.exists():
        return {"migrated": [], "reason": "no account"}
    raw = json.loads(account_path.read_text())
    positions = raw.get("positions")
    if not isinstance(positions, dict):
        raise ValueError("legacy account positions are not an object")
    missing = sorted(
        symbol
        for symbol, position in positions.items()
        if isinstance(position, dict) and "seat_role" not in position
    )
    marker_path = state / "seat-role-migration.json"
    current_sha256 = _canonical_sha256(raw)
    if marker_path.exists():
        marker = json.loads(marker_path.read_text())
        if current_sha256 == marker.get("after_account_sha256"):
            if marker.get("status") != "complete":
                marker["status"] = "complete"
                marker["completed_at"] = datetime.now(UTC).isoformat()
                _atomic_json(marker_path, marker)
            return {
                "migrated": list(marker.get("migrated_symbols", [])),
                "source_cycle": marker.get("source_cycle"),
                "already_complete": True,
            }
        if current_sha256 != marker.get("before_account_sha256"):
            raise ValueError("seat-role migration intent conflicts with current account")
    if not missing:
        return {"migrated": [], "reason": "all positions already carry seat_role"}

    source_cycle = None
    source_book = None
    for cycle in reversed(completed_cycle_numbers(state, cadence=cadence)):
        if not completed_artifact_is_bound(state, cycle, "book", cadence=cadence):
            continue
        book = json.loads((cycle_dir(state, cycle, cadence=cadence) / "book.json").read_text())
        by_symbol: dict[str, list[dict]] = {}
        for leg in book.get("legs", []):
            if isinstance(leg, dict) and leg.get("symbol"):
                by_symbol.setdefault(str(leg["symbol"]), []).append(leg)
        if all(len(by_symbol.get(symbol, [])) == 1 for symbol in missing):
            source_cycle = cycle
            source_book = book
            break
    if source_cycle is None or source_book is None:
        raise ValueError("no bound completed Book uniquely covers every legacy held symbol")

    by_symbol = {str(leg["symbol"]): leg for leg in source_book["legs"]}
    migrated: dict[str, str] = {}
    for symbol in missing:
        leg = by_symbol[symbol]
        position = positions[symbol]
        if leg.get("side") != position.get("direction"):
            raise ValueError(f"source Book side conflicts with held position {symbol}")
        role = str(leg.get("seat_role") or "")
        if role not in {"alpha", "hedge"}:
            raise ValueError(f"source Book lacks an explicit seat role for {symbol}")
        position["seat_role"] = role
        migrated[symbol] = role

    account = PaperAccount.from_dict(raw)
    after = account.to_dict()
    marker = {
        "version": 1,
        "paper_only": True,
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
        "source_cycle": source_cycle,
        "source_book_sha256": _canonical_sha256(source_book),
        "before_account_sha256": current_sha256,
        "after_account_sha256": _canonical_sha256(after),
        "migrated_symbols": migrated,
        "note": "metadata-only recovery; quantity, direction, prices, PnL and cash unchanged",
    }
    _atomic_json(marker_path, marker)
    save_account(state, account)
    marker["status"] = "complete"
    marker["completed_at"] = datetime.now(UTC).isoformat()
    _atomic_json(marker_path, marker)
    return {
        "migrated": sorted(migrated),
        "roles": migrated,
        "source_cycle": source_cycle,
        "already_complete": False,
    }
