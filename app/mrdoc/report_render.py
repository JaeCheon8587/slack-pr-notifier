"""report.html + slack-summary.txt — one refs gate, then one self-contained page.

The design's three render steps: (1) every block's refs must exist in
50-collect, otherwise the block is dropped, (2) the body comes from
50-collect — the renderer never recounts and never rewrites, (3) out come
report.html (single file, inline CSS, zero external dependencies) and
slack-summary.txt (section 1 verbatim, no document body).

The page is organised 파일(제품) → 연산 → 성질, not by level: 개요 · 변경
매트릭스 · 파일별 상세(3.1 구조 · 3.2 추가 · 3.3 삭제 · 3.4 변경) · 부록.
Nothing here judges — 판정 · 심각도 · 머지 의견이 모두 없다. Structure,
raw text and literal values come from the tool artifacts, so a run whose
설명 all failed still renders every one of them; only the prose says so.

Hand-rolled HTML with escaping — Jinja2 would be the pipeline's first
template dependency for a page this small.
"""

from __future__ import annotations

import html

from . import classes
from .collect import (
    EXPLANATION_FAILED,
    SUMMARY_FAILED,
    Collect,
    CollectedFile,
    CollectedUnit,
)
from .excerpt import Excerpts
from .structure import Structure, TreeSection

_AXES = (classes.STRUCTURE, classes.MEANING, classes.EXPRESSION)
_OPS = ("추가", "삭제", "변경")

#: 부록 carries the same wording in every product's report — 10 reports have
#: to be comparable, so the criteria are not re-worded per run.
CRITERIA: tuple[tuple[str, str], ...] = (
    (classes.STRUCTURE, "헤딩 · 파일 · 절의 존재 · 위치 · 레벨이 바뀜. 본문 명제는 무관"),
    (
        classes.MEANING,
        "명제가 달라짐 — 값 · 수치 · 조건 · 절차 순서 · 대상 · 부정/긍정 · 범위",
    ),
    (classes.EXPRESSION, "명제 동일 — 동의어 · 어순 · 오탈자 · 서식 · 문장 분할/병합"),
)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _basename(path: str) -> str:
    return path.rpartition("/")[2] or path


def _depth(heading_path: str) -> int:
    """05-structure carries the ancestor path, so nesting depth is its length."""

    return heading_path.count(" > ") + 1


def _leaf(heading_path: str) -> str:
    return heading_path.rpartition(" > ")[2] or heading_path


def _parent_of(heading_path: str) -> str:
    """The ancestor path — '' for a top-level section."""

    return heading_path.rpartition(" > ")[0]


def _previous_sibling(rows: list[TreeSection], row: TreeSection):
    """The section the reader sees just above, at the same level."""

    parent = _parent_of(row.heading_path)
    for prev in reversed(rows[: rows.index(row)]):
        if _parent_of(prev.heading_path) == parent:
            return prev
    return None


def _ro(level: int) -> str:
    """'3으로' vs '2로' — 받침 있는 수독음(삼·육)만 으로."""

    return "으로" if level in (3, 6) else "로"


# --------------------------------------------------------------------------
# the refs gate
# --------------------------------------------------------------------------


def gate(collect: Collect) -> tuple[tuple[CollectedUnit, ...], int]:
    """Keep units whose file exists in 50-collect; count what was dropped.

    This is the renderer's only gate. A unit pointing at a file_id no block
    declares cannot be placed under any file, and the design drops such a
    block instead of asking for a rewrite — the pipeline keeps one exit
    condition and the drop count goes to the report's 부분 실패 목록.
    """

    known = {entry.file_id for entry in collect.file_blocks}
    kept = tuple(unit for unit in collect.units if unit.file in known)
    return kept, len(collect.units) - len(kept)


# --------------------------------------------------------------------------
# section 1 — the overview, shared with slack-summary.txt
# --------------------------------------------------------------------------


