# tests/test_prompt_guard.py
import pytest

from futures_fund.prompt_guard import (
    BEGIN,
    END,
    MAX_REGION_CHARS,
    PromptGuardError,
    assert_only_region_changed,
    assert_valid_region,
    ensure_managed_block,
    splice_managed,
    split_managed,
)

DOC = f"# Role\n\n## Hard rules\n- never invent\n\n{BEGIN}\nold note\n{END}\n\n## Output\nJSON\n"


def test_split_roundtrip():
    prefix, region, suffix = split_managed(DOC)
    assert "Hard rules" in prefix and "Output" in suffix
    assert region.strip() == "old note"


def test_split_missing_markers_raises():
    with pytest.raises(PromptGuardError):
        split_managed("no markers here")


def test_splice_replaces_only_region():
    new = splice_managed(DOC, "fresh note")
    assert "fresh note" in new and "old note" not in new
    assert_only_region_changed(DOC, new)  # must not raise


def test_assert_only_region_changed_detects_outside_edit():
    tampered = DOC.replace("never invent", "sometimes invent")
    tampered = splice_managed(tampered, "x")
    with pytest.raises(PromptGuardError):
        assert_only_region_changed(DOC, tampered)


def test_valid_region_rejects_nested_marker_and_oversize():
    with pytest.raises(PromptGuardError):
        assert_valid_region(f"contains {END} marker")
    with pytest.raises(PromptGuardError):
        assert_valid_region("x" * (MAX_REGION_CHARS + 1))


def test_ensure_managed_block_idempotent():
    seeded = ensure_managed_block("# Role\n\n## Output\nJSON\n")
    assert BEGIN in seeded and END in seeded
    assert ensure_managed_block(seeded) == seeded  # already present -> unchanged
