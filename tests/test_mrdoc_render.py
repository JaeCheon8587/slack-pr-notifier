"""Tests for app/mrdoc/report_render.py — the six sections, one gate, escaping.

The page's contract is that the deterministic half always renders. A run
whose 설명 all failed must still show the overview, the matrix, the heading
trees, the raw text and the literal values; only the prose says it failed.
The other half is the refs gate: a block pointing at a file no block
declares is dropped and counted, never re-prompted.
"""

from __future__ import annotations

import re
from dataclasses import replace

from app.mrdoc.analysis import AnalysisUnit, FileAnalysis
from app.mrdoc.changeset import build_changeset
from app.mrdoc.collect import build_collect, parse_collect, render_collect
from app.mrdoc.excerpt import build_excerpts
from app.mrdoc.levelcheck import build_levelcheck
from app.mrdoc.literals import build_literals
from app.mrdoc.report_render import (
    _strip_ids,
    overview_lines,
    render_report_html,
    render_slack_summary,
    structure_notes,
)
from app.mrdoc.themes import Theme
from app.mrdoc.structure import build_structure

_BASE = {"docs/p-a/setup.md": "# 개요\n권장 Python 3.12.4\n\n## 정책\n만료 60분\n"}
_HEAD = {
    "docs/p-a/setup.md": "# 개요\n권장 Python 3.12.7\n\n## 정책 v2\n만료 30분\n",
    "docs/p-a/new.md": "# 새 문서\n항목 3개\n",
}

_RAW_FILES = [
    {
        "filename": "docs/p-a/setup.md",
        "status": "modified",
        "patch": "@@ -1,5 +1,5 @@",
    },
    {"filename": "docs/p-a/new.md", "status": "added", "patch": "@@ -0,0 +1,2 @@"},
]


def _pipeline(*, explain: bool = True):
    """Run the deterministic chain, then collect — returns what render reads."""

    changeset = build_changeset(
        mr_iid=18,
        project_id="p",
        base_sha="9c1e77a",
        head_sha="ee95ae7",
        start_sha="9c1e77a",
        raw_files=_RAW_FILES,
    )
    structure = build_structure(_BASE, _HEAD, changeset)
    literals = build_literals(structure, changeset, _BASE, _HEAD)
    excerpts = build_excerpts(structure, changeset, _BASE, _HEAD, mr_iid=18)
    fid = next(e.fid for e in changeset.files if e.path.endswith("setup.md"))
    analyses: list[FileAnalysis] = []
    if explain:
        units = [unit for unit in structure.changed if unit.file_id == fid]
        analyses.append(
            FileAnalysis(
                file_id=fid,
                path="docs/p-a/setup.md",
                units=tuple(
                    AnalysisUnit(
                        unit.unit_id,
                        unit.section_id,
                        "",
                        "권장 버전 3.12.4 → 3.12.7 로 바뀌었다.",
                    )
                    for unit in units
                ),
                summary_refs=tuple(unit.unit_id for unit in units),
                summary="버전 문구 1곳, 만료 값 1곳 변경.",
                confidence="high — 두 절을 읽었다",
            )
        )
    levelcheck = build_levelcheck(18, literals, analyses, structure)
    collect = build_collect(
        mr_iid=18,
        analyses=analyses,
        # what load_analyses_split reports: the artifact name, not the doc path
        failed_files=() if explain else (fid + ".md",),
        levelcheck=levelcheck,
        literals=literals,
        structure=structure,
        changeset=changeset,
        excerpts=excerpts,
        verifier_text="",
    )
    return parse_collect(render_collect(collect)), structure, excerpts


def _page(**kwargs) -> str:
    collect, structure, excerpts = _pipeline(**kwargs)
    return render_report_html(
        collect,
        mr_iid=18,
        excerpts=excerpts,
        structure=structure,
        base_sha="9c1e77a",
        head_sha="ee95ae7",
        diff_url="http://example.invalid/mr/18/diffs",
    )


