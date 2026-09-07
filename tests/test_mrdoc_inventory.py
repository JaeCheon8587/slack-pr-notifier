"""Tests for app/mrdoc/inventory.py — atoms, not unit tags, carry the counts.

The !22 failure: one unit held a heading rename plus four value edits and
counted once under one axis. These tests pin the other behavior — every
atom counts as itself, mixed units light every column they touch, and a
unit no atom covers stays out of the matrix.
"""

from __future__ import annotations

from app.mrdoc import classes
from app.mrdoc.changeset import build_changeset
from app.mrdoc.inventory import build_inventory
from app.mrdoc.literals import Literals, UnitLiterals, build_literals
from app.mrdoc.structure import ChangeUnit, Structure, build_structure


def _changeset():
    return build_changeset(
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
                "patch": "@@ -1,20 +1,22 @@",
            }
        ],
    )


def _pipeline(base_text: str, head_text: str):
    base = {"docs/a.md": base_text}
    head = {"docs/a.md": head_text}
    cs = _changeset()
    structure = build_structure(base, head, cs)
    return structure, build_literals(structure, cs, base, head), cs


def test_one_unit_lights_every_column_it_touches() -> None:
    """값 변화 없이 표현 원자 + 문장 추가가 같은 유닛에 섞여도 각자 센다."""

    structure, literals, cs = _pipeline(
        "# 설정\n타임아웃은 30초로 설정한다\n동일 문장입니다\n",
        "# 설정\n타임아웃을 30초로 지정한다\n동일 문장입니다\n추가 안내 문장입니다\n",
    )
    inventory = build_inventory(structure, literals, cs)
    unit = structure.changed[0]
    assert inventory.unit_axes[unit.unit_id] == (classes.MEANING, classes.EXPRESSION)
    assert inventory.unit_ops[unit.unit_id] == ("추가", "변경")
    fid = unit.file_id
    assert inventory.matrix_by_file[fid][classes.MEANING]["추가"] == 1
    assert inventory.matrix_by_file[fid][classes.EXPRESSION]["변경"] == 1


def test_added_section_counts_its_heading_not_its_values() -> None:
    """신규 절의 값은 정보일 뿐 — 원자는 헤딩 추가 하나뿐이다."""

    structure, literals, cs = _pipeline(
        "# 개요\n본문입니다\n",
        "# 개요\n본문입니다\n# 신규\n한도 100개\n",
    )
    inventory = build_inventory(structure, literals, cs)
    assert inventory.axis_counts == {
        classes.STRUCTURE: 1,
        classes.MEANING: 0,
        classes.EXPRESSION: 0,
        classes.UNCLASSIFIED: 0,
    }
    assert all(
        atom.kind == "heading" for atom in inventory.atoms
    )


def test_atom_free_unit_is_unclassified_and_never_matrixed() -> None:
    cs = _changeset()
    fid = cs.files[0].fid
    unit = ChangeUnit("u-atomless", "docs/a.md#s", fid, (1, 2), (1, 2), "none")
    structure = Structure((), (), (unit,), ())
    literals = Literals((UnitLiterals("u-atomless", "docs/a.md#s", (), (), ()),))
    inventory = build_inventory(structure, literals, cs)
    assert inventory.axis_counts[classes.UNCLASSIFIED] == 1
    assert inventory.unit_axes["u-atomless"] == (classes.UNCLASSIFIED,)
    assert inventory.matrix_by_file[fid] == {
        axis: {"추가": 0, "삭제": 0, "변경": 0}
        for axis in (classes.STRUCTURE, classes.MEANING, classes.EXPRESSION)
    }


def test_mr22_shaped_mix_counts_every_atom() -> None:
    """!22 의 형태: 유닛 태그가 아니라 원자 합계가 개요를 쓴다."""

    base = {
        "docs/a.md": "\n".join(
            [
                "# 개요",
                "제품 소개 문장입니다.",
                "# 설정",
                "타임아웃 30초",
                "공통 문장입니다",
                "# 버전",
                "권장 버전은 3.12.4 이다",
                "최소 버전은 3.8+ 이다",
                "동일한 안내 문장입니다",
                "버전은 위와 같이 안내합니다",
                "공통 안내입니다",
                "구버전 2.7 지원은 종료되었다",
                "# 제거",
                "기한 30일",
                "남을 문장입니다",
                "삭제될 문장입니다",
                "참조는 [안내](https://old.example.com) 로 한다",
                "# 표현",
                "대기 시간 5초",
            ]
            + [""]  # trailing newline
        )
    }
    head = {
        "docs/a.md": "\n".join(
            [
                "# 개요",
                "제품 소개 문장입니다.",
                "# 설정",
                "타임아웃 60초",
                "공통 문장입니다",
                "한도 100개",
                "경고 3회",
                "대기 10초",
                "새 문장 안내입니다",
                "# 버전",
                "권장 버전은 3.12.7 이다",
                "최소 버전은 3.10+ 이다",
                "동일한 안내 문장입니다",
                "버전은 다음과 같이 안내합니다",
                "공통 안내입니다",
                "# 제거",
                "기한 60일",
                "남을 문장입니다",
                "# 표현",
                "대기 시간 10초",
                "# 부록",
                "새로운 섹션입니다",
                "# 참고",
                "또 다른 섹션입니다",
            ]
            + [""]
        )
    }
    cs = _changeset()
    structure = build_structure(base, head, cs)
    literals = build_literals(structure, cs, base, head)
    inventory = build_inventory(structure, literals, cs)
    assert inventory.axis_counts == {
        classes.STRUCTURE: 2,
        classes.MEANING: 12,
        classes.EXPRESSION: 1,
        classes.UNCLASSIFIED: 0,
    }
    assert inventory.op_counts == {"추가": 6, "삭제": 3, "변경": 6}
    assert inventory.value_changes == 5
