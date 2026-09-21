"""Tests for app/mrdoc/scatter.py — 라인 폭 × 원자 수 산점도.

x는 유닛이 실제로 걸친 라인 폭(07 excerpt 양쪽 범위 중 큰 쪽), y는 원자
수 — 두 축 모두 실측값이라 점의 위치가 곧 변경의 성격이다. 점선 대각선은
원자=폭의 최대 밀도선, 모양은 연산(추가▲/삭제▼/변경●), 색은 첫 축, 금
테두리는 FIX 지목. 산문과 라벨에 u- 해시는 못 온다.
"""

from __future__ import annotations

import re

from app.mrdoc.collect import Collect, CollectedFile, CollectedUnit
from app.mrdoc.literals import ChangedValue
from app.mrdoc.scatter import (
    distribution_html,
    dot_plot,
    heatmap,
    interpret,
    scatter_rows,
)


def _unit(
    uid: str,
    *,
    kind: str = "changed",
    axis: str = "표현",
    span: int = 3,
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    changed: tuple[ChangedValue, ...] = (),
    textual: tuple[tuple[str, str], ...] = (),
    prose_added: tuple[str, ...] = (),
    prose_removed: tuple[str, ...] = (),
    section: str = "개요",
    file: str = "f-01",
) -> CollectedUnit:
    return CollectedUnit(
        unit_id=uid,
        kind=kind,
        structure_kind="none",
        klass=axis,
        section=section,
        file=file,
        removed=removed,
        added=added,
        changed=changed,
        excerpt_ref=uid,
        explanation="값이 바뀌었다.",
        axes=(axis,),
        ops=(),
        textual=textual,
        prose_added=prose_added,
        prose_removed=prose_removed,
        span=span,
    )


def _matrix(
    f01: dict[str, dict[str, int]] | None = None,
) -> dict[str, dict[str, dict[str, int]]]:
    zero = {"추가": 0, "삭제": 0, "변경": 0}
    base = {axis: dict(zero) for axis in ("구조", "의미", "표현")}
    if f01:
        base.update(f01)
    return {"f-01": base}


def _collect(
    units: tuple[CollectedUnit, ...],
    *,
    matrix: dict[str, dict[str, dict[str, int]]] | None = None,
    fix_targets: tuple[str, ...] = (),
) -> Collect:
    return Collect(
        mr_iid=18,
        files={"modified": 1, "added": 0, "deleted": 0, "moved_sections": 0},
        matrix=matrix or _matrix(),
        classes={},
        ops={},
        value_changes=0,
        verify={},
        confidence_dist={},
        uncertain=(),
        failed_files=(),
        refs_dropped=0,
        file_blocks=(
            CollectedFile(
                file_id="f-01",
                path="docs/p-a/setup.md",
                product_dir="docs/p-a",
                units=len(units),
                summary="요약",
            ),
        ),
        units=units,
        fix_targets=fix_targets,
    )


def test_row_x_is_line_span_and_y_is_total_atoms() -> None:
    """x는 라인 폭(실측), y는 부호 없는 원자 수 — 방향은 모양이 말한다."""

    units = (
        _unit("u-11111111", kind="added", span=5, added=("a", "b"), prose_added=("c",)),
        _unit("u-22222222", kind="removed", span=11, removed=("x", "y")),
        _unit(
            "u-33333333",
            kind="changed",
            span=3,
            changed=(ChangedValue("만료", "60", "30"),),
            textual=(("설치 한", "설치한"),),
        ),
    )
    rows = scatter_rows(_collect(units), units)
    assert [row.x for row in rows] == [5, 11, 3]
    assert [row.y for row in rows] == [3, 2, 2]
    assert [row.kind for row in rows] == ["added", "removed", "changed"]


def test_label_speaks_file_section_axis_without_hash() -> None:
    units = (_unit("u-abcdef12", kind="added", added=("a",), section="정책"),)
    rows = scatter_rows(_collect(units), units)
    label = rows[0].label
    assert "setup.md" in label
    assert "정책" in label
    assert "표현" in label
    assert not re.search(r"u-[0-9a-f]{8}", label)