def _prose_failures(
    collect: Collect, units: tuple[CollectedUnit, ...]
) -> tuple[str, ...]:
    """Files whose 설명 or FILE_SUMMARY came back empty — never silent.

    One document must count once. failed_files names the 20-analysis artifact
    ('<file_id>.md') while the blocks name the document path, so the artifact
    names are resolved through file_id first — the measured run counted a
    single unreadable file twice.
    """

    paths = {entry.file_id: entry.path for entry in collect.file_blocks}
    failed = {
        paths.get(name.removesuffix(".md"), name) for name in collect.failed_files
    }
    for entry in collect.file_blocks:
        if entry.summary == SUMMARY_FAILED:
            failed.add(entry.path)
    for unit in units:
        if unit.explanation == EXPLANATION_FAILED:
            failed.add(paths.get(unit.file, unit.file))
    return tuple(sorted(failed))


def overview_lines(
    collect: Collect,
    *,
    mr_iid: int,
    base_sha: str = "",
    head_sha: str = "",
) -> list[str]:
    """Section 1 as plain text — the one source both outputs read."""

    units, dropped = gate(collect)
    dirs = sorted({entry.product_dir for entry in collect.file_blocks})
    counts = collect.classes
    head = f"MR !{mr_iid}"
    if base_sha and head_sha:
        head += f" · base {base_sha} → head {head_sha}"
    lines = [
        head,
        "영향 제품 "
        + (" · ".join(dirs) if dirs else "(없음)")
        + f" — 파일 {len(collect.file_blocks)} · 유닛 {len(units)}",
        " · ".join(
            [
                f"구조 {counts.get(classes.STRUCTURE, 0)}",
                f"의미 {counts.get(classes.MEANING, 0)} (값 {collect.value_changes})",
                f"표현 {counts.get(classes.EXPRESSION, 0)}",
                f"추가 {collect.ops.get('추가', 0)}",
                f"삭제 {collect.ops.get('삭제', 0)}",
                f"변경 {collect.ops.get('변경', 0)}",
                f"이동 {collect.files.get('moved_sections', 0)}",
            ]
        ),
    ]
    for entry in collect.file_blocks:
        lines.append(f"{_basename(entry.path)}: {entry.summary}")
    lines.append(
        "[리포트 신뢰도] "
        + " · ".join(
            [
                f"설명 생성 실패 파일 {len(_prose_failures(collect, units))}",
                f"지적 잔존 {collect.verify.get('outstanding', 0)}",
                f"집계 불일치 {collect.verify.get('counts_mismatch', 0)}",
                f"분류 불확실 {counts.get(classes.UNCLASSIFIED, 0)}",
                f"refs 드롭 {collect.refs_dropped + dropped}",
            ]
        )
    )
    return lines


def render_slack_summary(
    collect: Collect,
    *,
    mr_iid: int,
    base_sha: str = "",
    head_sha: str = "",
    link: str = "",
) -> str:
    """slack-summary.txt — section 1 and nothing else.

    The document body never reaches Slack; the overview does, 리포트 신뢰도
    included, because a partial failure disappearing quietly is this
    pipeline's worst failure mode.
    """

    lines = overview_lines(
        collect, mr_iid=mr_iid, base_sha=base_sha, head_sha=head_sha
    )
    if link:
        lines.append(link)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# section 3.1 — structure notes, generated from the two trees
# --------------------------------------------------------------------------


def _rows_of(rows: tuple[TreeSection, ...], path: str) -> list[TreeSection]:
    return [row for row in rows if row.file == path]


def _by_lines(rows: list[TreeSection], lines: tuple[int, int] | None):
    if lines is None:
        return None
    for row in rows:
        if row.lines == lines:
            return row
    return None


def _reorder_notes(
    base_rows: list[TreeSection], head_rows: list[TreeSection]
) -> list[str]:
    """Sections whose neighbours changed — pure relocation makes no unit.

    Identical text moved to a new place produces no change unit anywhere
    upstream, so comparing TREE against BASE_TREE is the only place it can
    surface. Reported by the section it now follows, which is what a reader
    needs to find it again.
    """

    base_order = [row.heading_path for row in base_rows]
    head_order = [row.heading_path for row in head_rows]
    common = [path for path in head_order if path in base_order]
    base_common = [path for path in base_order if path in head_order]
    if common == base_common:
        return []
    notes: list[str] = []
    for index, path in enumerate(common):
        if index < len(base_common) and base_common[index] == path:
            continue
        after = common[index - 1] if index else ""
        notes.append(
            f"§{_leaf(path)} 가 §{_leaf(after)} 뒤로 이동되었다"
            if after
            else f"§{_leaf(path)} 가 문서 맨 앞으로 이동되었다"
        )
    return notes