def test_page_has_the_six_sections() -> None:
    page = _page()
    for heading in (
        "1. 개요",
        "2. 주요 변경사항",
        "3. 변경 매트릭스",
        "4. 파일별 상세",
        "5. 분석 상태",
        "6. 부록",
        "4.1 구조 변화",
        "4.2 추가된 내용",
        "4.3 삭제된 내용",
        "4.4 변경된 내용",
    ):
        assert heading in page, heading


def test_value_change_and_raw_text_appear_together() -> None:
    """4.4 carries the literal diff beside the excerpt — tools, then 설명."""

    page = _page()
    assert "version 3.12.4 → 3.12.7" in page
    assert "권장 Python 3.12.4" in page  # 07 before
    assert "권장 Python 3.12.7" in page  # 07 after
    assert "권장 버전 3.12.4 → 3.12.7 로 바뀌었다." in page
    assert "[의미]" in page


def test_slack_summary_is_section_one_verbatim() -> None:
    collect, _structure, _excerpts = _pipeline()
    lines = overview_lines(collect, mr_iid=18, base_sha="9c1e77a", head_sha="ee95ae7")
    summary = render_slack_summary(
        collect,
        mr_iid=18,
        base_sha="9c1e77a",
        head_sha="ee95ae7",
        link="http://example.invalid/mr/18/diffs",
    )
    assert summary.splitlines() == [*lines, "http://example.invalid/mr/18/diffs"]
    assert "[리포트 신뢰도]" in summary
    assert "권장 Python" not in summary  # no document body ever reaches Slack
    page = _page()
    for line in lines:
        assert line in page, line


def test_page_renders_with_no_analysis_at_all() -> None:
    """LLM failure isolated: overview, matrix, trees, raw text, values intact."""

    page = _page(explain=False)
    assert "1. 개요" in page
    assert "version 3.12.4 → 3.12.7" in page
    assert "권장 Python 3.12.7" in page
    assert "설명 생성 실패" in page
    assert "FILE_SUMMARY 생성 실패" in page
    assert "개명" in page  # 3.1 still describes the heading change
    # two documents, counted once each — the unreadable 20-analysis artifact
    # name resolves through file_id instead of adding a third entry
    assert "설명 생성 실패 파일 2" in page


def test_prose_is_escaped() -> None:
    collect, structure, excerpts = _pipeline()
    hostile = tuple(
        replace(unit, explanation="<script>alert(1)</script>")
        for unit in collect.units
    )
    page = render_report_html(
        replace(collect, units=hostile),
        mr_iid=18,
        excerpts=excerpts,
        structure=structure,
    )
    assert "&lt;script&gt;" in page
    assert "<script>alert" not in page


def test_unit_pointing_at_an_unknown_file_is_dropped() -> None:
    collect, structure, excerpts = _pipeline()
    orphan = replace(collect.units[0], file="no-such-file")
    broken = replace(collect, units=(orphan, *collect.units[1:]))
    page = render_report_html(
        broken, mr_iid=18, excerpts=excerpts, structure=structure
    )
    assert "refs 드롭 1" in page
    assert orphan.unit_id not in page


def test_appendix_carries_the_fixed_criteria() -> None:
    page = _page()
    assert "분류 기준표" in page
    assert "헤딩 · 파일 · 절의 존재 · 위치 · 레벨이 바뀜. 본문 명제는 무관" in page
    assert "명제 동일 — 동의어 · 어순 · 오탈자 · 서식 · 문장 분할/병합" in page
    assert "유닛 ID · 소스 대응" in page
    assert "주제 · 유닛 대응" in page
    assert "부분 실패 목록" in page


