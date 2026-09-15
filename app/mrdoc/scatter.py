"""유닛 분포 시각화 — 이번 검사 안의 유닛만, 결정론 SVG.

3번 매트릭스의 시각 짝. 유닛 하나가 점 하나: x는 리포트가 이미 쓰는
문서 순서, y는 종류 부호가 붙은 원자 수(추가·변경은 위, 삭제는 아래),
색은 인벤토리가 그 유닛을 놓은 첫 축, 금색 테두리는 verifier의 FIX
블록이 지목한 유닛. 밴드는 이번 검사 자체의 |원자| 평균+2σ — 과거
데이터도 기준선도 없다. 산문에 u- 해시는 못 온다: 툴팁과 해석 문장은
파일 · 섹션 · 축으로만 말한다.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass

from . import classes
from .collect import Collect, CollectedUnit

_AXIS_ORDER: tuple[str, ...] = (
    classes.STRUCTURE,
    classes.MEANING,
    classes.EXPRESSION,
    classes.UNCLASSIFIED,
)
_AXIS_COLORS: dict[str, str] = {
    classes.STRUCTURE: "#4c72b0",
    classes.MEANING: "#dd8452",
    classes.EXPRESSION: "#55a868",
    classes.UNCLASSIFIED: "#8c8c8c",
}
_FIX_STROKE = "#b98a00"


@dataclass(frozen=True)
class ScatterRow:
    """One unit as a dot — prose only, no ids."""

    label: str  # "setup.md § 개요 · 표현 · 원자 3"
    x: int  # document order, 0-based
    y: int  # signed atom count: + added/changed, − removed
    changed_atoms: int
    axis: str
    kind: str
    fixed: bool
    file_label: str


def _atoms(unit: CollectedUnit) -> int:
    """The unit's atom count — every atom kind 06 records, nothing else."""

    if unit.kind == "removed":
        return len(unit.removed) + len(unit.prose_removed)
    if unit.kind == "added":
        return len(unit.added) + len(unit.prose_added)
    return len(unit.changed) + len(unit.textual)


def _axis_of(unit: CollectedUnit) -> str:
    for axis in _AXIS_ORDER:
        if axis in unit.axes:
            return axis
    return classes.UNCLASSIFIED


def scatter_rows(
    collect: Collect, units: tuple[CollectedUnit, ...]
) -> tuple[ScatterRow, ...]:
    """Build the dots — x keeps the report's own document order."""

    label_of = {
        entry.file_id: entry.path.rpartition("/")[2] or entry.path
        for entry in collect.file_blocks
    }
    fixes = frozenset(collect.fix_targets)
    rows: list[ScatterRow] = []
    for index, unit in enumerate(units):
        axis = _axis_of(unit)
        atoms = _atoms(unit)
        file_label = label_of.get(unit.file, unit.file)
        # excerpt.section은 'auth.md § 개요'처럼 파일명을 이미 물고 온다 —
        # § 유무로만 판단한다.
        where = unit.section if "§" in unit.section else f"{file_label} § {unit.section}"
        fixed = unit.unit_id in fixes
        rows.append(
            ScatterRow(
                label=(
                    f"{where} · {axis} · 원자 {atoms}"
                    + (" · fix됨" if fixed else "")
                ),
                x=index,
                y=(-1 if unit.kind == "removed" else 1) * atoms,
                changed_atoms=(
                    len(unit.changed) + len(unit.textual)
                    if unit.kind == "changed"
                    else 0
                ),
                axis=axis,
                kind=unit.kind,
                fixed=fixed,
                file_label=file_label,
            )
        )
    return tuple(rows)


def band_of(rows: tuple[ScatterRow, ...]) -> tuple[float, float] | None:
    """(mean, mean+2σ) over this review's |y| — None under five units.

    Population σ (÷n): the band describes this run, it does not estimate
    any larger population. Five is where the number stops meaning anything.
    """

    if len(rows) < 5:
        return None
    magnitudes = [abs(row.y) for row in rows]
    mean = sum(magnitudes) / len(magnitudes)
    variance = sum((m - mean) ** 2 for m in magnitudes) / len(magnitudes)
    return (mean, mean + 2 * math.sqrt(variance))


