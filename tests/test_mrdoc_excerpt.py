"""Tests for app/mrdoc/excerpt.py — cut ranges, fences, round-trip.

The cut rules are the report's raw material: a wrong window silently prints
the wrong lines, and a fence that is not promoted past nested code blocks
makes the artifact unparseable exactly on the files that carry code.
"""

from __future__ import annotations

from app.mrdoc.changeset import build_changeset
from app.mrdoc.excerpt import (
    ELLIPSIS,
    build_excerpts,
    parse_excerpts,
    render_excerpts,
)
from app.mrdoc.structure import build_structure

_T = chr(96)


def _changeset(*files: dict):
    return build_changeset(
        mr_iid=482,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=list(files),
    )


def _excerpts(base: dict[str, str], head: dict[str, str], *files: dict):
    changeset = _changeset(*files)
    structure = build_structure(base, head, changeset)
    return build_excerpts(structure, changeset, base, head, mr_iid=482)


def _modified(path: str, patch: str) -> dict:
    return {
        "filename": path,
        "previous_filename": None,
        "status": "modified",
        "patch": patch,
    }


def test_changed_unit_cuts_hunk_window_plus_context() -> None:
    base = {"docs/a.md": "# 설정\nalpha\nb\nc\nd\nepsilon\nf\n"}
    head = {"docs/a.md": "# 설정\nALPHA\nb\nc\nd\nEPSILON\nf\n"}
    excerpts = _excerpts(
        base, head, _modified("docs/a.md", "@@ -2,1 +2,1 @@\n@@ -6,1 +6,1 @@")
    )
    unit = excerpts.units[0]
    assert unit.kind == "changed"
    assert unit.head_lines == ((1, 3), (5, 7))  # hunk ∩ section, ±1, clamped
    assert unit.before.startswith("# 설정\nalpha\nb")
    assert ELLIPSIS in unit.before  # two cuts stay one excerpt
    assert "d\nepsilon\nf" in unit.before
    assert "ALPHA" in unit.after and "EPSILON" in unit.after


def test_context_never_spills_into_the_next_section() -> None:
    base = {"docs/a.md": "# 하나\n첫째\n# 둘\nx\n# 셋\n마지막\n"}
    head = {"docs/a.md": "# 하나\n첫째\n# 둘\nY\n# 셋\n마지막\n"}
    excerpts = _excerpts(base, head, _modified("docs/a.md", "@@ -4,1 +4,1 @@"))
    unit = excerpts.units[0]
    assert unit.head_lines == ((3, 4),)  # section 둘 is lines 3-4, context clamped
    assert "첫째" not in unit.after and "마지막" not in unit.after


def test_added_unit_carries_only_after_and_removed_only_before() -> None:
    base = {"docs/old.md": "# 구\n본문\n"}
    head = {"docs/new.md": "# 신\n새 본문\n"}
    excerpts = _excerpts(
        base,
        head,
        {
            "filename": "docs/old.md",
            "previous_filename": None,
            "status": "removed",
            "patch": "",
        },
        {
            "filename": "docs/new.md",
            "previous_filename": None,
            "status": "added",
            "patch": "",
        },
    )
    by_kind = {unit.kind: unit for unit in excerpts.units}
    assert set(by_kind) == {"added", "removed"}
    added = by_kind["added"]
    assert added.before == "" and added.after == "# 신\n새 본문"
    assert added.base_lines == () and added.context == 0
    removed = by_kind["removed"]
    assert removed.after == "" and removed.before == "# 구\n본문"
    assert removed.head_lines == ()


def _fenced_doc(version: str) -> str:
    return "\n".join(
        [
            "### 설치 (Windows)",
            "",
            "설명 문장.",
            "",
            _T * 3 + "powershell",
            "# winget",
            f"winget install --id Python.Python.{version} -e",
            "py --version",
            _T * 3,
            "",
            "> 별칭 끄기 안내.",
            "",
        ]
    )


def test_cut_grows_to_hold_a_whole_fenced_block() -> None:
    """A window cut mid-block shipped an unclosed ``` in the measured run."""

    base = {"docs/a.md": _fenced_doc("3.11")}
    head = {"docs/a.md": _fenced_doc("3.12")}
    excerpts = _excerpts(base, head, _modified("docs/a.md", "@@ -7,1 +7,1 @@"))
    unit = excerpts.units[0]
    assert unit.head_lines == ((5, 9),)  # padded 6-8 snapped out to the fence
    for text in (unit.before, unit.after):
        assert text.count(_T * 3) == 2  # opening and closing fence both present
    assert "3.11" in unit.before and "3.12" in unit.after


def test_cut_without_a_fence_keeps_the_context_window() -> None:
    base = {"docs/a.md": "# 설정\n하나\n둘\n셋\n넷\n"}
    head = {"docs/a.md": "# 설정\n하나\nDOS\n셋\n넷\n"}
    excerpts = _excerpts(base, head, _modified("docs/a.md", "@@ -3,1 +3,1 @@"))
    assert excerpts.units[0].head_lines == ((2, 4),)


def test_section_label_is_basename_and_leaf_heading() -> None:
    base = {"docs/product-a/auth.md": "# 인증\n## 5. 토큰 정책\n60분\n"}
    head = {"docs/product-a/auth.md": "# 인증\n## 5. 토큰 정책\n30분\n"}
    excerpts = _excerpts(
        base, head, _modified("docs/product-a/auth.md", "@@ -3,1 +3,1 @@")
    )
    assert excerpts.units[0].section == "auth.md § 5. 토큰 정책"


def test_nested_fence_is_promoted_and_survives_round_trip() -> None:
    body = _T * 3 + "powershell\nwinget install\n" + _T * 3
    base = {"docs/a.md": f"# 설치\n{body}\n"}
    head = {"docs/a.md": f"# 설치\n{body.replace('winget', 'scoop')}\n"}
    excerpts = _excerpts(base, head, _modified("docs/a.md", "@@ -1,4 +1,4 @@"))
    text = render_excerpts(excerpts)
    assert _T * 4 + "before" in text  # 3-tick block inside -> 4-tick fence
    assert parse_excerpts(text) == excerpts


def test_render_parse_round_trip() -> None:
    base = {"docs/a.md": "# 개요\n개요\n\n# 설정\n30초\n"}
    head = {"docs/a.md": "# 개요\n개요\n\n# 구성\n60초\n"}
    excerpts = _excerpts(base, head, _modified("docs/a.md", "@@ -1,5 +1,5 @@"))
    text = render_excerpts(excerpts)
    assert parse_excerpts(text) == excerpts
    assert excerpts.files == 1
