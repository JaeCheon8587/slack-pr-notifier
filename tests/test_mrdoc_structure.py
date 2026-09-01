"""Tests for app/mrdoc/structure.py — pairing rules and move detection."""

from __future__ import annotations

from app.mrdoc.changeset import build_changeset
from app.mrdoc.structure import build_structure, parse_structure, render_structure


def _changeset(*files: dict) -> object:
    return build_changeset(
        mr_iid=1,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=list(files),
    )


def test_same_heading_pairs_even_when_rewritten() -> None:
    base = {"docs/a.md": "# 설정\n타임아웃 30초\n"}
    head = {"docs/a.md": "# 설정\n완전히 새로운 내용이다\n"}
    cs = _changeset(
        {
            "filename": "docs/a.md",
            "previous_filename": None,
            "status": "modified",
            "patch": "@@ -1,2 +1,2 @@",
        }
    )
    units = build_structure(base, head, cs).changed
    assert len(units) == 1
    assert units[0].kind == "modified"


def test_identical_paired_section_emits_no_unit() -> None:
    base = {"docs/a.md": "# 개요\n내용\n\n# 설정\n값\n"}
    head = {"docs/a.md": "# 개요\n내용\n\n# 설정\n값\n"}
    cs = _changeset(
        {
            "filename": "docs/a.md",
            "previous_filename": None,
            "status": "modified",
            "patch": "@@ -1,4 +1,4 @@",
        }
    )
    assert build_structure(base, head, cs).changed == ()


def test_renamed_heading_pairs_by_similarity() -> None:
    body = "\n".join(f"설정 항목 {i}에 대한 긴 설명 텍스트" for i in range(6))
    base = {"docs/a.md": "# 설정\n" + body + "\n"}
    head = {"docs/a.md": "# 구성\n" + body + "\n"}
    cs = _changeset(
        {
            "filename": "docs/a.md",
            "previous_filename": None,
            "status": "modified",
            "patch": "@@ -1,8 +1,8 @@",
        }
    )
    units = build_structure(base, head, cs).changed
    assert len(units) == 1
    assert units[0].kind == "modified"


def test_unrelated_headings_split_into_removed_and_added() -> None:
    base = {"docs/a.md": "# 삭제된 절\nxxxxxxxx yyyyyy\n"}
    head = {"docs/a.md": "# 새로운 절\nzzzzzzzz wwwwww\n"}
    cs = _changeset(
        {
            "filename": "docs/a.md",
            "previous_filename": None,
            "status": "modified",
            "patch": "@@ -1,2 +1,2 @@",
        }
    )
    kinds = sorted(u.kind for u in build_structure(base, head, cs).changed)
    assert kinds == ["added", "removed"]


def test_cross_file_move_excluded_from_review() -> None:
    base = {"docs/b.md": "# 가이드\n가이드 본문\n"}
    head = {"docs/c.md": "# 가이드\n가이드 본문\n"}
    cs = _changeset(
        {"filename": "docs/b.md", "previous_filename": None, "status": "removed", "patch": ""},
        {"filename": "docs/c.md", "previous_filename": None, "status": "added", "patch": ""},
    )
    structure = build_structure(base, head, cs)
    assert structure.changed == ()
    assert len(structure.moved) == 1
    assert structure.moved[0].from_file == "docs/b.md"
    assert structure.moved[0].to_file == "docs/c.md"


def test_added_and_removed_file_units() -> None:
    base = {"docs/old.md": "# 구\n본문\n"}
    head = {"docs/new.md": "# 신\n본문\n"}
    cs = _changeset(
        {"filename": "docs/old.md", "previous_filename": None, "status": "removed", "patch": ""},
        {"filename": "docs/new.md", "previous_filename": None, "status": "added", "patch": ""},
    )
    kinds = sorted(u.kind for u in build_structure(base, head, cs).changed)
    assert kinds == ["added", "removed"]


def test_render_parse_roundtrip() -> None:
    base = {"docs/a.md": "# 개요\n개요\n\n# 설정\n30초\n"}
    head = {"docs/a.md": "# 개요\n개요\n\n# 구성\n60초\n"}
    cs = _changeset(
        {
            "filename": "docs/a.md",
            "previous_filename": None,
            "status": "modified",
            "patch": "@@ -1,4 +1,4 @@",
        }
    )
    structure = build_structure(base, head, cs)
    assert parse_structure(render_structure(structure)) == structure
