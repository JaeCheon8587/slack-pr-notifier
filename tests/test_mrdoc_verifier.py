"""Tests for app/mrdoc/verifier.py — the audit contract and its drift gate.

Two things must not slip. The counts have to come from the blocks, because
required_fixes is the single exit condition of the pipeline's one retry loop
and a self-reported zero over three FIX blocks would skip the round. And the
prompt's template has to be the string the parser accepts — the measured
failure was a hand-written template no test ever parsed.
"""

from __future__ import annotations

from app.mrdoc import satellites
from app.mrdoc.frontmatter import parse_frontmatter, parse_sections
from app.mrdoc.verifier import (
    CountMismatch,
    CountsCheck,
    Fix,
    Verifier,
    VerifierUnit,
    parse_verifier,
    render_verifier,
    verify_summary,
)

_T = chr(96)


def _verifier() -> Verifier:
    return Verifier(
        mr_iid=482,
        round=1,
        checked=3,
        uncovered="none",
        uncertain="none",
        confidence="high — 3개 절의 base/head 원문을 직접 읽음",
        units=(
            VerifierUnit("u-7f21bc", "ok", "", "agree"),
            VerifierUnit(
                "u-2c81de",
                "invented",
                "의미",
                "agree",
                why="설명이 원문에 없는 범위를 만들었다.",
            ),
            VerifierUnit("u-9a03ff", "ok", "표현", "dispute"),
        ),
        counts=(
            CountsCheck(
                file_id="f-9a03",
                mismatches=(
                    CountMismatch("f-9a03", "삭제 0", "삭제 2"),
                ),
                why="FILE_SUMMARY 가 삭제 없다고 썼다.",
            ),
        ),
        fixes=(
            Fix("r-01", "u-2c81de", "설명", "fidelity_invented"),
            Fix("r-02", "f-9a03", "FILE_SUMMARY", "counts_mismatch"),
        ),
    )


def test_render_parse_round_trip() -> None:
    verifier = _verifier()
    text = render_verifier(verifier)
    assert parse_verifier(text) == verifier
    assert render_verifier(parse_verifier(text)) == text


def test_counts_are_measured_from_the_blocks() -> None:
    verifier = _verifier()
    assert verifier.fidelity == {"ok": 2, "invented": 1, "omitted": 0}
    assert verifier.class_opinion == {"agree": 2, "dispute": 1}
    assert verifier.required_fixes == 2
    assert verifier.fix_targets() == ("u-2c81de", "f-9a03")
    assert verifier.counts_mismatches == 1


def test_self_reported_zero_cannot_hide_a_fix_block() -> None:
    """required_fixes is the loop's only exit — it comes from the FIX blocks."""

    text = render_verifier(_verifier()).replace("required_fixes: 2", "required_fixes: 0")
    assert parse_verifier(text).required_fixes == 2


def test_artifact_carries_exactly_the_declared_fields() -> None:
    """Closed sets: the audit has nowhere to file a ruling or a rank."""

    text = render_verifier(_verifier())
    assert list(parse_frontmatter(text)) == [
        "mr_iid",
        "round",
        "checked",
        "fidelity",
        "class_opinion",
        "counts",
        "required_fixes",
        "uncovered",
        "uncertain",
        "confidence",
    ]
    blocks = parse_sections(text)
    assert list(blocks["u-7f21bc"]) == ["fidelity", "class_stated", "class_opinion"]
    assert list(blocks["r-01"]) == ["target", "field", "reason"]
    assert list(blocks["f-9a03"]) == ["mismatch", "why"]


def test_why_line_joins_its_wrapped_continuations() -> None:
    text = render_verifier(_verifier()).replace(
        "**why** 설명이 원문에 없는 범위를 만들었다.",
        "**why** 설명이 원문에\n없는 범위를 만들었다.",
    )
    parsed = parse_verifier(text)
    assert parsed.units[1].why == "설명이 원문에 없는 범위를 만들었다."


def test_receipt_block_is_accepted_and_ignored() -> None:
    text = render_verifier(_verifier())
    text += "\n## RECEIPT\n" + _T * 3 + "\nSTATUS: OK\n" + _T * 3 + "\n"
    parsed = parse_verifier(text)
    assert [unit.unit_id for unit in parsed.units] == [
        "u-7f21bc",
        "u-2c81de",
        "u-9a03ff",
    ]
    assert parsed.required_fixes == 2


def test_verify_summary_reads_rounds_and_outstanding() -> None:
    assert verify_summary(render_verifier(_verifier())) == {
        "rounds": 1,
        "outstanding": 2,
        "counts_mismatch": 1,
    }


def test_verify_summary_degrades_instead_of_raising() -> None:
    """A missing or broken audit must still let the report render."""

    assert verify_summary("no frontmatter at all\n") == {
        "rounds": 1,
        "outstanding": 0,
        "counts_mismatch": 0,
    }


def test_prompt_template_is_what_the_parser_reads() -> None:
    """Drift gate: the exact string in the verifier prompt must parse back."""

    template = satellites._VERIFIER_TEMPLATE
    parsed = parse_verifier(template)
    assert [unit.unit_id for unit in parsed.units] == ["u-xxxxxxxx", "u-yyyyyyyy"]
    assert [unit.fidelity for unit in parsed.units] == ["ok", "invented"]
    assert parsed.required_fixes == 2
    assert parsed.fixes[0].reason == "fidelity_invented"
    assert parsed.fixes[1].reason == "counts_mismatch"
    assert parsed.counts[0].file_id == "f-zzzzzzzz"
    assert render_verifier(parsed) == template


def test_spec_reads_the_evidence_and_not_the_vocab_gate(tmp_path) -> None:
    """40 은 충실도만 본다 — 어휘 게이트는 levelcheck 의 것이고 거기 남는다."""

    from app.mrdoc import dispatch
    from app.mrdoc.workspace import artifact_paths

    paths = artifact_paths(tmp_path)
    for key in ("changeset", "structure", "literals", "excerpt", "levelcheck"):
        paths[key].write_text("x", encoding="utf-8")
    (tmp_path / ".analysis-expected").write_text("0", encoding="utf-8")
    specs = dispatch.next_specs(tmp_path, wave=1, fanout=5, budget_usd=1.0)
    verifier = next(spec for spec in specs if spec.agent == "verifier")
    assert set(verifier.read) == {
        paths["analysis_dir"],
        paths["excerpt"],
        paths["literals"],
    }


def test_prompt_has_no_v1_judgement_vocabulary() -> None:
    text = satellites._VERIFIER_SYSTEM + satellites._VERIFIER_TEMPLATE
    for gone in ("APPROVE", "REVISE", "distorted", "level_opinion", "L2"):
        assert gone not in text, gone
