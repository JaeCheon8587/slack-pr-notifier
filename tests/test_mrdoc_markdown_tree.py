"""Tests for app/mrdoc/markdown_tree.py — the section coordinate system.

The design marks this parsing as the real workload: ATX and setext both
count, '#' inside fences must not, and '---' is three things at once.
"""

from __future__ import annotations

from app.mrdoc.markdown_tree import parse_sections, section_text

_T = chr(96)


def test_atx_headings_with_levels_and_paths() -> None:
    sections = parse_sections("# A\nb\n\n## B > C\nc\n")
    assert [(s.heading_path, s.level) for s in sections] == [("A", 1), ("A > B > C", 2)]


def test_setext_headings_count() -> None:
    sections = parse_sections("Title\n=====\nbody\nSub\n---\nmore\n")
    assert [s.level for s in sections] == [1, 2]


def test_hash_inside_code_fence_is_not_a_heading() -> None:
    text = "# Real\n\n" + _T * 3 + "python\n# not a heading\n" + _T * 3 + "\nafter\n"
    sections = parse_sections(text)
    assert len(sections) == 1
    assert sections[0].heading_path == "Real"


def test_frontmatter_delimiter_is_not_setext() -> None:
    sections = parse_sections("---\nx: 1\n---\n# A\nbody\n")
    assert len(sections) == 1
    assert sections[0].start == 4


def test_repeated_heading_paths_get_ordinals() -> None:
    sections = parse_sections("# A\none\n\n# A\ntwo\n")
    assert [s.ordinal for s in sections] == [1, 2]
    assert (sections[0].start, sections[0].end) == (1, 3)  # trailing blank included
    assert (sections[1].start, sections[1].end) == (4, 5)


def test_section_text_returns_inclusive_range() -> None:
    text = "# A\nfirst\n\n# B\nsecond\n"
    sections = parse_sections(text)
    assert section_text(text, sections[1]) == "# B\nsecond"