def structure_notes(structure: Structure, path: str) -> list[str]:
    """The tool's own sentences about one file's heading tree."""

    head_rows = _rows_of(structure.tree, path)
    base_rows = _rows_of(structure.base_tree, path)
    heads = {row.section_id: row for row in structure.tree}
    notes: list[str] = []
    for unit in structure.changed:
        head_row = heads.get(unit.section_id)
        base_row = _by_lines(base_rows, unit.old_lines)
        owner = head_row.file if head_row else (base_row.file if base_row else "")
        if owner != path:
            continue
        kind = unit.structure_kind
        if kind == "heading_renamed" and head_row and base_row:
            notes.append(
                f"§{_leaf(base_row.heading_path)} 가 "
                f"§{_leaf(head_row.heading_path)} 로 개명되었다"
            )
        elif kind == "level_changed" and head_row and base_row:
            level = _depth(head_row.heading_path)
            notes.append(
                f"§{_leaf(head_row.heading_path)} 의 레벨이 "
                f"{_depth(base_row.heading_path)}에서 {level}{_ro(level)} 바뀌었다"
            )
        elif kind == "reordered" and head_row:
            notes.append(f"§{_leaf(head_row.heading_path)} 의 순서가 바뀌었다")
        elif kind == "added" and head_row:
            sibling = _previous_sibling(head_rows, head_row)
            if sibling:
                where = f"§{_leaf(sibling.heading_path)} 뒤에"
            else:
                parent = _parent_of(head_row.heading_path)
                where = f"§{_leaf(parent)} 안에" if parent else "문서 맨 앞에"
            notes.append(
                f"§{_leaf(head_row.heading_path)} 절이 {where} 추가되었다"
            )
        elif kind == "removed" and base_row:
            sibling = _previous_sibling(base_rows, base_row)
            where = f"(기존 §{_leaf(sibling.heading_path)} 뒤)" if sibling else ""
            notes.append(f"§{_leaf(base_row.heading_path)} 절이 삭제되었다{where}")
    notes.extend(_reorder_notes(base_rows, head_rows))
    for move in structure.moved:
        if path in (move.from_file, move.to_file):
            notes.append(
                f"§{_leaf(move.section)} 절이 "
                f"{move.from_file}에서 {move.to_file}로 이동되었다"
            )
    return list(dict.fromkeys(notes))


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem;
  font: 15px/1.65 -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;
  color: #1b1f24; background: #fbfbfa; max-width: 62rem; margin-inline: auto;
}
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2.5rem 0 .75rem; padding-bottom: .3rem;
     border-bottom: 2px solid #d7dbe0; }
h3 { font-size: 1.02rem; margin: 1.75rem 0 .5rem; font-family: ui-monospace,
     "Cascadia Mono", Consolas, monospace; }
h4 { font-size: .92rem; margin: 1.25rem 0 .4rem; color: #4a5560;
     text-transform: none; letter-spacing: .01em; }
