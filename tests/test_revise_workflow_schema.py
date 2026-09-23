"""Tests for app/revise_workflow/schema.py — the seven artifacts' codec.

No agent is faked here: the artifacts are built as dataclasses and pushed
through the production renderers, which is also how the prompt templates are
produced ("템플릿은 렌더러가"). Asserted: `parse(render(x)) == x` for all
seven types, that a stage's block-sequence spelling of a list parses to the
same thing as the flow spelling the templates show, and the refusals the
measured runs made necessary — a sequence item with no key above it, a
missing or empty one of the four mandatory fields, and a frontmatter count
that disagrees with the blocks.
"""

from __future__ import annotations

import pytest

from app.report_html import render_review_report
from app.revise_workflow.schema import (
    OCCURRENCES_UNAVAILABLE,
    REQUIRED_KEYS,
    Anchor,
    AnchorCandidate,
    AnchorEntry,
    AnchorHit,
    AnchorRef,
    AnchorWindow,
    EditEntry,
    EditOp,
    EditReceipt,
    EditSelection,
    Evidence,
    Gate,
    GatePaths,
    GateResidue,
    GateSize,
    Impact,
    ImpactCandidate,
    ImpactEntry,
    ImpactLiteral,
    Intent,
    LinkIn,
    LiteralCheck,
    LiteralSets,
    LiteralText,
    Opinion,
    Required,
    Summary,
    Unprocessed,
    Verdict,
    Verify,
    edit_template,
    intent_template,
    join_ids,
    parse_anchor,
    parse_edit,
    parse_gate,
    parse_impact,
    parse_intent,
    parse_summary,
    parse_verify,
    render_anchor,
    render_edit,
    render_gate,
    render_impact,
    render_intent,
    render_summary,
    render_verify,
    verify_template,
)


def _required() -> Required:
    return Required(
        STATUS="OK",
        UNCOVERED="none",
        UNCERTAIN="none",
        CONFIDENCE="high — 원문과 diff 를 직접 대조",
    )


def _intent() -> Intent:
    return Intent(
        mr_iid=11,
        project_id="1009",
        round=1,
        base_sha="7c41d0e",
        opinions=(
            Opinion(
                opinion_id=41,
                op="modify",
                대상문서=("docs/install.md",),
                연관문서=("docs/README.md", "docs/spec.md"),
                범위="local",
                대상주장="설치 절차 절의 요구 버전 표기가 3.2 로 되어 있다",
                수정방향="3.2 → 4.0",
                근거="의견 원문 「설치 절차의 3.2 버전 표기를 4.0으로」",
                통합=(),
                충돌해소="none",
                search_terms=("2. 설치 절차", "요구 버전: 3.2"),
            ),
        ),
        required=_required(),
    )


def _anchor() -> Anchor:
    return Anchor(
        mr_iid=11,
        round=1,
        entries=(
            AnchorEntry(
                opinion_id=41,
                status="FOUND",
                hits=(
                    AnchorHit("2. 설치 절차", "docs/install.md", "s-3e7a", 28),
                    AnchorHit("요구 버전: 3.2", "docs/install.md", "s-3e7a", 31),
                ),
                unit_id="u-8d12f4",
                window=AnchorWindow("docs/install.md", 11, 51),
            ),
            AnchorEntry(
                opinion_id=42,
                status="MISSING",
                hits=(),
                candidates=(
                    AnchorCandidate("docs/install.md", "s-3e7a", "2. 설치 절차"),
                    AnchorCandidate("docs/install.md", "s-a02c", "1. 요구 사항"),
                    AnchorCandidate("docs/install.md", "s-f7b9", "3. 문제 해결"),
                ),
            ),
        ),
        required=_required(),
    )