def dot_plot(
    rows: tuple[ScatterRow, ...],
    band: tuple[float, float] | None,
    *,
    width: int = 760,
) -> str:
    """One self-contained SVG — inline colors, native <title> tooltips."""

    if not rows:
        return ""
    compact = len(rows) < 10
    plot_h = 150 if compact else 230
    left, right, top, axis_h = 46, 18, 14, 44
    height = top + plot_h + axis_h
    plot_w = width - left - right
    col = plot_w / len(rows)

    up = max(max((r.y for r in rows if r.y > 0), default=0), 1)
    down = max(max((-r.y for r in rows if r.y < 0), default=0), 1)
    zero = top + plot_h * (up / (up + down))

    def y_of(value: float) -> float:
        v = max(float(-down), min(float(up), value))
        if v >= 0:
            return zero - (v / up) * (zero - top)
        return zero + (-v / down) * (top + plot_h - zero)

    parts: list[str] = [
        f'<svg class="scatter" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="유닛 분포 — 가로 문서 순서, 세로 원자 수">'
    ]
    if band is not None:
        y_top, y_bottom = y_of(band[1]), y_of(-band[1])
        parts.append(
            f'<rect class="band" x="{left}" y="{y_top:.1f}" '
            f'width="{plot_w}" height="{max(y_bottom - y_top, 1.0):.1f}" '
            'fill="#8c8c8c" fill-opacity="0.08"/>'
        )

    has_pos = any(r.y > 0 for r in rows)
    has_neg = any(r.y < 0 for r in rows)
    grid = ""
    if has_pos:
        step = max(1, math.ceil(up / 6))
        for v in range(0, up + 1, step):
            yy = y_of(v)
            grid += (
                f'<line x1="{left}" x2="{left + plot_w}" y1="{yy:.1f}" '
                f'y2="{yy:.1f}" stroke="#e2e6ea"/>'
                f'<text x="{left - 6}" y="{yy + 3.5:.1f}" text-anchor="end">{v}</text>'
            )
    if has_neg:
        step = max(1, math.ceil(down / 6))
        for v in range(step, down + 1, step):
            yy = y_of(-v)
            grid += (
                f'<line x1="{left}" x2="{left + plot_w}" y1="{yy:.1f}" '
                f'y2="{yy:.1f}" stroke="#e2e6ea"/>'
                f'<text x="{left - 6}" y="{yy + 3.5:.1f}" text-anchor="end">-{v}</text>'
            )
    parts.append(grid)
    parts.append(
        f'<line x1="{left}" x2="{left + plot_w}" y1="{zero:.1f}" '
        f'y2="{zero:.1f}" stroke="#9aa4ad" stroke-width="1.2"/>'
    )

    # file groups — separators between runs of the same file_label
    groups: list[list[object]] = []
    for row in rows:
        if groups and groups[-1][0] == row.file_label:
            groups[-1][2] = row.x + 1
        else:
            groups.append([row.file_label, row.x, row.x + 1])
    for group in groups[1:]:
        x = left + float(group[1]) * col
        parts.append(
            f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top + plot_h}" '
            'stroke="#d5dbe0" stroke-dasharray="3 3"/>'
        )
    for label, start, end in groups:
        mid = left + (float(start) + float(end)) / 2 * col
        parts.append(
            f'<text x="{mid:.1f}" y="{top + plot_h + 16:.1f}" '
            f'text-anchor="middle">{html.escape(str(label))}</text>'
        )

    for row in rows:
        cx = left + (row.x + 0.5) * col
        color = _AXIS_COLORS.get(row.axis, _AXIS_COLORS[classes.UNCLASSIFIED])
        radius = (
            4.5
            if row.changed_atoms == 0
            else min(9.0, 4.0 + 1.1 * row.changed_atoms)
        )
        if row.fixed:
            stroke = f'stroke="{_FIX_STROKE}" stroke-width="2"'
            cls = "dot fixed"
        else:
            stroke = f'stroke="{color}" stroke-width="1"'
            cls = "dot"
        parts.append(
            f'<circle class="{cls}" cx="{cx:.1f}" cy="{y_of(row.y):.1f}" '
            f'r="{radius:.1f}" fill="{color}" {stroke}>'
            f"<title>{html.escape(row.label)}</title></circle>"
        )

    # legend — one row, bottom-left
    lx, ly = left, height - 8
    for axis in _AXIS_ORDER:
        parts.append(
            f'<circle cx="{lx:.1f}" cy="{ly}" r="4" fill="{_AXIS_COLORS[axis]}"/>'
        )
        parts.append(
            f'<text x="{lx + 8:.1f}" y="{ly + 3.5}">{html.escape(axis)}</text>'
        )
        lx += 14 + len(axis) * 11
    if band is not None:
        parts.append(
            f'<rect x="{lx:.1f}" y="{ly - 4}" width="10" height="8" '
            'fill="#8c8c8c" fill-opacity="0.15"/>'
        )
        parts.append(f'<text x="{lx + 14:.1f}" y="{ly + 3.5}">평균±2σ</text>')
        lx += 14 + 5 * 11
    parts.append(
        f'<circle cx="{lx:.1f}" cy="{ly}" r="4" fill="#fff" '
        f'stroke="{_FIX_STROKE}" stroke-width="2"/>'
    )
    parts.append(f'<text x="{lx + 8:.1f}" y="{ly + 3.5}">fix</text>')
    parts.append("</svg>")
    return "".join(parts)


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