h5 { font-size: .78rem; margin: 0 0 .3rem; color: #6b7684; font-weight: 600; }
p { margin: .35rem 0; }
.sub { color: #6b7684; font-size: .85rem; margin: 0 0 .5rem; }
.ov p { margin: .15rem 0; }
.ov { background: #fff; border: 1px solid #e2e6ea; border-radius: 6px;
      padding: .85rem 1rem; }
.trust { margin-top: .6rem !important; padding-top: .5rem;
         border-top: 1px dashed #d7dbe0; color: #4a5560; font-size: .88rem; }
table { border-collapse: collapse; width: 100%; font-size: .88rem;
        background: #fff; }
th, td { border: 1px solid #e2e6ea; padding: .35rem .5rem; text-align: left; }
th { background: #f2f4f6; font-weight: 600; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
.wrap { overflow-x: auto; }
.file { border: 1px solid #e2e6ea; border-radius: 6px; background: #fff;
        padding: .5rem 1rem 1rem; margin: 1.25rem 0; }
.trees { display: flex; gap: 1rem; flex-wrap: wrap; }
.trees > div { flex: 1 1 18rem; min-width: 15rem; }
.tree { list-style: none; margin: 0; padding: 0; font-size: .84rem;
        font-family: ui-monospace, Consolas, monospace; }
.tree li { padding: .05rem 0; color: #3d4650; }
.d2 { padding-left: 1rem !important; }
.d3 { padding-left: 2rem !important; }
.d4 { padding-left: 3rem !important; }
.d5 { padding-left: 4rem !important; }
.d6 { padding-left: 5rem !important; }
.notes { margin: .6rem 0 0; padding-left: 1.1rem; font-size: .88rem; }
.unit { border-top: 1px solid #eceff2; padding: .7rem 0 .2rem; }
.uid { font-family: ui-monospace, Consolas, monospace; font-size: .82rem;
       color: #6b7684; }
.tag { display: inline-block; padding: .02rem .4rem; margin-right: .35rem;
       border: 1px solid #c7ced6; border-radius: 3px; font-size: .78rem;
       color: #2f3a45; background: #f2f4f6; font-family: inherit; }
.sec { color: #1b1f24; }
pre.raw { margin: .35rem 0; padding: .55rem .7rem; overflow-x: auto;
          background: #f7f8f9; border: 1px solid #e2e6ea; border-radius: 4px;
          font-size: .82rem; white-space: pre; }
.vals { margin: .35rem 0; padding-left: 1.1rem; font-size: .85rem; }
.exp { margin: .35rem 0 0; }
.miss { color: #8a94a0; font-size: .85rem; font-style: italic; }
.none { color: #6b7684; font-size: .88rem; }
ul.plain { list-style: none; margin: .3rem 0; padding: 0; font-size: .88rem; }
ul.plain li { padding: .1rem 0; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e9ec; background: #16181c; }
  h2 { border-bottom-color: #2c3238; }
  .ov, table, .file { background: #1d2025; border-color: #2c3238; }
  th { background: #22262c; }
  th, td { border-color: #2c3238; }
  pre.raw { background: #14171a; border-color: #2c3238; }
  .tag { background: #22262c; border-color: #39414a; color: #d5dae0; }
  .sec, .tree li { color: #cdd3d9; }
  .sub, .uid, .none, .trust { color: #98a2ad; }
}
"""


def _tree_html(rows: list[TreeSection]) -> str:
    if not rows:
        return '<p class="miss">헤딩 없음</p>'
    items = "".join(
        f'<li class="d{min(_depth(row.heading_path), 6)}">'
        f"{_e(_leaf(row.heading_path))}</li>"
        for row in rows
    )
    return f'<ul class="tree">{items}</ul>'


def _raw(label: str, body: str) -> str:
    if not body:
        return f'<p class="miss">{_e(label)} 원문 없음</p>'
    return f"<h5>{_e(label)}</h5><pre class=\"raw\">{_e(body)}</pre>"


def _values_html(
    unit: CollectedUnit, ops: tuple[str, ...] | None = None
) -> str:
    """Value/textual/prose rows of one unit, filtered to one operation.

    3.2 shows only what was added, 3.3 only what was deleted, 3.4 only what
    changed — a mixed unit contributes its part to each section instead of
    repeating every row three times.
    """

    items: list[str] = []
    if ops is None or "변경" in ops:
        items.extend(
            f"{_e(change.key)} {_e(change.from_value)} → {_e(change.to_value)}"
            for change in unit.changed
        )
        items.extend(
            f"표현 재작성: {_e(before)} → {_e(after)}"
            for before, after in unit.textual
        )
    if ops is None or "삭제" in ops:
        if unit.removed:
            items.append("삭제된 값: " + _e(", ".join(unit.removed)))
        items.extend("삭제된 문장: " + _e(text) for text in unit.prose_removed)
    if ops is None or "추가" in ops:
        if unit.added:
            items.append("추가된 값: " + _e(", ".join(unit.added)))
        items.extend("추가된 문장: " + _e(text) for text in unit.prose_added)
        items.extend("추가된 문장: " + _e(text) for text in unit.prose_added)
    if not items:
        return ""
    return '<ul class="vals">' + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _unit_html(
    unit: CollectedUnit,
    excerpt,
    *,
    sides: tuple[str, ...],
    note: str = "",
    ops: tuple[str, ...] | None = None,
) -> str:
    note_html = f' <span class="tag">{_e(note)}</span>' if note else ""
    parts = [
        '<div class="unit">',
        f'<p class="uid">'
        f'<span class="tag">[{_e(" · ".join(unit.axes) or unit.klass)}]</span>'
        f'{note_html}'
        f' · <span class="sec">{_e(unit.section)}</span></p>',
    ]
    for side in sides:
        if side == "before":
            parts.append(_raw("before", excerpt.before if excerpt else ""))
        else:
            parts.append(_raw("after", excerpt.after if excerpt else ""))
    parts.append(_values_html(unit, ops))
    parts.append(f'<p class="exp">설명 — {_e(unit.explanation)}</p>')
    parts.append("</div>")
    return "".join(parts)


def _file_html(
    entry: CollectedFile,
    units: tuple[CollectedUnit, ...],
    excerpts: dict[str, object],
    structure: Structure | None,
) -> str:
    parts = [
        '<div class="file">',
        f"<h3>{_e(entry.path)}</h3>",
        f'<p class="sub">{_e(entry.product_dir)} · 유닛 {entry.units}</p>',
        f"<p>{_e(entry.summary)}</p>",
        "<h4>3.1 구조 변화</h4>",
    ]
    if structure is None:
        parts.append('<p class="miss">05-structure 없음</p>')
    else:
        head_rows = _rows_of(structure.tree, entry.path)
        base_rows = _rows_of(structure.base_tree, entry.path)
        notes = structure_notes(structure, entry.path)
        parts.append(
            '<div class="trees">'
            f"<div><h5>before</h5>{_tree_html(base_rows)}</div>"
            f"<div><h5>after</h5>{_tree_html(head_rows)}</div>"
            "</div>"
        )
        parts.append(
            '<ul class="notes">'
            + "".join(f"<li>{_e(note)}</li>" for note in notes)
            + "</ul>"
            if notes
            else '<p class="none">구조 변화 없음</p>'
        )
    for title, whole_kind, sides, op in (
        ("3.2 추가된 내용", "added", ("after",), "추가"),
        ("3.3 삭제된 내용", "removed", ("before",), "삭제"),
        ("3.4 변경된 내용", "", ("before", "after"), "변경"),
    ):
        if whole_kind:
            whole = [unit for unit in units if unit.kind == whole_kind]
            partial = [unit for unit in units if unit.kind == "changed" and op in unit.ops]
        else:
            whole = [unit for unit in units if op in unit.ops]
            partial = []
        parts.append(f"<h4>{title}</h4>")
        if not whole and not partial:
            parts.append('<p class="none">없음</p>')
            continue
        for unit in whole:
            parts.append(
                _unit_html(
                    unit, excerpts.get(unit.excerpt_ref), sides=sides, ops=(op,)
                )
            )
        for unit in partial:
            parts.append(
                _unit_html(
                    unit,
                    excerpts.get(unit.excerpt_ref),
                    sides=sides,
                    note="부분",
                    ops=(op,),
                )
            )
    parts.append("</div>")
    return "".join(parts)


def _matrix_html(collect: Collect) -> str:
    if not collect.file_blocks:
        return '<p class="none">변경된 파일 없음</p>'
    head = (
        "<tr><th rowspan=\"2\">파일</th>"
        + "".join(f'<th colspan="3">{_e(axis)}</th>' for axis in _AXES)
        + "</tr><tr>"
        + "".join(f'<th class="n">{_e(op)}</th>' for _ in _AXES for op in _OPS)
        + "</tr>"
    )
    rows = []
    for entry in collect.file_blocks:
        cells = "".join(
            f'<td class="n">'
            f"{collect.matrix.get(entry.file_id, {}).get(axis, {}).get(op, 0)}</td>"
            for axis in _AXES
            for op in _OPS
        )
        rows.append(
            f'<tr><td title="{_e(entry.path)}">{_e(_basename(entry.path))}</td>'
            f"{cells}</tr>"
        )
    return (
        '<div class="wrap"><table><thead>'
        + head
        + "</thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _appendix_html(
    collect: Collect, units: tuple[CollectedUnit, ...], dropped: int
) -> str:
    criteria = "".join(
        f"<tr><td>{_e(name)}</td><td>{_e(rule)}</td></tr>" for name, rule in CRITERIA
    )
    mapping = "".join(
        f"<tr><td>{_e(unit.unit_id)}</td><td>{_e(unit.section)}</td>"
        f"<td>{_e(unit.excerpt_ref or '-')}</td></tr>"
        for unit in units
    )
    uncertain: list[str] = [
        f"{unit.unit_id} — {classes.UNCLASSIFIED}"
        for unit in units
        if classes.UNCLASSIFIED in unit.axes or unit.klass == classes.UNCLASSIFIED
    ]
    uncertain += [
        f"{unit.unit_id} — 금지 어휘: {' · '.join(unit.vocab_violation)}"
        for unit in units
        if unit.vocab_violation
    ]
    uncertain += [str(item) for item in collect.uncertain]
    outstanding = collect.verify.get("outstanding", 0)
    if outstanding:
        uncertain.append(f"지적 잔존 {outstanding}건")
    partial: list[str] = [f"{name} — 20-analysis 읽기 실패" for name in collect.failed_files]
    partial += [
        f"{entry.path} — {SUMMARY_FAILED}"
        for entry in collect.file_blocks
        if entry.summary == SUMMARY_FAILED
    ]
    partial += [
        f"{unit.unit_id} — {EXPLANATION_FAILED}"
        for unit in units
        if unit.explanation == EXPLANATION_FAILED
    ]
    total_dropped = collect.refs_dropped + dropped
    if total_dropped:
        partial.append(f"refs 드롭 {total_dropped}건")

    def listing(items: list[str]) -> str:
        if not items:
            return '<p class="none">없음</p>'
        return (
            '<ul class="plain">'
            + "".join(f"<li>{_e(item)}</li>" for item in items)
            + "</ul>"
        )

    return "".join(
        [
            "<h4>집계 기준</h4>",
            "<p>카운트는 원자 단위로 센다 — 헤딩 변화 1건 · 값 1개 · 표현 재작성 1건 · "
            "문장 묶음 1건이 각각 1로 센다. 추가·삭제 유닛에 병기된 값은 정보용이며 "
            "카운트에는 넣지 않는다.</p>",
            "<h4>분류 기준표</h4>",
            '<div class="wrap"><table><thead><tr><th>성질</th><th>기준</th></tr>'
            "</thead><tbody>" + criteria + "</tbody></table></div>",
            "<h4>유닛 ID · 소스 대응</h4>",
            (
                '<div class="wrap"><table><thead><tr><th>unit_id</th><th>section</th>'
                "<th>excerpt_ref</th></tr></thead><tbody>"
                + mapping
                + "</tbody></table></div>"
                if mapping
                else '<p class="none">없음</p>'
            ),
            "<h4>분류 불확실 항목</h4>",
            listing(uncertain),
            "<h4>부분 실패 목록</h4>",
            listing(partial),
        ]
    )


def render_report_html(
    collect: Collect,
    *,
    mr_iid: int,
    excerpts: Excerpts | None = None,
    structure: Structure | None = None,
    base_sha: str = "",
    head_sha: str = "",
    diff_url: str = "",
) -> str:
    """One self-contained page — 4 sections, inline CSS, no external calls."""

    units, dropped = gate(collect)
    by_ref = {unit.unit_id: unit for unit in (excerpts.units if excerpts else ())}
    lines = overview_lines(
        collect, mr_iid=mr_iid, base_sha=base_sha, head_sha=head_sha
    )
    body = "".join(f"<p>{_e(line)}</p>" for line in lines[:-1])
    body += f'<p class="trust">{_e(lines[-1])}</p>'
    link = f' · <a href="{_e(diff_url)}">MR diff</a>' if diff_url else ""
    files_html = "".join(
        _file_html(
            entry,
            tuple(unit for unit in units if unit.file == entry.file_id),
            by_ref,
            structure,
        )
        for entry in collect.file_blocks
    )
    return "".join(
        [
            "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">",
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>mrdoc — MR !{_e(mr_iid)}</title><style>{_CSS}</style>",
            "</head><body>",
            f"<h1>mrdoc 문서 변경 리포트 — MR !{_e(mr_iid)}</h1>",
            f'<p class="sub">문서 변경 설명 리포트{link}</p>',
            '<h2>1. 개요</h2><div class="ov">' + body + "</div>",
            "<h2>2. 변경 매트릭스</h2>" + _matrix_html(collect),
            "<h2>3. 파일별 상세</h2>"
            + (files_html or '<p class="none">변경된 파일 없음</p>'),
            "<h2>4. 부록</h2>" + _appendix_html(collect, units, dropped),
            "</body></html>",
        ]
    )
