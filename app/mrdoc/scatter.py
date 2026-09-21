"""유닛 분포 — 라인 폭 × 원자 수 산점도, 결정론 SVG.

3번 매트릭스의 시각 짝. 유닛 하나가 점 하나: x는 07 excerpt가 잰 라인
폭(변경이 실제로 걸친 범위), y는 원자 수 — 두 축 모두 실측값이라 점의
위치가 곧 변경의 성격이다. 좌하단 뭉침은 국소 수정, 대각선(원자=폭)
근처는 밀도 높은 재작성, 우상단은 넓은 범위의 대형 수술. 모양은 연산
(추가▲/삭제▼/변경●), 색은 인벤토리의 첫 축, 금 테두리는 verifier의
FIX 지목, 겹침은 반투명이라 뭉칠수록 진해진다. 산문에 u- 해시는 못
온다: 툴팁·라벨·해석 문장은 파일 · 섹션 · 축으로만 말한다.
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
_WIDE_SPAN = 8  # 넓은 범위 — 폭 이상
_BIG_ATOMS = 5  # 대형 수술 — 원자 이상


@dataclass(frozen=True)
class ScatterRow:
    """One unit as a dot — prose only, no ids."""

    label: str  # "setup.md § 개요 · 표현 · 원자 3"
    short: str  # "개요" — chart label
    x: int  # 라인 폭
    y: int  # 원자 수
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
    """Build the dots — x is the measured line span, y the atom count."""

    label_of = {
        entry.file_id: entry.path.rpartition("/")[2] or entry.path
        for entry in collect.file_blocks
    }
    fixes = frozenset(collect.fix_targets)
    rows: list[ScatterRow] = []
    for unit in units:
        axis = _axis_of(unit)
        atoms = max(1, _atoms(unit))  # 유닛 자체가 최소 하나의 변경이다
        file_label = label_of.get(unit.file, unit.file)
        # excerpt.section은 'auth.md § 개요'처럼 파일명을 이미 물고 온다 —
        # § 유무로만 판단한다.
        where = unit.section if "§" in unit.section else f"{file_label} § {unit.section}"
        short = unit.section.split("§")[-1].strip() if "§" in unit.section else unit.section
        fixed = unit.unit_id in fixes
        rows.append(
            ScatterRow(
                label=(
                    f"{where} · {axis} · 원자 {atoms} · 폭 {unit.span}"
                    + (" · fix됨" if fixed else "")
                ),
                short=short,
                x=unit.span,
                y=atoms,
                axis=axis,
                kind=unit.kind,
                fixed=fixed,
                file_label=file_label,
            )
        )
    return tuple(rows)


def _ticks(maxv: int) -> list[int]:
    step = max(1, math.ceil(maxv / 6))
    return list(range(0, maxv + 1, step))


def _jitter(index: int, salt: int) -> float:
    """Deterministic ±0.2 spread — stacked coordinates become a cloud."""

    return (((index * 37 + salt * 53) % 11) - 5) / 5 * 0.2


def dot_plot(rows: tuple[ScatterRow, ...], *, width: int = 760) -> str:
    """One self-contained SVG — inline colors, native <title> tooltips."""

    if not rows:
        return ""
    left, right, top, axis_h = 46, 110, 14, 46
    plot_h = 230
    height = top + plot_h + axis_h
    plot_w = width - left - right
    x_max = max(max(r.x for r in rows), 1)
    y_max = max(max(r.y for r in rows), 1)

    def x_of(v: float) -> float:
        return left + (max(0.0, v) / x_max) * plot_w

    def y_of(v: float) -> float:
        return top + plot_h - (max(0.0, v) / y_max) * plot_h

    parts: list[str] = [
        f'<svg class="scatter" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="유닛 분포 — 가로 라인 폭, 세로 원자 수">'
    ]

    # grid + ticks
    for v in _ticks(x_max):
        xx = x_of(v)
        parts.append(
            f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{top}" y2="{top + plot_h}" '
            'stroke="#eef1f4"/>'
            f'<text x="{xx:.1f}" y="{top + plot_h + 14:.1f}" text-anchor="middle">{v}</text>'
        )
    for v in _ticks(y_max):
        yy = y_of(v)
        parts.append(
            f'<line x1="{left}" x2="{left + plot_w}" y1="{yy:.1f}" '
            f'y2="{yy:.1f}" stroke="#eef1f4"/>'
            f'<text x="{left - 6}" y="{yy + 3.5:.1f}" text-anchor="end">{v}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w:.1f}" y="{top + plot_h + 30:.1f}" '
        'text-anchor="end" class="ax">라인 폭</text>'
        f'<text x="{left - 6}" y="{top - 4:.1f}" text-anchor="end" class="ax">원자 수</text>'
    )

    # 대각선 — 원자=폭의 최대 밀도선, 플롯을 벗어나는 지점에서 끊는다
    m = min(x_max, y_max)
    if m >= 1:
        parts.append(
            f'<line class="diag" x1="{x_of(0):.1f}" y1="{y_of(0):.1f}" '
            f'x2="{x_of(m):.1f}" y2="{y_of(m):.1f}" stroke="#9aa4ad" '
            'stroke-dasharray="4 4"/>'
        )

    # dots — shape by kind, translucent so overlap darkens
    centers: dict[int, tuple[float, float]] = {}
    for index, row in enumerate(rows):
        jx = min(max(0.0, row.x + _jitter(index, 1)), x_max)
        jy = min(max(0.0, row.y + _jitter(index, 2)), y_max)
        cx = x_of(jx)
        cy = y_of(jy)
        centers[index] = (cx, cy)
        color = _AXIS_COLORS.get(row.axis, _AXIS_COLORS[classes.UNCLASSIFIED])
        stroke = (
            f'stroke="{_FIX_STROKE}" stroke-width="2"'
            if row.fixed
            else f'stroke="{color}" stroke-width="1"'
        )
        cls = "dot fixed" if row.fixed else "dot"
        tip = f"<title>{html.escape(row.label)}</title>"
        r = 5.0
        if row.kind == "added":
            pts = (
                f"{cx:.1f},{cy - r:.1f} {cx - r:.1f},{cy + r:.1f} "
                f"{cx + r:.1f},{cy + r:.1f}"
            )
            parts.append(
                f'<polygon class="{cls}" points="{pts}" fill="{color}" '
                f'fill-opacity="0.55" {stroke}>{tip}</polygon>'
            )
        elif row.kind == "removed":
            pts = (
                f"{cx:.1f},{cy + r:.1f} {cx - r:.1f},{cy - r:.1f} "
                f"{cx + r:.1f},{cy - r:.1f}"
            )
            parts.append(
                f'<polygon class="{cls}" points="{pts}" fill="{color}" '
                f'fill-opacity="0.55" {stroke}>{tip}</polygon>'
            )
        else:
            parts.append(
                f'<circle class="{cls}" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
                f'fill="{color}" fill-opacity="0.55" {stroke}>{tip}</circle>'
            )

    # labels — the biggest three units, like the reference's named outliers
    labelled = sorted(rows, key=lambda r: (-r.y, -r.x))[:3]
    for row in labelled:
        if row.y < 3:
            continue
        index = rows.index(row)
        cx, cy = centers[index]
        if cx > left + plot_w * 0.72:
            parts.append(
                f'<text x="{cx - 8:.1f}" y="{cy + 3.5:.1f}" text-anchor="end" '
                f'class="lbl">{html.escape(row.short)}</text>'
            )
        else:
            parts.append(
                f'<text x="{cx + 8:.1f}" y="{cy + 3.5:.1f}" '
                f'class="lbl">{html.escape(row.short)}</text>'
            )

    # legend — axis colors, kind shapes, fix ring, diagonal note
    lx, ly = left, height - 8
    for axis in _AXIS_ORDER:
        parts.append(
            f'<circle cx="{lx:.1f}" cy="{ly}" r="4" fill="{_AXIS_COLORS[axis]}"/>'  # noqa: E501
        )
        parts.append(
            f'<text x="{lx + 8:.1f}" y="{ly + 3.5}">{html.escape(axis)}</text>'
        )
        lx += 14 + len(axis) * 11
    parts.append(
        f'<polygon points="{lx},{ly - 4} {lx - 4},{ly + 4} {lx + 4},{ly + 4}" '
        'fill="#8c8c8c"/>'
        f'<text x="{lx + 8:.1f}" y="{ly + 3.5}">추가</text>'
    )
    lx += 14 + 2 * 11
    parts.append(
        f'<polygon points="{lx},{ly + 4} {lx - 4},{ly - 4} {lx + 4},{ly - 4}" '
        'fill="#8c8c8c"/>'
        f'<text x="{lx + 8:.1f}" y="{ly + 3.5}">삭제</text>'
    )
    lx += 14 + 2 * 11
    parts.append(
        f'<circle cx="{lx:.1f}" cy="{ly}" r="4" fill="#fff" '
        f'stroke="{_FIX_STROKE}" stroke-width="2"/>'
    )
    parts.append(f'<text x="{lx + 8:.1f}" y="{ly + 3.5}">fix</text>')
    lx += 14 + 3 * 11
    parts.append(
        f'<line x1="{lx:.1f}" x2="{lx + 14:.1f}" y1="{ly}" y2="{ly}" '
        'stroke="#9aa4ad" stroke-dasharray="4 4"/>'
        f'<text x="{lx + 18:.1f}" y="{ly + 3.5}">최대 밀도</text>'
    )
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


def interpret(rows: tuple[ScatterRow, ...]) -> str:
    """The chart's one deterministic reading — counts, never judgement."""

    if not rows:
        return ""
    total = len(rows)
    big = wide = 0
    for r in rows:
        if r.y >= _BIG_ATOMS:
            big += 1
        elif r.x >= _WIDE_SPAN:
            wide += 1
    rest = total - big - wide
    biggest = max(rows, key=lambda r: (r.y, r.x))
    axis_part = "축 분포: " + " · ".join(
        f"{axis} {sum(1 for r in rows if r.axis == axis)}"
        for axis in _AXIS_ORDER
        if any(r.axis == axis for r in rows)
    )
    fixed_count = sum(1 for r in rows if r.fixed)
    fixed_part = f" · fix 재작성 {fixed_count}개" if fixed_count else ""
    buckets = [f"잔 변경 {rest}"]
    if wide:
        buckets.append(f"넓은 범위 {wide}")
    if big:
        buckets.append(f"대형 수술 {big}")
    return (
        f"유닛 {total}개 — {' · '.join(buckets)}"
        f" · 최대: {biggest.short} (폭 {biggest.x} · 원자 {biggest.y}). "
        f"{axis_part}{fixed_part}"
    )


def distribution_html(
    collect: Collect, units: tuple[CollectedUnit, ...]
) -> str:
    """3번 매트릭스 뒤에 붙는 블록 — 유닛이 없으면 통째로 생략."""

    rows = scatter_rows(collect, units)
    if not rows:
        return ""
    parts = ["<h3>유닛 분포</h3>"]
    parts.append(
        '<p class="sub">가로 = 변경이 걸친 라인 폭 · 세로 = 원자 수 · '
        "점선 = 최대 밀도(원자=폭) · ▲ 추가 ▼ 삭제 ● 변경</p>"
    )
    parts.append(f"<p>{html.escape(interpret(rows))}</p>")
    parts.append(f'<div class="chart">{dot_plot(rows)}</div>')
    heat = heatmap(collect)
    if heat:
        parts.append("<h3>파일 × 축 밀도</h3>")
        parts.append(f'<div class="chart">{heat}</div>')
    return "".join(parts)
