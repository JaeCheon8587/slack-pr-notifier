"""Tests for app/mrdoc/collect.py — assembly, the matrix, and empty prose.

Three things have to hold. The unit spine is 05-structure, not 20-analysis,
so a file the analyzer never covered still shows its kind, its raw literal
diff and its tool-determined 성질 — the design calls that degrading to "a
report without 설명" and it is the difference between an empty report and a
wrong one. The matrix is counted here once, because the report prints it
verbatim and two sources of a number leave nothing to compare. And prose
pointing at an id that does not exist is dropped and counted, never
re-prompted: the loop keeps one exit condition.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.mrdoc.analysis import AnalysisUnit, FileAnalysis
from app.mrdoc.changeset import build_changeset
from app.mrdoc.collect import (
    EXPLANATION_FAILED,
    SUMMARY_FAILED,
    build_collect,
    parse_collect,
    render_collect,
)
from app.mrdoc.excerpt import build_excerpts
from app.mrdoc.frontmatter import parse_frontmatter, parse_sections
from app.mrdoc.levelcheck import build_levelcheck
from app.mrdoc.literals import build_literals
from app.mrdoc.structure import build_structure
from app.mrdoc.themes import Theme, Themes, parse_themes, render_themes

_BASE = {"docs/p-a/auth.md": "# 개요\n권장 Python 3.12.4\n\n## 정책\n만료 60분\n"}
_HEAD = {
    "docs/p-a/auth.md": "# 개요\n권장 Python 3.12.7\n\n## 정책 v2\n만료 30분\n",
    "docs/p-a/new.md": "# 새 문서\n항목 3개\n",
}


def _inputs():
    """(changeset, structure, literals, excerpts) for the two-file fixture."""

    changeset = build_changeset(
        mr_iid=18,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=[
            {
                "filename": "docs/p-a/auth.md",
                "status": "modified",
                "patch": "@@ -1,5 +1,5 @@",
            },
            {
                "filename": "docs/p-a/new.md",
                "status": "added",
                "patch": "@@ -0,0 +1,2 @@",
            },
        ],
    )
    structure = build_structure(_BASE, _HEAD, changeset)
    literals = build_literals(structure, changeset, _BASE, _HEAD)
    excerpts = build_excerpts(structure, changeset, _BASE, _HEAD, mr_iid=18)
    return changeset, structure, literals, excerpts


def _analysis(structure, changeset, **overrides) -> FileAnalysis:
    """A full 20-analysis for auth.md — new.md is deliberately uncovered."""

    fid = next(e.fid for e in changeset.files if e.path.endswith("auth.md"))
    units = [unit for unit in structure.changed if unit.file_id == fid]
    fields: dict = {
        "file_id": fid,
        "path": "docs/p-a/auth.md",
        "units": tuple(
            AnalysisUnit(unit.unit_id, unit.section_id, "", "값 하나가 바뀌었다.")
            for unit in units
        ),
        "summary_refs": tuple(unit.unit_id for unit in units),
        "summary": "값 2곳 변경.",
        "confidence": "high — 두 절을 읽었다",
    }
    fields.update(overrides)
    return FileAnalysis(**fields)


def _collect(analyses: list[FileAnalysis], *, failed=(), themes_text: str = ""):
    changeset, structure, literals, excerpts = _inputs()
    levelcheck = build_levelcheck(18, literals, analyses, structure)
    return build_collect(
        mr_iid=18,
        analyses=analyses,
        failed_files=failed,
        levelcheck=levelcheck,
        literals=literals,
        structure=structure,
        changeset=changeset,
        excerpts=excerpts,
        verifier_text="",
        themes_text=themes_text,
    )


def _unit(collect, needle: str):
    return next(unit for unit in collect.units if needle in unit.section)


def test_unit_assembly_takes_every_fact_from_a_tool() -> None:
    changeset, structure, _literals, _excerpts = _inputs()
    collect = _collect([_analysis(structure, changeset)])
    unit = _unit(collect, "개요")
    assert unit.kind == "changed"
    assert unit.structure_kind == "none"
    assert unit.klass == "의미"  # 06 found a literal diff, so 30 verified 의미
    assert unit.section == "auth.md § 개요"
    assert [(c.key, c.from_value, c.to_value) for c in unit.changed] == [
        ("version", "3.12.4", "3.12.7")
    ]
    assert unit.excerpt_ref == unit.unit_id
    assert unit.explanation == "값 하나가 바뀌었다."


def test_renamed_heading_counts_in_two_columns() -> None:
    """구조 comes from 05, the property column from 30 — a rename is both."""

    changeset, structure, _literals, _excerpts = _inputs()
    collect = _collect([_analysis(structure, changeset)])
    unit = _unit(collect, "정책 v2")
    assert unit.structure_kind == "heading_renamed"
    assert unit.axes == ("구조", "의미")
    auth = next(e.file_id for e in collect.file_blocks if "auth" in e.path)
    assert collect.matrix[auth]["구조"] == {"추가": 0, "삭제": 0, "변경": 1}
    assert collect.matrix[auth]["의미"] == {"추가": 0, "삭제": 0, "변경": 2}


def test_added_section_counts_as_structure_only() -> None:
    """An added section's 성질 is that it appeared — not what it says."""

    changeset, structure, _literals, _excerpts = _inputs()
    collect = _collect([_analysis(structure, changeset)])
    unit = _unit(collect, "새 문서")
    assert (unit.kind, unit.klass, unit.axes) == ("added", "구조", ("구조",))
    new = next(e.file_id for e in collect.file_blocks if "new" in e.path)
    assert collect.matrix[new]["구조"]["추가"] == 1
    assert collect.classes == {"구조": 2, "의미": 2, "표현": 0, "미분류": 0}
    assert collect.files == {
        "added": 1,
        "deleted": 0,
        "modified": 1,
        "moved_sections": 0,
    }