def _impact() -> Impact:
    return Impact(
        mr_iid=11,
        round=1,
        entries=(
            ImpactEntry(
                opinion_id=41,
                anchor=AnchorRef("docs/install.md", "s-3e7a", "u-8d12f4"),
                literals=(ImpactLiteral("kv", "요구 버전", "3.2"),),
                links_in=(LinkIn("docs/README.md", 12, "install.md#2-설치-절차"),),
                candidates=(
                    ImpactCandidate(
                        "docs/README.md", "s-91c4", "u-c07a31", 12, "literal", "3.2"
                    ),
                    ImpactCandidate(
                        "docs/README.md",
                        "s-91c4",
                        "u-c07a31",
                        12,
                        "link",
                        "install.md#2-설치-절차",
                    ),
                ),
                related_unverified=("docs/spec.md",),
            ),
        ),
        required=_required(),
    )


def _edit() -> EditReceipt:
    return EditReceipt(
        mr_iid=11,
        round=1,
        processed=(41,),
        unprocessed=(Unprocessed(42, "앵커 MISSING — 편집 대상 없음"),),
        files_touched=("docs/install.md", "docs/README.md"),
        entries=(
            EditEntry(
                opinion_id=41,
                result="applied",
                edits=(
                    EditOp("docs/install.md", 31, "요구 버전: 3.2", "요구 버전: 4.0"),
                    EditOp("docs/README.md", 12, "3.2 기준", "4.0 기준"),
                ),
                selected=(
                    EditSelection("docs/README.md", 12, "literal", "apply"),
                    EditSelection(
                        "docs/README.md",
                        12,
                        "link",
                        "skip",
                        "링크 타깃 절 제목은 바뀌지 않는다",
                    ),
                ),
            ),
            EditEntry(
                opinion_id=42,
                result="unapplied",
                edits=(),
                reason="앵커 MISSING — 편집 대상 없음",
            ),
        ),
        required=_required(),
    )


def _gate() -> Gate:
    return Gate(
        mr_iid=11,
        round=1,
        kind="ok",
        reverted=(),
        revert_all=False,
        failed_opinion_ids=(),
        paths=GatePaths(
            changed=("docs/README.md", "docs/install.md"),
            allowed=("docs/README.md", "docs/install.md"),
            non_md=(),
            outside=(),
            reverted=(),
            result="pass",
        ),
        literals=(
            LiteralCheck(
                unit_id="u-8d12f4",
                opinion_id=41,
                from_value="3.2",
                to_value="4.0",
                sets=LiteralSets(True, True, True),
                text=LiteralText(False, True),
                occurrences=OCCURRENCES_UNAVAILABLE,
                result="pass",
            ),
            LiteralCheck(
                unit_id="u-c07a31",
                opinion_id=41,
                from_value="3.2",
                to_value="4.0",
                sets=None,
                text=LiteralText(False, True),
                occurrences=OCCURRENCES_UNAVAILABLE,
                result="pass",
            ),
        ),
        size=GateSize(files=2, max_files=10, lines=2, max_lines=400, result="pass"),
        residue=GateResidue(untracked=(), result="pass"),
        required=_required(),
    )


def _verify() -> Verify:
    return Verify(
        mr_iid=11,
        round=1,
        verdicts=(
            Verdict(
                opinion_id=41,
                verdict="applied",
                evidence=(
                    Evidence("docs/install.md", 31, "요구 버전: 3.2", "요구 버전: 4.0"),
                ),
                판정문="의견 원문이 요구한 표기 변경이 install.md 31행에서 그대로 반영되었다.",
            ),
            Verdict(opinion_id=42, verdict="unapplied", evidence=()),
        ),
        summary="설치 절차의 요구 버전 표기를 3.2 에서 4.0 으로 바꿨다.",
        required=_required(),
    )


def _summary() -> Summary:
    return Summary(
        mr_iid=11,
        round=1,
        applied=(41,),
        partial=(),
        unapplied=(42,),
        commit_paths=("docs/install.md", "docs/README.md"),
        summary="설치 절차의 요구 버전 표기를 3.2 에서 4.0 으로 바꿨다.",
        key_changes=("docs/install.md § 2. 설치 절차 — 요구 버전: 3.2 → 4.0",),
        points_to_watch=("[unapplied] 의견 42 — 판정문 없음",),
        required=_required(),
    )


