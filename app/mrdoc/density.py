"""파일 × 축 밀도 — 3번 매트릭스의 시각 짝, 결정론 HTML.

matrix 셀(축 × 연산 카운트)을 축별로 합산해 색 농도로 칠한 표 하나.
판정·해석은 넣지 않는다 — 수치는 matrix가 이미 확정한 사실 그대로다.
"""

from __future__ import annotations

import html

from . import classes
from .collect import Collect

_AXIS_COLORS: dict[str, str] = {
    classes.STRUCTURE: "#4c72b0",
    classes.MEANING: "#dd8452",
    classes.EXPRESSION: "#55a868",
}


def heatmap(collect: Collect) -> str:
    """파일 × 축 밀도 — matrix 셀을 축별 합으로 농도낸다."""

    if not collect.file_blocks:
        return ""
    axes = (classes.STRUCTURE, classes.MEANING, classes.EXPRESSION)

    def cell(file_id: str, axis: str) -> int:
        return sum(collect.matrix.get(file_id, {}).get(axis, {}).values())

    peak = max(
        (cell(e.file_id, axis) for e in collect.file_blocks for axis in axes),
        default=0,
    )
    head = (
        "<tr><th>파일</th>"
        + "".join(f"<th>{html.escape(axis)}</th>" for axis in axes)
        + "</tr>"
    )
    rows_html: list[str] = []
    for entry in collect.file_blocks:
        cells: list[str] = []
        for axis in axes:
            value = cell(entry.file_id, axis)
            rgb = _AXIS_COLORS[axis].lstrip("#")
            r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
            alpha = 0.10 + 0.55 * (value / peak) if peak and value else 0.0
            cells.append(
                f'<td class="hc" style="background:rgba({r},{g},{b},{alpha:.2f})">'
                f"{value}</td>"
            )
        basename = entry.path.rpartition("/")[2] or entry.path
        rows_html.append(
            f'<tr><td title="{html.escape(entry.path)}">{html.escape(basename)}</td>'
            + "".join(cells)
            + "</tr>"
        )
    return (
        '<div class="wrap"><table class="heat"><thead>'
        + head
        + "</thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table></div>"
    )


def distribution_html(collect: Collect) -> str:
    """3번 매트릭스 뒤에 붙는 블록 — 파일이 없으면 통째로 생략."""

    heat = heatmap(collect)
    if not heat:
        return ""
    return f'<h3>파일 × 축 밀도</h3><div class="chart">{heat}</div>'
