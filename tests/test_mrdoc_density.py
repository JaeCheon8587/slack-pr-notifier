"""Tests for app/mrdoc/density.py — 파일 × 축 밀도 표.

matrix 셀의 축별 합을 색 농도로 칠하는 결정론 표 — 판정은 matrix가
이미 끝낸 사실 그대로, 해석 문장은 붙지 않는다.
"""

from __future__ import annotations

from app.mrdoc.collect import Collect, CollectedFile, CollectedUnit
from app.mrdoc.density import distribution_html, heatmap
from app.mrdoc.literals import ChangedValue


def _unit(
    uid: str,
    *,
    kind: str = "changed",
    axis: str = "표현",
    changed: tuple[ChangedValue, ...] = (),
) -> CollectedUnit:
    return CollectedUnit(
        unit_id=uid,
        kind=kind,
        structure_kind="none",
        klass=axis,
        section="개요",
        file="f-01",
        removed=(),
        added=(),
        changed=changed,
        excerpt_ref=uid,
        explanation="값이 바뀌었다.",
        axes=(axis,),
        ops=(),
        textual=(),
        prose_added=(),
        prose_removed=(),
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
    file_blocks: tuple[CollectedFile, ...] | None = None,
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
            file_blocks
            if file_blocks is not None
            else (
                CollectedFile(
                    file_id="f-01",
                    path="docs/p-a/setup.md",
                    product_dir="docs/p-a",
                    units=len(units),
                    summary="요약",
                ),
            )
        ),
        units=units,
    )


def test_heatmap_sums_matrix_cells_per_axis() -> None:
    collect = _collect(
        (_unit("u-00000001", changed=(ChangedValue("a", "1", "2"),)),),
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


def test_heatmap_alpha_scales_with_peak() -> None:
    collect = _collect(
        (),
        matrix=_matrix(
            {"구조": {"추가": 4, "삭제": 0, "변경": 0}, "의미": {"추가": 1, "삭제": 0, "변경": 0}}
        ),
    )
    out = heatmap(collect)
    assert "0.65" in out  # peak cell — 0.10 + 0.55 * (4/4)
    assert "0.24" in out  # quarter cell — 0.10 + 0.55 * (1/4)


def test_distribution_html_emits_only_the_heatmap() -> None:
    assert distribution_html(_collect((), file_blocks=())) == ""
    collect = _collect(
        (_unit("u-00000001", changed=(ChangedValue("a", "1", "2"),)),),
        matrix=_matrix({"의미": {"추가": 0, "삭제": 0, "변경": 1}}),
    )
    out = distribution_html(collect)
    assert "파일 × 축 밀도" in out
    assert "유닛 분포" not in out
    assert "<svg" not in out