def test_interpret_counts_size_buckets_and_names_the_biggest() -> None:
    units = (
        _unit("u-00000001", kind="added", span=3, added=("a",), section="서두"),
        _unit("u-00000002", kind="added", span=3, added=("a",), section="서두"),
        _unit("u-00000003", kind="added", span=6, added=("a", "b", "c"), section="용어집"),
        _unit(
            "u-00000005",
            kind="changed",
            span=12,
            changed=(ChangedValue("a", "1", "2"),),
            section="아키텍처",
        ),
        _unit(
            "u-00000004",
            kind="removed",
            span=11,
            removed=("a",) * 5,
            section="1차 스코프 테마",
        ),
    )
    rows = scatter_rows(_collect(units, fix_targets=("u-00000004",)), units)
    text = interpret(rows)
    assert "유닛 5개" in text
    assert "잔 변경 3" in text
    assert "넓은 범위 1" in text
    assert "대형 수술 1" in text
    assert "1차 스코프 테마" in text
    assert "폭 11" in text
    assert "원자 5" in text
    assert "fix 재작성 1개" in text
    assert not re.search(r"u-[0-9a-f]{8}", text)


def test_dot_plot_draws_diagonal_shapes_opacity_and_hides_hashes() -> None:
    units = (
        _unit("u-00000001", kind="added", span=5, added=("a", "b", "c")),
        _unit("u-00000002", kind="removed", span=11, removed=("a",) * 5),
        _unit("u-00000003", kind="changed", span=3, changed=(ChangedValue("a", "1", "2"),)),
    )
    svg = dot_plot(scatter_rows(_collect(units), units))
    assert svg.count("<svg") == 1
    assert 'class="diag"' in svg  # 최대 밀도선
    assert "polygon" in svg  # 추가▲ 삭제▼
    assert "<circle" in svg  # 변경●
    assert "fill-opacity" in svg  # 겹침은 농도로
    assert "라인 폭" in svg and "원자 수" in svg
    assert not re.search(r"u-[0-9a-f]{8}", svg)


def test_same_coordinates_spread_into_a_cloud() -> None:
    """같은 좌표의 유닛은 결정론 지터로 흩어져 뭉침이 보인다."""

    units = tuple(
        _unit(f"u-{i:08d}", kind="changed", span=3, changed=(ChangedValue("a", "1", "2"),))
        for i in range(4)
    )
    svg = dot_plot(scatter_rows(_collect(units), units))
    dots = re.findall(r'<circle class="dot[^"]*" cx="([\d.]+)" cy="([\d.]+)"', svg)
    assert len(dots) == 4
    assert len(set(dots)) >= 3


def test_dot_plot_axis_colors_fix_ring_and_big_unit_labels() -> None:
    units = (
        _unit(
            "u-00000001",
            axis="구조",
            kind="added",
            span=8,
            added=("a",) * 5,
            section="운영 원칙",
        ),
        _unit(
            "u-00000002",
            axis="의미",
            kind="changed",
            span=3,
            changed=(ChangedValue("만료", "60", "30"),),
        ),
        _unit("u-00000003", axis="표현", kind="changed", span=3, textual=(("한", "한"),)),
        _unit("u-00000004", axis="미분류", kind="added", span=3, added=("a",)),
    )
    svg = dot_plot(scatter_rows(_collect(units, fix_targets=("u-00000002",)), units))
    for color in ("#4c72b0", "#dd8452", "#55a868", "#8c8c8c"):
        assert color in svg
    assert svg.count('class="dot fixed"') == 1
    assert "운영 원칙" in svg  # 큰 유닛은 라벨이 붙는다


def test_heatmap_sums_matrix_cells_per_axis() -> None:
    units = (_unit("u-00000001", kind="changed", changed=(ChangedValue("a", "1", "2"),)),)
    collect = _collect(
        units,
        matrix=_matrix(
            {"의미": {"추가": 0, "삭제": 0, "변경": 3}, "표현": {"추가": 2, "삭제": 0, "변경": 0}}
        ),
    )
    out = heatmap(collect)
    assert "setup.md" in out
    assert "의미" in out and "표현" in out and "구조" in out
    assert ">3<" in out
    assert ">2<" in out
    assert ">0<" in out


def test_distribution_html_requires_units() -> None:
    assert distribution_html(_collect(()), ()) == ""
    units = (
        _unit("u-00000001", kind="added", added=("a",)),
        _unit("u-00000002", kind="removed", removed=("b",)),
    )
    out = distribution_html(_collect(units), units)
    assert "유닛 분포" in out
    assert "<svg" in out
    assert "파일 × 축 밀도" in out
