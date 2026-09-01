"""Tests for app/mrdoc/literals.py — extractors, diff rules, density."""

from __future__ import annotations

from app.mrdoc.changeset import build_changeset
from app.mrdoc.literals import (
    average_per_unit,
    build_literals,
    extract_literals,
    parse_literals,
    render_literals,
)
from app.mrdoc.structure import build_structure

_T = chr(96)


def test_extractors_cover_all_five_kinds() -> None:
    text = (
        "타임아웃: 30초\n"
        "재시도 5회, 버퍼 64KB\n"
        "verbose true\n"
        "명령은 " + _T + "TIMEOUT=30" + _T + " 로 설정\n"
        "[가이드](https://example.com/guide) 참고\n"
    )
    found = extract_literals(text)
    kinds = {lit.kind for lit in found}
    assert kinds == {"kv", "unit", "bool", "code", "link"}
    assert ("unit", "초", "30") in [(lit.kind, lit.key, lit.value) for lit in found]
    assert ("link", "가이드", "https://example.com/guide") in [
        (lit.kind, lit.key, lit.value) for lit in found
    ]


def _pipeline(base_text: str, head_text: str):
    base = {"docs/a.md": base_text}
    head = {"docs/a.md": head_text}
    cs = build_changeset(
        mr_iid=1,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=[
            {
                "filename": "docs/a.md",
                "previous_filename": None,
                "status": "modified",
                "patch": "@@ -1,3 +1,3 @@",
            }
        ],
    )
    structure = build_structure(base, head, cs)
    return build_literals(structure, cs, base, head)


def test_changed_rule_moves_same_key_value_pairs() -> None:
    literals = _pipeline(
        "# 설정\n타임아웃: 30초, 최대 5회\n", "# 구성\n타임아웃: 60초, 최대 10회\n"
    )
    unit = literals.units[0]
    changed = {(c.key, c.from_value, c.to_value) for c in unit.changed}
    assert ("초", "30", "60") in changed
    assert ("회", "5", "10") in changed
    assert ("타임아웃", "30초, 최대 5회", "60초, 최대 10회") in changed
    assert unit.removed == () and unit.added == ()


def test_pure_addition_lands_in_added() -> None:
    literals = _pipeline("# 설정\n기존 내용\n", "# 설정\n기존 내용, 한도 100개 추가\n")
    unit = literals.units[0]
    found = extract_literals("한도 100개")
    assert ("unit", "개", "100") in [(lit.kind, lit.key, lit.value) for lit in found]
    assert "100" in unit.added


def test_density_average_and_roundtrip() -> None:
    literals = _pipeline(
        "# 설정\n타임아웃: 30초, 재시도 5회, verbose true\n",
        "# 구성\n타임아웃: 60초, 재시도 10회, verbose false\n",
    )
    assert average_per_unit(literals) >= 3  # footer gate: keep design as-is
    assert parse_literals(render_literals(literals)) == literals
