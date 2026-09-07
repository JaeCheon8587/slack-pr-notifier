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


def test_rewritten_words_with_identical_literals_are_textual() -> None:
    """리터럴 집합이 같고 문장만 고친 줄 — 값 원자 없이 표현 원자 1쌍."""

    literals = _pipeline(
        "# 설정\n타임아웃은 30초로 설정한다\n",
        "# 설정\n타임아웃을 30초로 지정한다\n",
    )
    unit = literals.units[0]
    assert unit.changed == () and unit.added == () and unit.removed == ()
    assert unit.textual == (
        ("타임아웃은 30초로 설정한다", "타임아웃을 30초로 지정한다"),
    )


def test_mixed_run_keeps_textual_atom_next_to_value_change() -> None:
    """값 변경과 표현 변경이 한 run 에 묶여도 표현 원자는 소실되지 않는다.

    빈 줄 삭제로 두 변경이 인접해 하나의 replace run 이 되면, 런 전체의
    리터럴 집합은 다르다 — 예전엔 run 을 통째로 건너뛰어 '신뢰도 → 신뢰성'
    이 사라졌다. 값은 런 단위 diff 가, 표현은 같은 집합의 줄짝이 가져간다.
    """

    literals = _pipeline(
        "# 설치\n"
        "winget install --id Python.Python.3.11 -e\n"
        "\n"
        "문서의 신뢰도를 높인다\n",
        "# 설치\n"
        "winget install --id Python.Python.3.12 -e\n"
        "문서의 신뢰성을 높인다\n",
    )
    unit = literals.units[0]
    changed = {(c.key, c.from_value, c.to_value) for c in unit.changed}
    assert ("Python.Python", "3.11", "3.12") in changed
    assert unit.textual == (("문서의 신뢰도를 높인다", "문서의 신뢰성을 높인다"),)


def test_unchanged_duplicate_value_elsewhere_keeps_change_paired() -> None:
    """같은 키의 변하지 않은 값이 섹션에 또 있어도 짝이 added 로 새지 않는다.

    다이어그램 표기의 '2회' 는 equal 줄이라 원자 후보가 못 된다. 예전의
    섹션 전체 diff 는 이 값이 재시도 줄의 '2회 → 3회' 짝을 삼켜 added[3]
    을 만들었다 — 값 diff 는 이제 replace run 단위로 묶인다.
    """

    literals = _pipeline(
        "# 흐름\n"
        "| 다이어그램 | 흐름을 2회 반복 표기 |\n"
        "\n"
        "검증 실패 시 재호출 최대 2회\n",
        "# 흐름\n"
        "| 다이어그램 | 흐름을 2회 반복 표기 |\n"
        "\n"
        "검증 실패 시 재호출 최대 3회\n",
    )
    unit = literals.units[0]
    changed = {(c.key, c.from_value, c.to_value) for c in unit.changed}
    assert ("회", "2", "3") in changed
    assert unit.added == () and unit.removed == ()


def test_literal_free_lines_become_prose_runs() -> None:
    """값 없는 문장의 추가/삭제 — 값 원자와 겹치지 않는 의미 원자."""

    # equal 줄을 앵커로 삼아야 insert/delete 가 각각 열린다 — 인접하면
    # 하나의 replace 로 병합되어 prose 원자가 사라진다.
    literals = _pipeline(
        "# 설정\n지워질 문장입니다\n기존 안내 문장입니다\n",
        "# 설정\n기존 안내 문장입니다\n새로운 안내 문장입니다\n",
    )
    unit = literals.units[0]
    assert unit.prose_added == ("새로운 안내 문장입니다",)
    assert unit.prose_removed == ("지워질 문장입니다",)
    assert unit.textual == ()