ROUND_TRIPS = (
    ("intent", _intent, render_intent, parse_intent),
    ("anchor", _anchor, render_anchor, parse_anchor),
    ("impact", _impact, render_impact, parse_impact),
    ("edit", _edit, render_edit, parse_edit),
    ("gate", _gate, render_gate, parse_gate),
    ("verify", _verify, render_verify, parse_verify),
    ("summary", _summary, render_summary, parse_summary),
)


@pytest.mark.parametrize(("name", "build", "render", "parse"), ROUND_TRIPS)
def test_round_trip(name, build, render, parse) -> None:
    original = build()
    assert parse(render(original)) == original


@pytest.mark.parametrize(("name", "build", "render", "parse"), ROUND_TRIPS)
def test_every_artifact_carries_the_four_required_fields(name, build, render, parse) -> None:
    text = render(build())
    header = text.split("---")[1]
    for key in REQUIRED_KEYS:
        assert f"{key}:" in header, f"{name} lost {key}"


@pytest.mark.parametrize(("name", "build", "render", "parse"), ROUND_TRIPS)
def test_no_artifact_uses_a_block_sequence(name, build, render, parse) -> None:
    for line in render(build()).splitlines():
        assert not line.lstrip().startswith("- {"), f"{name} emitted a block sequence"


def _to_block(text: str, key: str) -> str:
    """Rewrite `key: [{...}, {...}]` into yaml's block-sequence spelling."""

    prefix = f"{key}: ["
    line = next(one for one in text.splitlines() if one.startswith(prefix))
    items = [f"{{{part}}}" for part in line[len(prefix) : -1].strip("{}").split("}, {")]
    return text.replace(line, f"{key}:" + "".join(f"\n- {item}" for item in items))


def test_a_block_sequence_parses_to_the_same_thing_as_flow() -> None:
    """A stage may spell a list either way; both carry the same data.

    The templates show one-line flow, but a stage reformats for readability
    and hands back yaml's block form instead. Refusing that cost a measured
    smoke round its verify stage *after* the edit had already landed, so the
    codec takes both spellings and re-renders the canonical one.
    """

    flow = render_anchor(_anchor())
    block = _to_block(flow, "hits")
    assert "hits:\n-" in block  # the rewrite landed
    assert parse_anchor(block) == parse_anchor(flow)
    assert "hits: [" in render_anchor(parse_anchor(block))  # canonical form restored


def test_a_sequence_item_with_no_key_above_it_is_refused() -> None:
    flow = render_anchor(_anchor())
    orphan = next(one for one in flow.splitlines() if one.startswith("hits: ["))
    with pytest.raises(ValueError, match="no key above it"):
        parse_anchor(flow.replace(orphan, '- {term: "orphan"}'))


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_a_missing_required_field_is_refused(key) -> None:
    text = render_intent(_intent())
    stripped = "\n".join(
        line for line in text.splitlines() if not line.startswith(f"{key}:")
    )
    with pytest.raises(ValueError, match=key):
        parse_intent(stripped)


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_an_empty_required_field_is_refused_on_both_sides(key) -> None:
    text = render_intent(_intent()).replace(f"{key}: ", f"{key}: ", 1)
    blanked = "\n".join(
        f"{key}:" if line.startswith(f"{key}:") else line for line in text.splitlines()
    )
    with pytest.raises(ValueError, match=key):
        parse_intent(blanked)

    required = Required(**{name: ("" if name == key else "none") for name in REQUIRED_KEYS})
    with pytest.raises(ValueError, match=key):
        render_intent(
            Intent(
                mr_iid=11,
                project_id="1009",
                round=1,
                base_sha="7c41d0e",
                opinions=(),
                required=required,
            )
        )


def test_frontmatter_count_disagreeing_with_the_blocks_is_refused() -> None:
    text = render_intent(_intent()).replace("opinions: 1", "opinions: 2")
    with pytest.raises(ValueError, match="opinions"):
        parse_intent(text)