def test_page_shows_only_the_six_sections_and_three_properties() -> None:
    """Closed sets: no banner, no ranked cards, no level labels, no 권고."""

    page = _page()
    assert re.findall(r"<h2>([^<]*)</h2>", page) == [
        "1. 개요",
        "2. 주요 변경사항",
        "3. 변경 매트릭스",
        "4. 파일별 상세",
        "5. 분석 상태",
        "6. 부록",
    ]
    tags = set(re.findall(r'<span class="tag">\[([^\]]*)\]</span>', page))
    # a mixed unit's tag lists every axis it touches, ' · '-joined
    atoms = {axis for tag in tags for axis in tag.split(" · ")}
    assert atoms <= {"구조", "의미", "표현", "미분류"}
    assert "부분" not in atoms  # the partial note is not an axis
    assert "권고" not in page
    assert not {"L1", "L2", "L3"} & set(re.findall(r"L[123]", page))


def test_themes_section_measures_members_and_hides_ids() -> None:
    """Section 2 counts what the members cover — ids live in the appendix."""

    collect, structure, excerpts = _pipeline()
    ids = tuple(unit.unit_id for unit in collect.units)
    themed = replace(
        collect,
        themes=(Theme("t-01", "버전 정책", "버전 요구 값이 2곳 바뀌었다.", ids),),
    )
    page = render_report_html(
        themed, mr_iid=18, excerpts=excerpts, structure=structure
    )
    assert "<h3>버전 정책</h3>" in page
    assert "유닛 %d" % len(ids) in page
    assert "t-01" in page  # appendix 주제 · 유닛 대응 — the one place ids appear


def test_slack_summary_lists_top_themes_before_the_trust_line() -> None:
    collect, _structure, _excerpts = _pipeline()
    ids = tuple(unit.unit_id for unit in collect.units)
    themed = replace(
        collect,
        themes=(
            Theme("t-01", "버전 정책", "버전 요구 값이 2곳 바뀌었다.", ids),
            Theme("t-02", "문서 개편", "절 구성이 바뀌었다.", ids[:1]),
        ),
    )
    summary = render_slack_summary(themed, mr_iid=18)
    lines = summary.splitlines()
    assert "주요 주제: 버전 정책 · 문서 개편" in lines
    assert lines[-2].startswith("주요 주제: ")
    assert "[리포트 신뢰도]" in lines[-1]


def test_strip_ids_removes_hashes_from_prose() -> None:
    assert (
        _strip_ids("값 변경 (u-a0c3709c, head 1-2) — 상향")
        == "값 변경 (head 1-2) — 상향"
    )
    assert _strip_ids("u-a0c3709c") == ""


def test_pure_reorder_is_read_off_the_two_trees() -> None:
    """Identical text in a new place makes no unit — only the trees show it."""

    base = {"docs/x.md": "# A\n알파\n\n# B\n베타\n"}
    head = {"docs/x.md": "# B\n베타\n\n# A\n알파\n"}
    changeset = build_changeset(
        mr_iid=1,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=[
            {
                "filename": "docs/x.md",
                "status": "modified",
                "patch": "@@ -1,5 +1,5 @@",
            }
        ],
    )
    structure = build_structure(base, head, changeset)
    assert structure.changed == ()
    assert structure_notes(structure, "docs/x.md") == [
        "§B 가 문서 맨 앞으로 이동되었다",
        "§A 가 §B 뒤로 이동되었다",
    ]


def test_rename_and_no_change_both_get_a_sentence() -> None:
    _collect, structure, _excerpts = _pipeline()
    notes = structure_notes(structure, "docs/p-a/setup.md")
    assert "§정책 가 §정책 v2 로 개명되었다" in notes
    assert structure_notes(structure, "docs/p-a/new.md") == [
        "§새 문서 절이 문서 맨 앞에 추가되었다"
    ]


def test_page_has_only_the_matrix_in_section_three() -> None:
    """3번은 매트릭스 표 하나로 끝난다 — 추가 차트 블록 없음."""

    page = _page()
    start = page.index("3. 변경 매트릭스")
    end = page.index("4. 파일별 상세")
    block = page[start:end]
    assert "파일 × 축 밀도" not in block
    assert "유닛 분포" not in block
    assert "<svg" not in block
    assert not re.search(r"u-[0-9a-f]{8}", block)