def interpret(rows: tuple[ScatterRow, ...], band: tuple[float, float] | None) -> str:
    """The chart's one deterministic reading — counts, never judgement."""

    if not rows:
        return ""
    total = len(rows)
    axis_part = "축 분포: " + " · ".join(
        f"{axis} {sum(1 for r in rows if r.axis == axis)}"
        for axis in _AXIS_ORDER
        if any(r.axis == axis for r in rows)
    )
    fixed_count = sum(1 for r in rows if r.fixed)
    fixed_part = f" · fix 재작성 {fixed_count}개" if fixed_count else ""
    if band is None:
        return f"유닛 {total}개 — 밴드는 유닛 5개 미만이라 생략. {axis_part}{fixed_part}"
    edge = band[1]
    inside = sum(1 for r in rows if abs(r.y) <= edge)
    outside = total - inside
    lead = (
        f"유닛 {total}개 — 밴드 안 {inside}개({round(100 * inside / total)}%), "
        f"밴드 밖 {outside}개"
    )
    if outside:
        biggest = max(rows, key=lambda r: abs(r.y))
        lead += f"(최대: {biggest.label})"
    return f"{lead}. {axis_part}{fixed_part}"


def distribution_html(
    collect: Collect, units: tuple[CollectedUnit, ...]
) -> str:
    """3번 매트릭스 뒤에 붙는 블록 — 유닛이 없으면 통째로 생략."""

    rows = scatter_rows(collect, units)
    if not rows:
        return ""
    band = band_of(rows)
    parts = ["<h3>유닛 분포</h3>"]
    if band is None:
        note = "밴드 없음 — 유닛 5개 미만"
    else:
        note = f"밴드 = 이번 검사 |원자| 평균 {band[0]:.1f} + 2σ (상한 {band[1]:.1f})"
    parts.append(f'<p class="sub">{html.escape(note)}</p>')
    parts.append(f"<p>{html.escape(interpret(rows, band))}</p>")
    parts.append(f'<div class="chart">{dot_plot(rows, band)}</div>')
    heat = heatmap(collect)
    if heat:
        parts.append("<h3>파일 × 축 밀도</h3>")
        parts.append(f'<div class="chart">{heat}</div>')
    return "".join(parts)