def test_no_analysis_at_all_still_carries_the_tool_facts() -> None:
    """The design's partial-failure rule: empty prose, everything else exact."""

    collect = _collect([], failed=("auth-md.md",))
    unit = _unit(collect, "개요")
    assert unit.explanation == EXPLANATION_FAILED
    assert unit.klass == "의미"
    assert [(c.key, c.from_value, c.to_value) for c in unit.changed] == [
        ("version", "3.12.4", "3.12.7")
    ]
    assert unit.excerpt_ref == unit.unit_id
    assert all(entry.summary == SUMMARY_FAILED for entry in collect.file_blocks)
    assert collect.failed_files == ("auth-md.md",)
    assert collect.classes["의미"] == 2  # counted from the tools, not the prose


def test_explanation_for_an_unknown_unit_is_dropped_and_counted() -> None:
    changeset, structure, _literals, _excerpts = _inputs()
    analysis = _analysis(structure, changeset)
    ghost = AnalysisUnit("u-ghost", "s-ghost", "", "존재하지 않는 절 설명.")
    collect = _collect([replace(analysis, units=(*analysis.units, ghost))])
    assert collect.refs_dropped == 1
    assert "u-ghost" not in [unit.unit_id for unit in collect.units]


def test_file_summary_naming_an_unknown_ref_is_dropped() -> None:
    changeset, structure, _literals, _excerpts = _inputs()
    analysis = _analysis(structure, changeset, summary_refs=("u-ghost",))
    collect = _collect([analysis])
    assert collect.refs_dropped == 1
    assert collect.file_blocks[0].summary == SUMMARY_FAILED
    # the units of that same file keep their 설명 — one bad ref, one drop
    assert _unit(collect, "개요").explanation == "값 하나가 바뀌었다."


def test_vocab_violation_reaches_the_artifact() -> None:
    """어휘 게이트가 잡은 것은 재작성이 아니라 리포트 4 부록으로 간다."""

    changeset, structure, _literals, _excerpts = _inputs()
    fid = next(e.fid for e in changeset.files if e.path.endswith("auth.md"))
    units = [unit for unit in structure.changed if unit.file_id == fid]
    analysis = _analysis(
        structure,
        changeset,
        units=(
            AnalysisUnit(units[0].unit_id, units[0].section_id, "", "값이 상향되었다."),
            *[
                AnalysisUnit(unit.unit_id, unit.section_id, "", "값이 바뀌었다.")
                for unit in units[1:]
            ],
        ),
    )
    collect = _collect([analysis])
    hit = next(unit for unit in collect.units if unit.vocab_violation)
    assert hit.vocab_violation == ("상향",)


def test_render_parse_round_trip() -> None:
    changeset, structure, _literals, _excerpts = _inputs()
    collect = _collect([_analysis(structure, changeset)])
    text = render_collect(collect)
    assert parse_collect(text) == collect
    assert render_collect(parse_collect(text)) == text


