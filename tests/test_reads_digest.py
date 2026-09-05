from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from futures_fund.adversary_binding import specialist_reads_sha256
from futures_fund.desk_contracts import Book, SpecialistRead
from scripts.desk_precheck import _verify_specialist_digest
from scripts.desk_reads_digest import main

SYMBOLS = ("A/USDT:USDT", "B/USDT:USDT")


def _pending(tmp_path):
    memory = tmp_path / "memory"
    pending = memory / "pending" / "7"
    pending.mkdir(parents=True)
    now = datetime.now(UTC)
    (memory / "pending" / "current.json").write_text(json.dumps({
        "cycle": 7,
        "dir": str(pending),
        "created": now.isoformat(),
    }))
    (pending / "meta.json").write_text(json.dumps({
        "cycle": 7,
        "now": now.isoformat(),
        "cash": 20_000.0,
        "symbols": list(SYMBOLS),
    }))
    (pending / "evidence.json").write_text(json.dumps([
        {"symbol": symbol, "mark": 1.0} for symbol in SYMBOLS
    ]))
    return memory, pending


def _reads(role: str) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "lean": "long" if role == "technical" and index == 0 else "flat",
            "conviction": 0.6 if role == "technical" and index == 0 else 0.0,
            "rationale": f"{role} rationale for {symbol}",
            "evidence": [f"{role} evidence {index}"],
        }
        for index, symbol in enumerate(SYMBOLS)
    ]


def test_digest_normalizes_failed_role_and_is_exclusive_and_read_only(tmp_path):
    memory, pending = _pending(tmp_path)
    (pending / "sentiment_reads.json").write_text(json.dumps(_reads("sentiment")))
    # Partial coverage is a documented fail-soft role failure. Its prose must not survive beside
    # a semantic [] digest that later roles never actually saw.
    (pending / "technical_reads.json").write_text(json.dumps(_reads("technical")[:1]))
    (pending / "futures_reads.json").write_text(json.dumps(_reads("futures")))

    assert main(["--memory-dir", str(memory)]) == 0

    assert json.loads((pending / "technical_reads.json").read_text()) == []
    normalized = {
        role: [
            SpecialistRead.model_validate(row)
            for row in json.loads((pending / f"{role}_reads.json").read_text())
        ]
        for role in ("sentiment", "technical", "futures")
    }
    sidecar = pending / "specialist_reads.sha256"
    assert sidecar.read_text().strip() == specialist_reads_sha256(normalized)
    assert sidecar.stat().st_mode & 0o777 == 0o400

    with pytest.raises(ValueError, match="already exists"):
        main(["--memory-dir", str(memory)])


def test_post_digest_mutation_cannot_be_redigested_and_no_longer_matches(tmp_path):
    memory, pending = _pending(tmp_path)
    for role in ("sentiment", "technical", "futures"):
        (pending / f"{role}_reads.json").write_text(json.dumps(_reads(role)))
    assert main(["--memory-dir", str(memory)]) == 0
    original_digest = (pending / "specialist_reads.sha256").read_text().strip()
    evidence = json.loads((pending / "evidence.json").read_text())
    bound_book = Book(specialist_reads_sha256=original_digest)
    _verify_specialist_digest(pending, evidence, bound_book)

    mutated = _reads("technical")
    mutated[0]["rationale"] = "post-digest rationale mutation"
    (pending / "technical_reads.json").write_text(json.dumps(mutated))
    with pytest.raises(ValueError, match="already exists"):
        main(["--memory-dir", str(memory)])
    with pytest.raises(ValueError, match="changed after"):
        _verify_specialist_digest(pending, evidence, bound_book)

    current = {
        role: [
            SpecialistRead.model_validate(row)
            for row in json.loads((pending / f"{role}_reads.json").read_text())
        ]
        for role in ("sentiment", "technical", "futures")
    }
    assert specialist_reads_sha256(current) != original_digest
    assert (pending / "specialist_reads.sha256").read_text().strip() == original_digest


def test_all_three_failed_roles_halt_without_digest(tmp_path):
    memory, pending = _pending(tmp_path)
    for role in ("sentiment", "technical", "futures"):
        (pending / f"{role}_reads.json").write_text("not-json")

    with pytest.raises(ValueError, match="all three"):
        main(["--memory-dir", str(memory)])
    assert not (pending / "specialist_reads.sha256").exists()
