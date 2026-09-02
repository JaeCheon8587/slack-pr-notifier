"""Tests for app/mrdoc/collect.py — the deterministic finding gate."""

from __future__ import annotations

from app.mrdoc.analysis import AnalysisFinding, AnalysisUnit, Evidence, FileAnalysis
from app.mrdoc.changeset import Changeset, FileEntry
from app.mrdoc.collect import build_collect
from app.mrdoc.frontmatter import render_frontmatter
from app.mrdoc.levelcheck import LevelCheck, LevelRow
from app.mrdoc.literals import ChangedValue, Literals, UnitLiterals
from app.mrdoc.structure import Structure, TreeSection

BASE_TREE = {"docs/a.md": "intro\nRetry Count: 3\n"}
HEAD_TREE = {"docs/a.md": "Retry Count: 5\noutro\n"}


def _structure() -> Structure:
    return Structure(
        tree=(
            TreeSection(
                section_id="s-x",
                file="docs/a.md",
                heading_path="guide > setup",
                lines=(1, 2),
            ),
        ),
        changed=(),
        moved=(),
    )


def _changeset() -> Changeset:
    return Changeset(
        mr_iid=17,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        files=(
            FileEntry(
                path="docs/a.md",
                status="modified",
                old_path=None,
                new_ranges=((1, 2),),
                old_ranges=((1, 2),),
            ),
        ),
        non_md=(),
        skipped=False,
    )


def _levelcheck(verified: str = "L2") -> LevelCheck:
    return LevelCheck(
        mr_iid=17,
        units=(LevelRow(unit_id="u-1", claimed="L2", verified=verified, basis="b"),),
    )


def _literals() -> Literals:
    return Literals(
        units=(
            UnitLiterals(
                unit_id="u-1",
                section_id="s-x",
                removed=(),
                added=(),
                changed=(ChangedValue("Retry Count", "3", "5"),),
            ),
        )
    )


def _analysis(findings: tuple[AnalysisFinding, ...] = ()) -> FileAnalysis:
    return FileAnalysis(
        file_id="docs-a-md",
        path="docs/a.md",
        units=(AnalysisUnit("u-1", "s-x", "L2", "이전 맥락", "이후 맥락"),),
        findings=findings,
        confidence="high",
    )


def _verifier_text() -> str:
    return render_frontmatter(
        {
            "mr_iid": 17,
            "round": 1,
            "verdict": "APPROVE",
            "checked": 1,
            "required_fixes": 0,
            "uncovered": "none",
            "uncertain": "none",
        }
    )


def _evidence(rev: str, line: int, quote: str) -> Evidence:
    return Evidence(role="conflicting", rev=rev, file="docs/a.md", line=line, quote=quote)


def _finding(
    fid: str,
    category: str,
    unit_id: str = "u-1",
    evidence: tuple[Evidence, ...] = (),
) -> AnalysisFinding:
    return AnalysisFinding(
        finding_id=fid,
        unit_id=unit_id,
        category=category,
        evidence=evidence,
        claim="클레임",
        recommendation="권고",
    )


def _collect(
    analyses: list[FileAnalysis], levelcheck: LevelCheck | None = None
):
    return build_collect(
        mr_iid=17,
        analyses=analyses,
        failed_files=(),
        levelcheck=levelcheck or _levelcheck(),
        literals=_literals(),
        structure=_structure(),
        changeset=_changeset(),
        verifier_text=_verifier_text(),
        base_tree=BASE_TREE,
        head_tree=HEAD_TREE,
    )


def test_blocker_on_conflicting_literal_values() -> None:
    finding = _finding(
        "f-01",
        "contradiction",
        evidence=(
            _evidence("base", 2, "Retry Count: 3"),
            _evidence("head", 1, "Retry Count: 5"),
        ),
    )
    collect = _collect([_analysis((finding,))])
    assert collect.findings[0].severity == "BLOCKER"
    assert collect.verdict == "BLOCK"
    assert "모순" in collect.reason
    assert collect.must_read[0] == "f-01"


def test_quote_mismatch_drops_finding() -> None:
    finding = _finding(
        "f-01",
        "stale_reference",
        evidence=(_evidence("head", 1, "이런 문장은 원문에 없다"),),
    )
    collect = _collect([_analysis((finding,))])
    assert collect.findings == ()
    assert collect.dropped[0].reason == "quote_mismatch"
    assert collect.gate["dropped_quote_mismatch"] == 1
    assert collect.verdict == "PASS"


def test_dedup_merges_same_content() -> None:
    evidence = (_evidence("base", 2, "Retry Count: 3"),)
    first = _finding("f-01", "stale_reference", evidence=evidence)
    second = _finding("f-02", "stale_reference", evidence=evidence)
    collect = _collect([_analysis((first, second))])
    assert len(collect.findings) == 1
    assert collect.gate["merged_dup"] == 1
    assert collect.gate["findings_out"] == 1


def test_major_finding_forces_review() -> None:
    finding = _finding(
        "f-01",
        "stale_reference",
        evidence=(_evidence("head", 1, "Retry Count: 5"),),
    )
    collect = _collect([_analysis((finding,))])
    assert collect.findings[0].severity == "MAJOR"
    assert collect.verdict == "REVIEW"


def test_minor_only_passes() -> None:
    finding = _finding(
        "f-01",
        "terminology",
        evidence=(_evidence("head", 1, "Retry Count: 5"),),
    )
    collect = _collect([_analysis((finding,))])
    assert collect.findings[0].severity == "MINOR"
    assert collect.verdict == "PASS"


def test_l1_unit_forces_review_without_findings() -> None:
    collect = _collect([_analysis()], levelcheck=_levelcheck("L1"))
    assert collect.verdict == "REVIEW"
    assert "맥락이 바뀐 절 1건" in collect.reason
    assert collect.must_read == ("u-1",)
