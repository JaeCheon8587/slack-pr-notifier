"""Tests for app/mrdoc/analysis.py — the v2 20-analysis contract.

The measured failure was a parser/producer mismatch that nothing caught: the
file was discarded whole and the pipeline still reported complete. So the
contract tested here is narrow and total — what renders must parse back, a
RECEIPT block must not break it, and the schema must have no room for a
judgement field.
"""

from __future__ import annotations

from app.mrdoc.analysis import (
    AnalysisUnit,
    FileAnalysis,
    load_analyses_split,
    parse_analysis,
    render_analysis,
)

_T = chr(96)


def _analysis() -> FileAnalysis:
    return FileAnalysis(
        file_id="auth-md-a3c19d",
        path="docs/product-a/auth.md",
        units=(
            AnalysisUnit(
                unit_id="u-2c81de",
                section_id="s-2c81",
                klass="",
                explanation="Access Token 만료 값이 60분 에서 30분 으로 바뀌었다.",
            ),
            AnalysisUnit(
                unit_id="u-9a03ff",
                section_id="s-3f11",
                klass="표현",
                explanation="문장 두 개가 하나로 병합되었다.",
            ),
        ),
        summary_refs=("u-2c81de", "u-9a03ff"),
        summary="값이 바뀐 절 1곳, 표현 수정 1곳.",
        confidence="high — base/head 양쪽 원문 직접 대조",
    )


def test_render_parse_round_trip() -> None:
    analysis = _analysis()
    text = render_analysis(analysis)
    assert parse_analysis(text) == analysis
    assert render_analysis(parse_analysis(text)) == text


def test_class_counts_split_claims_from_the_tool_territory() -> None:
    counts = _analysis().class_counts()
    assert counts == {"의미": 1, "표현": 1, "후보판정": 1}


def test_frontmatter_carries_the_required_fields() -> None:
    text = render_analysis(_analysis())
    for key in ("file_id:", "path:", "units:", "classes:", "STATUS:", "UNCOVERED:"):
        assert key in text
    assert "UNCERTAIN:" in text and "CONFIDENCE:" in text


def test_receipt_block_is_accepted_and_ignored() -> None:
    text = render_analysis(_analysis())
    text += "\n## RECEIPT\n" + _T * 3 + "\nSTATUS: OK\n" + _T * 3 + "\n"
    parsed = parse_analysis(text)
    assert [unit.unit_id for unit in parsed.units] == ["u-2c81de", "u-9a03ff"]
    assert parsed.summary == "값이 바뀐 절 1곳, 표현 수정 1곳."


def test_wrapped_explanation_lines_are_joined() -> None:
    text = render_analysis(_analysis()).replace(
        "**설명** 문장 두 개가 하나로 병합되었다.",
        "**설명** 문장 두 개가\n하나로 병합되었다.",
    )
    parsed = parse_analysis(text)
    assert parsed.units[1].explanation == "문장 두 개가 하나로 병합되었다."


def test_added_unit_carries_an_explanation_and_no_class() -> None:
    analysis = FileAnalysis(
        file_id="overview-md-b71f04",
        path="docs/product-a/overview.md",
        units=(
            AnalysisUnit(
                unit_id="u-b04c19",
                section_id="s-b310",
                klass="",
                explanation="개요 절이 새로 추가되었다.",
            ),
        ),
    )
    parsed = parse_analysis(render_analysis(analysis))
    assert parsed.units[0].klass == ""
    assert parsed.units[0].explanation == "개요 절이 새로 추가되었다."


def test_malformed_file_is_split_out_not_raised(tmp_path) -> None:
    directory = tmp_path / "20-analysis"
    directory.mkdir()
    (directory / "good.md").write_text(render_analysis(_analysis()), encoding="utf-8")
    (directory / "bad.md").write_text("no frontmatter here\n", encoding="utf-8")
    parsed, failed = load_analyses_split(directory)
    assert [item.file_id for item in parsed] == ["auth-md-a3c19d"]
    assert failed == ("bad.md",)
