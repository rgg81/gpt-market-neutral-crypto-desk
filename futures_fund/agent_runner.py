"""Agent invocation seam for offline drivers and tests.

Production decisions are orchestrated by the subscription workflow in SKILL.md; this module
intentionally contains no raw-API LLM client.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


def parse_or_raise(schema: type[BaseModel], text: str) -> BaseModel:
    """Validate `text` (JSON) against `schema`; raise ValueError on any violation."""
    try:
        return schema.model_validate_json(text)
    except Exception as exc:  # noqa: BLE001 — normalize pydantic/json errors to ValueError
        raise ValueError(f"schema {schema.__name__} validation failed: {exc}") from exc


class AgentRunner(Protocol):
    def run(self, role: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        """Invoke `role` on `prompt`, returning a schema-valid model (or raising)."""
        ...


class StubAgentRunner:
    """Deterministic test double: returns canned model(s) keyed by role, ignoring the prompt."""

    def __init__(self, canned: dict[str, object]):
        self._canned = canned

    def run(self, role: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        if role not in self._canned:
            raise KeyError(f"StubAgentRunner has no canned output for role '{role}'")
        return self._canned[role]
