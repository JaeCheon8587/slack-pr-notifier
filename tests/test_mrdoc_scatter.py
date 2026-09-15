"""Tests for app/mrdoc/scatter.py — 단일 리포트 안의 유닛 분포 시각화.

The chart is deterministic code, so its contract is arithmetic. Atoms are
counted from the collected facts with the unit's kind as the sign, the band
is mean+2σ over this review's own units only (population σ, ÷n), and no
u- hash may reach the page — the tooltip speaks file · section · axis like
every other prose the report shows.
"""

from __future__ import annotations

import re

from app.mrdoc.collect import Collect, CollectedFile, CollectedUnit
from app.mrdoc.literals import ChangedValue
from app.mrdoc.scatter import (
    band_of,
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


def test_row_atoms_signed_by_kind() -> None:
    """y는 종류 부호가 붙은 원자 수 — 추가 +, 삭제 −, 변경은 변경 원자 수."""

    units = (
        _unit("u-11111111", kind="added", added=("a", "b"), prose_added=("c",)),
        _unit("u-22222222", kind="removed", removed=("x", "y")),
        _unit(
            "u-33333333",
            kind="changed",
            changed=(ChangedValue("만료", "60", "30"),),
            textual=(("설치 한", "설치한"),),
        ),
    )
    rows = scatter_rows(_collect(units), units)
    assert [row.y for row in rows] == [3, -2, 2]
    assert [row.x for row in rows] == [0, 1, 2]
    assert rows[0].changed_atoms == 0
    assert rows[2].changed_atoms == 2


def test_label_speaks_file_section_axis_without_hash() -> None:
    units = (_unit("u-abcdef12", kind="added", added=("a",), section="정책"),)
    rows = scatter_rows(_collect(units), units)
    label = rows[0].label
    assert "setup.md" in label
    assert "정책" in label
    assert "표현" in label
    assert not re.search(r"u-[0-9a-f]{8}", label)


def test_band_needs_five_rows_and_is_mean_plus_two_sigma() -> None:
    small = tuple(
        _unit(f"u-{i:08d}", kind="added", added=("a",)) for i in range(4)
    )
    small_rows = scatter_rows(_collect(small), small)
    assert band_of(small_rows) is None

    ys = (1, 1, 1, 1, 6)
    units = tuple(
        _unit(f"u-{i:08d}", kind="added", added=("a",) * y) for i, y in enumerate(ys)
    )
    rows = scatter_rows(_collect(units), units)
    # |y| = 1,1,1,1,6 — mean 2.0, population σ 2.0, edge 6.0
    assert band_of(rows) == (2.0, 6.0)


def test_interpret_sentence_counts_band_and_outliers() -> None:
    units = (
        _unit("u-00000001", kind="added", added=("a",), section="서두"),
        _unit("u-00000002", kind="added", added=("a",), section="서두"),
        _unit("u-00000003", kind="added", added=("a",), section="서두"),
        _unit("u-00000004", kind="added", added=("a", "b"), section="서두"),
        _unit("u-00000005", kind="added", added=("a", "b"), section="서두"),
        # 50-원자 유닛 — 유닛 5개로는 mean+2σ이 항상 최대값을 덮는다
        # (4Σ(x−μ)² ≥ d²), 밴드 밖 판정을 내려면 6개 이상이 필요하다.
        _unit("u-00000006", kind="added", added=("a",) * 50, section="운영 원칙"),
    )
    rows = scatter_rows(_collect(units, fix_targets=("u-00000006",)), units)
    text = interpret(rows, band_of(rows))
    assert "유닛 6개" in text
    assert "밴드 안 5개(83%)" in text
    assert "밴드 밖 1개" in text
    assert "운영 원칙" in text
    assert "fix 재작성 1개" in text
    assert not re.search(r"u-[0-9a-f]{8}", text)


def test_interpret_without_band_says_so() -> None:
    units = tuple(_unit(f"u-{i:08d}", kind="added", added=("a",)) for i in range(3))
    rows = scatter_rows(_collect(units), units)
    text = interpret(rows, None)
    assert "밴드는 유닛 5개 미만이라 생략" in text
    assert "유닛 3개" in text


def test_dot_plot_draws_band_only_when_available_and_hides_hashes() -> None:
    ys = (1, 1, 1, 1, 6)
    units = tuple(
        _unit(f"u-{i:08d}", kind="added", added=("a",) * y) for i, y in enumerate(ys)
    )
    rows = scatter_rows(_collect(units), units)
    with_band = dot_plot(rows, band_of(rows))
    assert with_band.count("<svg") == 1
    assert 'class="band"' in with_band
    assert not re.search(r"u-[0-9a-f]{8}", with_band)

    small = tuple(_unit(f"u-{i:08d}", kind="added", added=("a",)) for i in range(4))
    small_rows = scatter_rows(_collect(small), small)
    assert 'class="band"' not in dot_plot(small_rows, None)


def test_dot_plot_axis_colors_and_fix_ring() -> None:
    units = (
        _unit("u-00000001", axis="구조", kind="added", added=("a",)),
        _unit(
            "u-00000002",
            axis="의미",
            kind="changed",
            changed=(ChangedValue("만료", "60", "30"),),
        ),
        _unit("u-00000003", axis="표현", kind="changed", textual=(("한", "한"),)),
        _unit("u-00000004", axis="미분류", kind="added", added=("a",)),
    )
    rows = scatter_rows(_collect(units, fix_targets=("u-00000002",)), units)
    svg = dot_plot(rows, None)
    for color in ("#4c72b0", "#dd8452", "#55a868", "#8c8c8c"):
        assert color in svg
    assert 'class="dot fixed"' in svg
    assert svg.count('class="dot fixed"') == 1


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
