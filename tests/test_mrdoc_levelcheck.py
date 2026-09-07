"""Tests for app/mrdoc/levelcheck.py — property rules, vocab gate, round-trip.

Three failure modes this file pins down: 표현 surviving a non-empty literal
diff (a value change would vanish from the report), an unclassified unit
being defaulted to 의미(값) (the measured run printed a level nobody
determined), and the vocab gate drifting away from the analyzer's prompt.
"""

from __future__ import annotations

from app.mrdoc.analysis import AnalysisUnit, FileAnalysis
from app.mrdoc.levelcheck import (
    build_levelcheck,
    class_counts,
    parse_levelcheck,
    render_levelcheck,
)
from app.mrdoc.literals import ChangedValue, Literals, UnitLiterals
from app.mrdoc.structure import ChangeUnit, Structure


def _analysis(
    claims: dict[str, str], explanations: dict[str, str] | None = None, summary: str = ""
) -> list[FileAnalysis]:
    units = tuple(
        AnalysisUnit(
            unit_id=uid,
            section_id="s-x",
            klass=klass,
            explanation=(explanations or {}).get(uid, "값이 바뀌었다."),
        )
        for uid, klass in claims.items()
    )
    return [FileAnalysis(file_id="f", path="docs/a.md", units=units, summary=summary)]


def _literals(rows: dict[str, tuple[int, int, int]]) -> Literals:
    units = tuple(
        UnitLiterals(
            unit_id=uid,
            section_id="s-x",
            removed=tuple(f"removed{i}" for i in range(removed)),
            added=tuple(f"added{i}" for i in range(added)),
            changed=tuple(ChangedValue(f"key{i}", "1", "2") for i in range(changed)),
        )
        for uid, (removed, added, changed) in rows.items()
    )
    return Literals(units=units)


def _structure(kinds: dict[str, str]) -> Structure:
    return Structure(
        tree=(),
        base_tree=(),
        changed=tuple(
            ChangeUnit(
                unit_id=uid,
                section_id="s-x",
                file_id="f",
                old_lines=(1, 2),
                new_lines=(1, 2),
                structure_kind=kind,
            )
            for uid, kind in kinds.items()
        ),
        moved=(),
    )


def test_expression_survives_only_with_empty_diffs() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-keep": (0, 0, 0), "u-move": (0, 0, 1)}),
        _analysis({"u-keep": "표현", "u-move": "표현"}),
    )
    by_id = {row.unit_id: row for row in check.units}
    assert by_id["u-keep"].verified == "L3"
    assert not by_id["u-keep"].promoted
    assert by_id["u-move"].verified == "L2"  # promoted — 표현 불성립
    assert by_id["u-move"].promoted
    assert check.promoted == 1


def test_meaning_claim_without_literals_keeps_its_claim_and_warns() -> None:
    check = build_levelcheck(
        17, _literals({"u-flat": (0, 0, 0)}), _analysis({"u-flat": "의미"})
    )
    row = check.units[0]
    assert row.verified == "L1"  # 의미(맥락) — 강등 없음
    assert row.warning  # no literal evidence -> verifier audit flag
    assert row.classes == ("의미",)


def test_unclaimed_unit_without_diffs_is_unclassified() -> None:
    """The measured run defaulted this to L2 and printed an invented level."""

    check = build_levelcheck(17, _literals({"u-x": (0, 0, 0)}), [])
    row = check.units[0]
    assert row.claimed == ""
    assert row.verified == "L0"
    assert row.classes == ("미분류",)


def test_unclaimed_unit_with_diffs_is_meaning_value() -> None:
    check = build_levelcheck(17, _literals({"u-x": (0, 1, 0)}), [])
    assert check.units[0].verified == "L2"
    assert not check.units[0].promoted


def test_added_unit_is_tagged_structure_on_top_of_its_body() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-new": (0, 2, 0)}),
        [],
        _structure({"u-new": "added"}),
    )
    assert check.units[0].classes == ("구조", "의미")
    assert class_counts(check) == {"구조": 1, "의미": 1, "표현": 0, "미분류": 0}


def test_vocab_gate_flags_explanation_and_file_summary() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-x": (0, 0, 1)}),
        _analysis(
            {"u-x": ""},
            explanations={"u-x": "값이 상향되어 문서가 개선되었다."},
            summary="버전이 바뀐 배경은 릴리스 주기 때문이다.",
        ),
    )
    assert check.units[0].vocab_violation == ("개선", "상향")
    assert check.summaries[0].file_id == "f"
    assert set(check.summaries[0].vocab_violation) == {"때문", "배경"}
    assert check.vocab_violations == 2


def test_clean_explanation_trips_nothing() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-x": (0, 0, 1)}),
        _analysis({"u-x": ""}, explanations={"u-x": "검증 기준이 3.12.4 에서 3.12.7 로 바뀌었다."}),
    )
    assert check.units[0].vocab_violation == ()
    assert check.vocab_violations == 0


def test_render_parse_round_trip() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-a": (0, 0, 1), "u-b": (0, 0, 0), "u-c": (0, 0, 0)}),
        _analysis(
            {"u-a": "표현", "u-b": "의미"},
            explanations={"u-a": "값이 상향되었다."},
            summary="영향이 크다.",
        ),
        _structure({"u-c": "removed"}),
    )
    parsed = parse_levelcheck(render_levelcheck(check))
    assert parsed == check
    assert parsed.verified_level("u-a") == "L2"
    assert parsed.verified_level("u-c") == "L0"


def test_artifact_prints_design_labels_not_internal_codes() -> None:
    check = build_levelcheck(
        17, _literals({"u-a": (0, 0, 1), "u-b": (0, 0, 0)}), _analysis({"u-a": "표현"})
    )
    text = render_levelcheck(check)
    assert "의미(값)" in text and "미분류" in text
    assert "L1" not in text and "L2" not in text and "L3" not in text
    assert "classes: {구조: 0, 의미: 1, 표현: 0, 미분류: 1}" in text
