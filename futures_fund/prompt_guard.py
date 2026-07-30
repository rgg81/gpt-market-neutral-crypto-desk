"""Guard for reflector edits to agent prompts.

The Reflector may only change text INSIDE the fenced managed region; everything else (output schema,
hard rules, neutrality mandate) is out-of-region and therefore un-weakenable. The guarantee is
structural: an edit is applied by splicing the proposed region between the unchanged prefix/suffix,
and `assert_only_region_changed` re-verifies nothing outside moved."""
from __future__ import annotations

BEGIN = ("<!-- REFLECTOR:BEGIN (auto-managed calibration — evidence-backed, reversible; "
         "do not hand-edit) -->")
END = "<!-- REFLECTOR:END -->"
MAX_REGION_CHARS = 4000


class PromptGuardError(ValueError):
    """A proposed edit violated the managed-region contract."""


def split_managed(text: str) -> tuple[str, str, str]:
    """Return (prefix, region, suffix). Raise if markers are missing/duplicated/out-of-order."""
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise PromptGuardError("managed markers missing or duplicated")
    i = text.index(BEGIN)
    j = text.index(END)
    if j < i + len(BEGIN):
        raise PromptGuardError("managed markers out of order")
    return text[:i], text[i + len(BEGIN):j], text[j + len(END):]


def assert_valid_region(region: str) -> None:
    if "REFLECTOR:" in region:
        raise PromptGuardError("nested marker inside region")
    if len(region) > MAX_REGION_CHARS:
        raise PromptGuardError(f"region exceeds {MAX_REGION_CHARS} chars")


def splice_managed(text: str, new_region: str) -> str:
    """Return `text` with its managed region replaced by `new_region` (prefix/suffix untouched)."""
    assert_valid_region(new_region)
    prefix, _old, suffix = split_managed(text)
    return f"{prefix}{BEGIN}\n{new_region.strip()}\n{END}{suffix}"


def assert_only_region_changed(old_text: str, new_text: str) -> None:
    po, _ro, so = split_managed(old_text)
    pn, _rn, sn = split_managed(new_text)
    if po != pn or so != sn:
        raise PromptGuardError("edit changed text OUTSIDE the managed region")


def ensure_managed_block(text: str) -> str:
    """Idempotently append an empty managed block if the file has none."""
    if BEGIN in text and END in text:
        return text
    return f"{text.rstrip()}\n\n{BEGIN}\n{END}\n"
