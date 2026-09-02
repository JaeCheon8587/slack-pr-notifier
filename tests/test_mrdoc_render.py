"""Tests for app/mrdoc/report_render.py — verdict/ref gates + escaping."""

from __future__ import annotations

import pytest

from app.mrdoc.collect import Collect, CollectedUnit
from app.mrdoc.frontmatter import render_frontmatter, render_section
from app.mrdoc.report_render import parse_reportdata, render_report_html


def _collect(verdict: str = "PASS") -> Collect:
    return Collect(
        mr_iid=17,
        verdict=verdict,
        reason="수집 사유 문구",
        levels={"L1": 0, "L2": 1, "L3": 0, "promoted": 0},
        files={"added": 0, "deleted": 0, "moved_sections": 0},
        counts={"blocker": 0, "major": 0, "minor": 0},
        gate={"files_parsed": 1, "findings_in": 0, "findings_out": 0},
        verify={"rounds": 1, "verdict": "APPROVE", "outstanding": 0},
        coverage={"checks": 0, "answered": 0, "missing": []},
        confidence_dist={"high": 1, "medium": 0, "low": 0},
        uncertain=(),
        failed_files=(),
        must_read=(),
        units=(
            CollectedUnit(
                unit_id="u-1",
                level="L2",
                section="docs/a.md § 가이드",
                file="docs/a.md",
                removed=(),
                added=(),
                changed=(),
                before="이전",
                after="이후",
                confidence="high",
            ),
        ),
        findings=(),
        dropped=(),
    )


def _report(
    verdict: str = "PASS",
    headline_body: str = "요약 문장",
    headline_refs: tuple[str, ...] = (),
    reason_body: str = "사유 문장",
    reason_refs: tuple[str, ...] = (),
) -> str:
    parts = [
        render_frontmatter(
            {
                "verdict": verdict,
                "sentences": 2,
                "STATUS": "OK",
                "SOURCES": '"1/1"',
                "UNSOURCED": "none",
                "CONFLICTS": "none",
                "UNCOVERED": "none",
                "CONFIDENCE": "high",
            }
        ),
        "",
        render_section("HEADLINE", "", {"refs": list(headline_refs)}),
        headline_body,
        "",
        render_section("VERDICT_REASON", "", {"refs": list(reason_refs)}),
        reason_body,
    ]
    return "\n".join(parts) + "\n"


def test_verdict_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="verdict mismatch"):
        render_report_html(_collect("PASS"), _report(verdict="REVIEW"), mr_iid=17)


def test_block_with_bad_refs_is_dropped() -> None:
    html = render_report_html(
        _collect("PASS"),
        _report(reason_body="유령 블록 문장", reason_refs=("u-ghost",)),
        mr_iid=17,
    )
    assert "수집 사유 문구" in html  # collect.reason fallback
    assert "유령 블록 문장" not in html


def test_valid_blocks_render_and_scripts_escape() -> None:
    html = render_report_html(
        _collect("PASS"),
        _report(headline_body="<script>alert(1)</script>"),
        mr_iid=17,
        diff_url="http://example.invalid/mr/17/diffs",
    )
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "MR diff 보기" in html
    assert "docs/a.md § 가이드" in html


def test_parse_reportdata_strips_fences_from_bodies() -> None:
    data = parse_reportdata(_report())
    headline = next(b for b in data.blocks if b.kind == "HEADLINE")
    assert headline.body == "요약 문장"
    assert headline.refs == ()
