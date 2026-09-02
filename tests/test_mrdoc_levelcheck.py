"""Tests for app/mrdoc/levelcheck.py — the four level rules + round-trip."""

from __future__ import annotations

from app.mrdoc.analysis import AnalysisUnit, FileAnalysis
from app.mrdoc.levelcheck import build_levelcheck, parse_levelcheck, render_levelcheck
from app.mrdoc.literals import ChangedValue, Literals, UnitLiterals


def _analysis(levels: dict[str, str]) -> list[FileAnalysis]:
    units = tuple(
        AnalysisUnit(unit_id=uid, section_id="s-x", level=level, before="b", after="a")
        for uid, level in levels.items()
    )
    return [FileAnalysis(file_id="f", path="docs/a.md", units=units)]


def _literals(rows: dict[str, tuple[int, int, int]]) -> Literals:
    units = tuple(
        UnitLiterals(
            unit_id=uid,
            section_id="s-x",
            removed=tuple(f"removed{i}" for i in range(removed)),
            added=tuple(f"added{i}" for i in range(added)),
            changed=tuple(
                ChangedValue(f"key{i}", "1", "2") for i in range(changed)
            ),
        )
        for uid, (removed, added, changed) in rows.items()
    )
    return Literals(units=units)


def test_l3_survives_only_with_empty_diffs() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-keep": (0, 0, 0), "u-move": (0, 0, 1)}),
        _analysis({"u-keep": "L3", "u-move": "L3"}),
    )
    by_id = {row.unit_id: row for row in check.units}
    assert by_id["u-keep"].verified == "L3"
    assert not by_id["u-keep"].promoted
    assert by_id["u-move"].verified == "L2"
    assert by_id["u-move"].promoted
    assert check.promoted == 1


def test_l1_never_demoted() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-swap": (1, 1, 0), "u-flat": (0, 0, 0)}),
        _analysis({"u-swap": "L1", "u-flat": "L1"}),
    )
    by_id = {row.unit_id: row for row in check.units}
    assert by_id["u-swap"].verified == "L1"
    assert "사실 교체" in by_id["u-swap"].basis
    assert not by_id["u-swap"].promoted
    assert by_id["u-flat"].verified == "L1"
    assert by_id["u-flat"].warning  # no literal evidence -> verifier audit flag


def test_l2_stays_l2_on_any_diff() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-x": (1, 0, 0)}),
        _analysis({"u-x": "L2"}),
    )
    assert check.units[0].verified == "L2"
    assert not check.units[0].promoted


def test_unclaimed_unit_defaults_to_l2() -> None:
    check = build_levelcheck(17, _literals({"u-x": (0, 1, 0)}), [])
    assert check.units[0].claimed == "L2"
    assert check.units[0].verified == "L2"


def test_render_parse_round_trip() -> None:
    check = build_levelcheck(
        17,
        _literals({"u-a": (0, 0, 1), "u-b": (1, 1, 0)}),
        _analysis({"u-a": "L3", "u-b": "L1"}),
    )
    parsed = parse_levelcheck(render_levelcheck(check))
    assert parsed.mr_iid == 17
    assert [row.unit_id for row in parsed.units] == [row.unit_id for row in check.units]
    for original, back in zip(check.units, parsed.units, strict=True):
        assert back.claimed == original.claimed
        assert back.verified == original.verified
        assert back.promoted == original.promoted
        assert back.warning == original.warning
    assert parsed.verified_level("u-b") == "L1"
