"""Tests for app/mrdoc/frontmatter.py — the artifact vocabulary.

Pins the round-trip contract: whatever render produces, parse must
recover exactly, and anything outside the subset must fail loudly
(an unparseable artifact aborts the pipeline, never degrades it).
"""

from __future__ import annotations

import pytest

from app.mrdoc.frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)


def test_frontmatter_roundtrip_scalars_and_flow() -> None:
    data = {
        "mr_iid": 42,
        "project_id": "grp/proj",
        "skipped": False,
        "diff_refs": {"base_sha": "b" * 8, "head_sha": "h" * 8},
        "paths": ["docs/a.md", "docs/b.md"],
    }
    assert parse_frontmatter(render_frontmatter(data)) == data


def test_frontmatter_quotes_values_with_specials() -> None:
    data = {"note": "a, b: c \"quoted\" \\ path"}
    assert parse_frontmatter(render_frontmatter(data)) == data


def test_frontmatter_rejects_missing_fence() -> None:
    with pytest.raises(ValueError):
        parse_frontmatter("no fence here")


def test_section_roundtrip_and_multi_block_parse() -> None:
    first = render_section(
        "UNIT", "u-abc", {"section_id": "s-x", "removed": ["30초"], "changed": []}
    )
    second = render_section("UNIT", "u-def", {"section_id": "s-y"})
    text = first + "\n\n" + second
    parsed = parse_sections(text)
    assert set(parsed) == {"u-abc", "u-def"}
    assert parsed["u-abc"]["section_id"] == "s-x"
    assert parsed["u-abc"]["removed"] == ["30초"]