def test_atom_fields_survive_the_round_trip() -> None:
    literals = _pipeline(
        "# 설정\n지워질 문장입니다\n동일한 첫 문장입니다\n"
        "타임아웃은 30초로 설정한다\n동일 문장입니다\n",
        "# 설정\n동일한 첫 문장입니다\n타임아웃을 30초로 지정한다\n"
        "동일 문장입니다\n새로운 안내 문장입니다\n",
    )
    unit = literals.units[0]
    assert unit.textual and unit.prose_added and unit.prose_removed
    again = parse_literals(render_literals(literals))
    assert again.units[0].textual == unit.textual
    assert again.units[0].prose_added == unit.prose_added
    assert again.units[0].prose_removed == unit.prose_removed


def _kinds(text: str) -> set[tuple[str, str, str]]:
    return {(lit.kind, lit.key, lit.value) for lit in extract_literals(text)}


def test_version_extractor_keys_on_the_token_it_is_glued_to() -> None:
    found = _kinds("winget install --id Python.Python.3.11 -e")
    assert ("version", "Python.Python", "3.11") in found
    assert ("version", "python@", "3.12") in _kinds("brew install python@3.12")


def test_standalone_version_keys_on_version() -> None:
    """'3.12.4' and '3.12.7' must group even when the sentence is rewritten."""

    base = _kinds("권장 버전은 3.12 이상 (검증 기준 3.12.4). 최소 3.8+에서 동작한다")
    head = _kinds("권장 버전은 3.12 이상 (검증 기준 3.12.7). 최소 지원은 3.10+이다")
    assert ("version", "version", "3.12.4") in base
    assert ("version", "version", "3.8+") in base
    assert ("version", "version", "3.12.7") in head
    assert ("version", "version", "3.10+") in head


def test_version_never_double_counts_a_number_unit() -> None:
    found = _kinds("타임아웃 1.5 초")
    assert ("unit", "초", "1.5") in found
    assert not any(kind == "version" for kind, _, _ in found)


def test_code_span_stays_inside_one_line() -> None:
    text = "앞줄 " + _T + "TIMEOUT=30" + _T + " 뒤\n다음 줄 " + _T + "PATH" + _T + " 끝\n"
    spans = {value for kind, _, value in _kinds(text) if kind == "code"}
    assert spans == {"TIMEOUT=30", "PATH"}
    assert not any("\n" in value for value in spans)


def test_fenced_block_is_extracted_line_by_line() -> None:
    text = (
        "본문\n"
        + _T * 3
        + "powershell\n"
        + "winget install --id Python.Python.3.11 -e\n"
        + "타임아웃: 30초\n"
        + _T * 3
        + "\n"
    )
    found = _kinds(text)
    assert ("version", "Python.Python", "3.11") in found
    assert ("kv", "타임아웃", "30초") in found
    # the info string is a delimiter, never a value; no whole-block literal
    assert not any(key == "powershell" for _, key, _ in found)
    assert not any("\n" in value for _, _, value in found)


def test_changed_pairs_leftovers_in_document_order() -> None:
    """The measured expectation: 3.12.4→3.12.7 and 3.8+→3.10+, not crossed."""

    literals = _pipeline(
        "# 요구사항\n- Python 3.12 이상 (검증 기준 3.12.4). 최소 3.8+에서 동작한다.\n",
        "# 요구사항\n- Python 3.12 이상 (검증 기준 3.12.7). 최소 지원은 3.10+이다.\n",
    )
    changed = {(c.from_value, c.to_value) for c in literals.units[0].changed}
    assert ("3.12.4", "3.12.7") in changed
    assert ("3.8+", "3.10+") in changed


def test_density_average_and_roundtrip() -> None:
    literals = _pipeline(
        "# 설정\n타임아웃: 30초, 재시도 5회, verbose true\n",
        "# 구성\n타임아웃: 60초, 재시도 10회, verbose false\n",
    )
    assert average_per_unit(literals) >= 3  # footer gate: keep design as-is
    assert parse_literals(render_literals(literals)) == literals