def test_files_are_grouped_by_product_folder() -> None:
    changeset, structure, _literals, _excerpts = _inputs()
    collect = _collect([_analysis(structure, changeset)])
    assert [entry.product_dir for entry in collect.file_blocks] == [
        "docs/p-a",
        "docs/p-a",
    ]
    assert [entry.path for entry in collect.file_blocks] == [
        "docs/p-a/auth.md",
        "docs/p-a/new.md",
    ]


def test_artifact_carries_exactly_the_declared_fields() -> None:
    """Closed sets, not a banned-word list — a new field has to be declared.

    The v2 schema is the whole guard against judgement creeping back: there
    is nowhere to put a ruling, a rank or a merge opinion, so exact equality
    on the key sets is a stronger check than grepping for the old names.
    """

    changeset, structure, _literals, _excerpts = _inputs()
    collect = _collect([_analysis(structure, changeset)])
    text = render_collect(collect)
    assert list(parse_frontmatter(text)) == [
        "mr_iid",
        "files",
        "matrix",
        "classes",
        "ops",
        "value_changes",
        "verify",
        "confidence_dist",
        "uncertain",
        "failed_files",
        "refs_dropped",
        "themes_dropped",
        "themes_failed",
    ]
    blocks = parse_sections(text)
    assert list(blocks[_unit(collect, "개요").unit_id]) == [
        "kind",
        "structure_kind",
        "class",
        "section",
        "file",
        "removed",
        "added",
        "changed",
        "excerpt_ref",
        "axes",
        "ops",
        "textual",
        "prose_added",
        "prose_removed",
    ]
    assert list(blocks[collect.file_blocks[0].file_id]) == [
        "path",
        "product_dir",
        "units",
    ]
    # the only prose markers are one 설명 per unit and one summary per file
    markers = [line.split("**")[1] for line in text.splitlines() if line[:2] == "**"]
    assert sorted(set(markers)) == ["FILE_SUMMARY", "설명"]
    assert len(markers) == len(collect.units) + len(collect.file_blocks)


def test_themes_gate_keeps_only_two_member_themes() -> None:
    """Members are measured against this collect — prose counts for nothing."""

    changeset, structure, _literals, _excerpts = _inputs()
    base = _collect([_analysis(structure, changeset)])
    known = [unit.unit_id for unit in base.units]
    text = render_themes(
        Themes(
            mr_iid=18,
            themes=(
                Theme("t-01", "버전 정책", "버전 값이 함께 바뀌었다.", tuple(known[:2])),
                Theme("t-02", "고아 주제", "존재하지 않는 유닛만 가리킨다.", ("u-00000000",)),
                Theme("t-03", "외톨이", "멤버가 하나뿐이다.", (known[0], "u-00000000")),
            ),
        )
    )
    gated = _collect([_analysis(structure, changeset)], themes_text=text)
    assert [theme.theme_id for theme in gated.themes] == ["t-01"]
    assert gated.themes[0].units == tuple(known[:2])
    assert gated.themes_dropped == 2
    assert gated.themes_failed is False


def test_themes_parse_failure_flags_the_report_not_kills_it() -> None:
    changeset, structure, _literals, _excerpts = _inputs()
    good = render_themes(
        Themes(
            mr_iid=18,
            themes=(Theme("t-01", "버전 정책", "버전 값이 함께 바뀌었다.", ("u-1", "u-2")),),
        )
    )
    bad = good.replace("themes: 1", "themes: 3")
    collect = _collect([_analysis(structure, changeset)], themes_text=bad)
    assert collect.themes == ()
    assert collect.themes_failed is True


def test_themes_plain_preamble_drift_is_salvaged() -> None:
    """MR !34's drift: fields as prose first, '---' as the boundary."""

    rendered = render_themes(
        Themes(
            mr_iid=34,
            themes=(
                Theme("t-01", "버전 값 갱신", "버전 값이 바뀌었다.", ("u-1", "u-2")),
            ),
        )
    )
    drifted = rendered.replace("---\n", "", 1)  # the opening fence is gone
    assert parse_themes(drifted) == parse_themes(rendered)


def test_themes_prose_preamble_is_still_rejected() -> None:
    rendered = render_themes(Themes(mr_iid=34))
    with pytest.raises(ValueError):
        parse_themes("주제를 정리했다.\n" + rendered)


def test_themes_failed_status_flags_the_report() -> None:
    changeset, structure, _literals, _excerpts = _inputs()
    stub = render_themes(
        Themes(mr_iid=18, status="FAILED — satellite wrote nothing")
    )
    collect = _collect([_analysis(structure, changeset)], themes_text=stub)
    assert collect.themes == ()
    assert collect.themes_failed is True