def test_an_unknown_op_kind_is_refused() -> None:
    """`op` is a closed enum — a fourth kind fails the stage, not the data."""

    text = render_intent(_intent()).replace("op: modify", "op: tweak")
    with pytest.raises(ValueError, match="op"):
        parse_intent(text)


def test_quoted_values_keep_their_commas_and_colons() -> None:
    original = _anchor()
    parsed = parse_anchor(render_anchor(original))
    assert parsed.entries[0].hits[1].term == "요구 버전: 3.2"


def test_gate_literal_blocks_survive_two_checks_on_one_unit() -> None:
    """A dict keyed on unit_id would drop one; the parser keeps a list."""

    gate = _gate()
    doubled = Gate(
        **{
            **gate.__dict__,
            "literals": (gate.literals[0], gate.literals[0]),
        }
    )
    assert len(parse_gate(render_gate(doubled)).literals) == 2


def test_plain_text_check_records_sets_as_null() -> None:
    text = render_gate(_gate())
    assert "sets: null" in text
    assert parse_gate(text).literals[1].sets is None


def test_occurrence_count_is_declared_unavailable() -> None:
    for check in parse_gate(render_gate(_gate())).literals:
        assert check.occurrences == OCCURRENCES_UNAVAILABLE


def test_verify_narrative_survives_the_trip_and_3b_is_never_quoted() -> None:
    text = render_verify(_verify())
    assert "**판정문**" in text
    assert "30-edit" not in text
    parsed = parse_verify(text)
    assert parsed.verdicts[0].판정문.startswith("의견 원문이 요구한")
    assert parsed.verdicts[1].판정문 == ""
    assert parsed.summary == "설치 절차의 요구 버전 표기를 3.2 에서 4.0 으로 바꿨다."


def test_summary_slots_render_as_markdown_and_empty_becomes_none() -> None:
    empty = Summary(
        mr_iid=11,
        round=1,
        applied=(),
        partial=(),
        unapplied=(),
        commit_paths=(),
        summary="",
        key_changes=(),
        points_to_watch=(),
        required=_required(),
    )
    text = render_summary(empty)
    assert "## key_changes\n\n(none)" in text
    assert parse_summary(text) == empty


def test_summary_duck_types_the_existing_report_renderer() -> None:
    html = render_review_report({"iid": 11, "title": "t"}, _summary(), None)
    assert "요구 버전: 3.2 → 4.0" in html
    assert "[unapplied] 의견 42" in html


def test_templates_come_from_the_renderers() -> None:
    intent = intent_template(mr_iid=11, project_id="1009", round=1, base_sha="abc1234")
    edit = edit_template(mr_iid=11, round=1)
    verify = verify_template(mr_iid=11, round=1)
    for template in (intent, edit, verify):
        for key in REQUIRED_KEYS:
            assert f"{key}:" in template
        assert "```yaml" in template
        for line in template.splitlines():
            assert not line.lstrip().startswith("- {")
    assert "search_terms: [" in intent
    assert "selected: [{" in edit
    assert "**판정문**" in verify
    assert "## SUMMARY" in verify


def test_templates_carry_real_identity_not_a_placeholder() -> None:
    """The stage must not be asked for ids Python already has.

    A placeholder here was answered wrong twice under measurement: 3b wrote
    the project id into `mr_iid`, and 3c copied `<mr_iid>` through verbatim
    and lost its stage to the int coercion.
    """

    for template in (
        intent_template(mr_iid=11, project_id="1009", round=1, base_sha="abc1234"),
        edit_template(mr_iid=11, round=1),
        verify_template(mr_iid=11, round=1),
    ):
        assert "<mr_iid>" not in template
        assert "<round>" not in template
        assert 'mr_iid: 11' in template or 'mr_iid: "11"' in template


def test_join_ids_never_renders_an_empty_value() -> None:
    assert join_ids(()) == "none"
    assert join_ids([41, 42]) == "41, 42"
