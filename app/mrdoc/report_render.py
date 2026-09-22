"""report.html + slack-summary.txt — one refs gate, then one self-contained page.

The design's three render steps: (1) every block's refs must exist in
50-collect, otherwise the block is dropped, (2) the body comes from
50-collect — the renderer never recounts and never rewrites, (3) out come
report.html (single file, inline CSS, zero external dependencies) and
slack-summary.txt (the overview verbatim, no document body).

The page layout follows docs/mrdoc-report-template.html (navy-indigo /
matrix-first): hero · stats · 01 변경 매트릭스 · 02 핵심 변경사항 ·
03 확인이 필요한 항목 · 04 파일별 상세 · 05 분석 상태+부록. The CSS lives
in report_style.css next to this module so the template stays a file.

Nothing here judges — 판정 · 심각도 · 머지 의견이 모두 없다. Structure,
raw text and literal values come from the tool artifacts, so a run whose
설명 all failed still renders every one of them; only the prose says so.

Hand-rolled HTML with escaping — Jinja2 would be the pipeline's first
template dependency for a page this small.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from . import classes
from .collect import (
    _CONF_KEYS,
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
_UNIT_ID = re.compile(r"u-[0-9a-f]{8}")

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


def _strip_ids(text: str) -> str:
    """Remove u- hash ids from prose — the appendix table owns them.

    The measured complaint: 산문에 u-a0c3709c 같은 해시가 섞여 읽힌다.
    Ids stay in the appendix mapping (and nowhere else), so prose lines
    drop them here — "(u-x, head 1-2)" becomes "(head 1-2)" and an id that
    leaves an empty parenthesis takes the parenthesis with it.
    """

    out = _UNIT_ID.sub("", text)
    out = re.sub(r"\(\s*,\s*", "(", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


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
# the overview — shared with slack-summary.txt
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
    """The overview as plain text — the one source slack-summary reads."""

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
        lines.append(f"{_basename(entry.path)}: {_strip_ids(entry.summary)}")
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
    """slack-summary.txt — the overview and nothing else.

    The document body never reaches Slack; the overview does, 리포트 신뢰도
    included, because a partial failure disappearing quietly is this
    pipeline's worst failure mode.
    """

    lines = overview_lines(
        collect, mr_iid=mr_iid, base_sha=base_sha, head_sha=head_sha
    )
    if collect.themes:
        top = sorted(collect.themes, key=lambda t: (-len(t.units), t.theme_id))[:3]
        theme_line = "주요 주제: " + " · ".join(theme.title for theme in top)
        lines = [*lines[:-1], theme_line, lines[-1]]
    if link:
        lines.append(link)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# structure notes, generated from the two trees
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
# the page — navy-indigo / matrix-first (docs/mrdoc-report-template.html)
# --------------------------------------------------------------------------

_CSS = Path(__file__).with_name("report_style.css").read_text(encoding="utf-8")


def _pipeline_issues(
    collect: Collect, units: tuple[CollectedUnit, ...], dropped: int
) -> tuple[int, list[str]]:
    """Prose-failure count plus the warning reasons — one pass, two consumers."""

    prose_failures = len(_prose_failures(collect, units))
    reasons: list[str] = []
    outstanding = int(collect.verify.get("outstanding", 0) or 0)
    if outstanding:
        reasons.append(f"지적 잔존 {outstanding}")
    mismatch = int(collect.verify.get("counts_mismatch", 0) or 0)
    if mismatch:
        reasons.append(f"집계 불일치 {mismatch}")
    unclassified = int(collect.classes.get(classes.UNCLASSIFIED, 0) or 0)
    if unclassified:
        reasons.append(f"분류 불확실 {unclassified}")
    if prose_failures:
        reasons.append(f"설명 생성 실패 파일 {prose_failures}")
    refs = collect.refs_dropped + dropped
    if refs:
        reasons.append(f"refs 드롭 {refs}")
    if collect.themes_dropped:
        reasons.append(f"주제 드롭 {collect.themes_dropped}")
    if collect.themes_failed:
        reasons.append("주제 생성 실패")
    return prose_failures, reasons


def _hero_html(
    collect: Collect, mr_iid: int, base_sha: str, head_sha: str, warning: bool
) -> str:
    dirs = sorted({entry.product_dir for entry in collect.file_blocks})
    scope = " · ".join(dirs) if dirs else "(없음)"
    status = (
        '<div class="status"><i></i> PIPELINE WARNING</div>'
        if warning
        else '<div class="status ok"><i></i> ANALYSIS OK</div>'
    )
    hashline = ""
    if base_sha and head_sha:
        hashline = (
            f'<span>base <span class="hash">{_e(base_sha)}</span></span>'
            "<span>→</span>"
            f'<span>head <span class="hash">{_e(head_sha)}</span></span>'
            "<span>·</span>"
            f"<span>{_e(scope)}</span>"
        )
    return (
        '<section class="hero"><div class="hero-row"><div>'
        '<div class="eyebrow">DOCUMENT CHANGE REPORT</div>'
        f"<h1>MR !{_e(mr_iid)}</h1>"
        f'<p class="hero-sub">{_e(scope)} 문서 변경을 '
        "구조 · 의미 · 표현 단위로 분석한 리포트</p>"
        f"</div>{status}</div>"
        f'<div class="hashline">{hashline}</div>'
        "</section>"
    )


def _stats_html(
    collect: Collect,
    units: tuple[CollectedUnit, ...],
    warning: bool,
    reasons: list[str],
) -> str:
    analysis_value = (
        '<div class="value" style="font-size:24px;color:#ffd77e">WARN</div>'
        if warning
        else '<div class="value" style="font-size:24px;color:#8fe4aa">OK</div>'
    )
    cards = [
        ("Files", str(len(collect.file_blocks)), "affected documents"),
        ("Units", str(len(units)), "analyzed changes"),
        (
            "Semantic",
            str(collect.classes.get(classes.MEANING, 0)),
            f"{collect.value_changes} value-level changes",
        ),
    ]
    parts = [
        f'<div class="stat"><div class="label">{_e(label)}</div>'
        f'<div class="value">{_e(value)}</div>'
        f'<div class="subv">{_e(sub)}</div></div>'
        for label, value, sub in cards
    ]
    parts.append(
        '<div class="stat"><div class="label">Analysis</div>'
        + analysis_value
        + f'<div class="subv">{_e(reasons[0] if reasons else "all checks passed")}</div>'
        + "</div>"
    )
    return (
        '<section aria-label="MR summary" class="stats">'
        + "".join(parts)
        + "</section>"
    )


def _section(num: str, en: str, ko: str, note: str, body: str, sid: str = "") -> str:
    id_attr = f' id="{_e(sid)}"' if sid else ""
    return (
        f'<section class="section"{id_attr}><div class="section-head">'
        f'<div><div class="section-num">{_e(num)} / {_e(en)}</div>'
        f"<h2>{_e(ko)}</h2></div>"
        f'<div class="section-note">{_e(note)}</div></div>'
        f"{body}</section>"
    )


def _matrix_html(collect: Collect) -> str:
    if not collect.file_blocks:
        return '<p class="none">변경된 파일 없음</p>'
    head = (
        '<tr><th rowspan="2">파일</th>'
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
        '<div class="data-card"><div class="wrap"><table><thead>'
        + head
        + "</thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></div>"
    )


def _dominant_axis(members: list[CollectedUnit]) -> str:
    """The axis a theme's members touch most — ties break to axis order."""

    tally = dict.fromkeys(_AXES, 0)
    for member in members:
        for axis in member.axes or (member.klass,):
            if axis in tally:
                tally[axis] += 1
    best = max(_AXES, key=lambda axis: (tally[axis], -_AXES.index(axis)))
    return best if tally[best] else ""


def _theme_delta(members: list[CollectedUnit], dominant: str) -> str:
    """The card's one-line delta — value change or section count.

    A 구조-dominant theme leads with how many sections came or went; any
    other theme leads with its first value change. The loser is the
    fallback, so a structural theme that also changed a value still shows
    the value when no whole section moved.
    """

    def value_delta() -> str:
        for member in members:
            for change in member.changed:
                return (
                    '<div class="delta">'
                    f'<span class="old">{_e(change.from_value)}</span>'
                    '<span class="arrow">→</span>'
                    f'<span class="new">{_e(change.to_value)}</span>'
                    "</div>"
                )
        return ""

    def section_delta() -> str:
        adds = sum(1 for member in members if member.kind == "added")
        dels = sum(1 for member in members if member.kind == "removed")
        if adds >= dels and adds:
            return f'<div class="delta"><span class="new">+ {adds}개 섹션</span></div>'
        if dels:
            return f'<div class="delta"><span class="old">- {dels}개 섹션</span></div>'
        return ""

    if dominant == classes.STRUCTURE:
        return section_delta() or value_delta()
    return value_delta() or section_delta()


def _keys_html(collect: Collect, units: tuple[CollectedUnit, ...]) -> str:
    """02 핵심 변경사항 — the themes satellite's grouping as key-cards.

    The satellite writes a title, one line and member ids; the tag, delta
    and file/unit counts are measured here from the members, never trusted
    from prose. Member ids stay out — the appendix's 주제 · 유닛 대응 table
    is where they live.
    """

    if not collect.themes:
        if collect.themes_failed:
            return '<p class="none">주제 생성 실패 — 05 분석 상태 참조</p>'
        return '<p class="none">주제 없음</p>'
    by_unit = {unit.unit_id: unit for unit in units}
    path_of = {entry.file_id: entry.path for entry in collect.file_blocks}
    cards: list[str] = []
    for theme in sorted(collect.themes, key=lambda t: (-len(t.units), t.theme_id)):
        members = [by_unit[u] for u in theme.units if u in by_unit]
        files = sorted({path_of.get(m.file, m.file) for m in members})
        dominant = _dominant_axis(members)
        cards.append(
            '<div class="key-card">'
            f'<div class="k-tag">{_e(dominant or "변경")}</div>'
            f"<strong>{_e(theme.title)}</strong>"
            f"<p>{_e(_strip_ids(theme.line))}</p>"
            + _theme_delta(members, dominant)
            + f'<div class="k-meta">유닛 {len(theme.units)} · 파일 {len(files)}</div>'
            + "</div>"
        )
    return '<div class="key-grid">' + "".join(cards) + "</div>"


def _attention_html(
    collect: Collect, units: tuple[CollectedUnit, ...], dropped: int
) -> str:
    """03 확인이 필요한 항목 — amber for reader-check items, blue for pipeline.

    The template separates MR-content issues from pipeline state; the data
    we actually have is pipeline state, so amber carries what a reader must
    re-check by eye (분류 불확실 · 검증 지적 · 집계 불일치) and blue carries
    degradation the run absorbed (드롭 · 생성 실패).
    """

    prose_failures = len(_prose_failures(collect, units))
    primary: list[tuple[str, str]] = []
    secondary: list[tuple[str, str]] = []
    outstanding = int(collect.verify.get("outstanding", 0) or 0)
    if outstanding:
        primary.append(
            (
                f"검증 지적 {outstanding}건 잔존",
                "verifier의 재분석 요구가 마지막 라운드까지 남았다 — 해당 유닛의 "
                "설명을 사람이 직접 확인해야 한다.",
            )
        )
    mismatch = int(collect.verify.get("counts_mismatch", 0) or 0)
    if mismatch:
        primary.append(
            (
                f"집계 불일치 {mismatch}건",
                "파일 요약이 말한 카운트와 원자 집계가 어긋난다 — 05 분석 상태의 "
                "표와 함께 확인.",
            )
        )
    unclassified = int(collect.classes.get(classes.UNCLASSIFIED, 0) or 0)
    if unclassified:
        primary.append(
            (
                f"분류 불확실 {unclassified}유닛",
                "축 분류가 확정되지 않은 유닛이 있다 — 05 분석 상태의 분류 불확실 "
                "항목 참조.",
            )
        )
    if collect.themes_dropped:
        secondary.append(
            (
                f"주제 드롭 {collect.themes_dropped}",
                "원자 단위 분석은 완료됐지만 상위 주제 묶음 일부가 멤버 게이트에서 "
                "제외됐다.",
            )
        )
    if collect.themes_failed:
        secondary.append(
            (
                "주제 생성 실패",
                "themes 위성 산출물을 읽지 못해 02 섹션이 주제 없이 렌더링됐다.",
            )
        )
    if prose_failures:
        secondary.append(
            (
                f"설명 생성 실패 파일 {prose_failures}",
                "해당 파일의 설명·요약이 비어 있다 — 원문 · 값 · 구조 정보는 정상 "
                "표기된다.",
            )
        )
    refs = collect.refs_dropped + dropped
    if refs:
        secondary.append(
            (
                f"refs 드롭 {refs}",
                "알 수 없는 파일을 가리키는 블록이 refs 게이트에서 제외됐다.",
            )
        )
    cards = [
        '<div class="alert"><div class="alert-top">'
        f'<div class="alert-icon">!</div><h3>{_e(title)}</h3></div>'
        f"<p>{_e(body)}</p></div>"
        for title, body in primary
    ]
    cards += [
        '<div class="alert secondary"><div class="alert-top">'
        f'<div class="alert-icon">i</div><h3>{_e(title)}</h3></div>'
        f"<p>{_e(body)}</p></div>"
        for title, body in secondary
    ]
    if not cards:
        cards.append(
            '<div class="alert secondary"><div class="alert-top">'
            '<div class="alert-icon">✓</div><h3>확인이 필요한 항목 없음</h3></div>'
            "<p>모든 파이프라인 지표가 정상 범위다.</p></div>"
        )
    return '<div class="alert-grid">' + "".join(cards) + "</div>"


def _tree_html(rows: list[TreeSection]) -> str:
    if not rows:
        return '<p class="miss">헤딩 없음</p>'
    items = "".join(
        f'<li class="d{min(_depth(row.heading_path), 6)}">'
        f"{_e(_leaf(row.heading_path))}</li>"
        for row in rows
    )
    return f'<ul class="tree">{items}</ul>'


def _value_items(
    unit: CollectedUnit, ops: tuple[str, ...] | None = None
) -> list[str]:
    """Value/textual/prose rows of one unit, filtered to one operation.

    A mixed unit contributes its part to each card instead of repeating
    every row — the 추가 card shows only what was added, and so on.
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
    return items


def _change_entries(
    units: tuple[CollectedUnit, ...],
) -> list[tuple[CollectedUnit, tuple[str, ...], str, tuple[str, ...]]]:
    """Flatten the op groups into one stack — 추가 → 삭제 → 변경.

    A changed-kind unit that also added or deleted text appears once per
    operation it touches, marked 부분 — the same semantics the old 4.2/4.3/
    4.4 split had, without the subsection headers.
    """

    entries: list[tuple[CollectedUnit, tuple[str, ...], str, tuple[str, ...]]] = []
    for whole_kind, sides, op in (
        ("added", ("after",), "추가"),
        ("removed", ("before",), "삭제"),
        ("", ("before", "after"), "변경"),
    ):
        if whole_kind:
            whole = [unit for unit in units if unit.kind == whole_kind]
            partial = [
                unit for unit in units if unit.kind == "changed" and op in unit.ops
            ]
        else:
            whole = [unit for unit in units if op in unit.ops]
            partial = []
        entries.extend((unit, sides, "", (op,)) for unit in whole)
        entries.extend((unit, sides, "부분", (op,)) for unit in partial)
    return entries


def _change_card(
    unit: CollectedUnit,
    excerpt,
    index: int,
    sides: tuple[str, ...],
    note: str,
    ops: tuple[str, ...],
) -> str:
    axes = tuple(axis for axis in (unit.axes or (unit.klass,)) if axis)
    tag_text = " · ".join(axes) or unit.klass
    pills = f'<span class="pill tag-{_e("-".join(axes))}">[{_e(tag_text)}]</span>'
    if note:
        pills += f'<span class="pill tag-부분">{_e(note)}</span>'
    panes: list[str] = []
    for side in sides:
        body = (excerpt.before if side == "before" else excerpt.after) if excerpt else ""
        if not body:
            continue
        pane_cls = "before-pane" if side == "before" else "after-pane"
        panes.append(
            f'<div class="diff-pane {pane_cls}">'
            f'<div class="diff-label">{side.upper()}</div>'
            f"<pre>{_e(body)}</pre></div>"
        )
    if panes:
        single = " single" if len(panes) == 1 else ""
        grid = f'<div class="diff-grid{single}">' + "".join(panes) + "</div>"
    else:
        grid = '<p class="miss">원문 없음</p>'
    values = _value_items(unit, ops)
    value_box = ""
    if values:
        value_box = (
            '<div class="value-box"><div class="value-label">Detected values</div>'
            "<ul>" + "".join(f"<li>{item}</li>" for item in values) + "</ul></div>"
        )
    open_attr = " open" if index == 1 else ""
    return (
        f'<details class="change-card"{open_attr}><summary>'
        '<div class="change-summary-main">'
        f'<div class="change-kicker">CHANGE {index:02d}</div>'
        f'<div class="change-title">{_e(unit.section)}</div></div>'
        f'<div class="change-tags">{pills}</div></summary>'
        '<div class="change-body">'
        + grid
        + value_box
        + '<div class="explain"><div class="explain-label">WHAT CHANGED</div>'
        + f"<p>설명 — {_e(_strip_ids(unit.explanation))}</p></div>"
        + "</div></details>"
    )


def _file_html(
    entry: CollectedFile,
    units: tuple[CollectedUnit, ...],
    excerpts: dict[str, object],
    structure: Structure | None,
    index: int,
) -> str:
    entries = _change_entries(units)
    parts = [
        f'<article class="file-card" id="file-{index}">',
        '<header class="file-head"><div>',
        f'<div class="eyebrow">FILE {index:02d}</div>',
        f"<h3>{_e(entry.path)}</h3>",
        f'<p class="pathline">{_e(entry.product_dir)} · 유닛 {len(units)}</p>',
        "</div>",
        f'<div class="file-count">{len(entries)}<span>changes</span></div>',
        "</header>",
        f'<p class="file-summary">{_e(_strip_ids(entry.summary))}</p>',
    ]
    if structure is not None:
        notes = structure_notes(structure, entry.path)
        if notes:
            head_rows = _rows_of(structure.tree, entry.path)
            base_rows = _rows_of(structure.base_tree, entry.path)
            parts.append(
                '<details class="structure-card"><summary>'
                "<span>Document structure change</span>"
                '<span class="muted">before / after outline</span>'
                '</summary><div class="structure-body"><div class="trees">'
                f"<div><h5>before</h5>{_tree_html(base_rows)}</div>"
                f"<div><h5>after</h5>{_tree_html(head_rows)}</div>"
                "</div>"
                '<ul class="notes">'
                + "".join(f"<li>{_e(note)}</li>" for note in notes)
                + "</ul></div></details>"
            )
    parts.append('<div class="change-stack">')
    for number, (unit, sides, note, ops) in enumerate(entries, 1):
        parts.append(
            _change_card(
                unit, excerpts.get(unit.excerpt_ref), number, sides, note, ops
            )
        )
    parts.append("</div></article>")
    return "".join(parts)


def _status_panel(
    collect: Collect,
    units: tuple[CollectedUnit, ...],
    dropped: int,
    prose_failures: int,
    warning: bool,
) -> str:
    """The pipeline's own bookkeeping, WARNING badge when degraded."""

    rows: list[tuple[str, str]] = [
        ("검증 라운드", str(collect.verify.get("rounds", 0))),
        ("지적 잔존", str(collect.verify.get("outstanding", 0))),
        ("집계 불일치", str(collect.verify.get("counts_mismatch", 0))),
        ("설명 생성 실패 파일", str(prose_failures)),
        ("분류 불확실", str(collect.classes.get(classes.UNCLASSIFIED, 0))),
        ("refs 드롭", str(collect.refs_dropped + dropped)),
        ("주제 드롭", str(collect.themes_dropped)),
        ("주제 생성 실패", "있음" if collect.themes_failed else "없음"),
    ]
    confidence = " · ".join(
        f"{key} {collect.confidence_dist.get(key, 0)}"
        for key in _CONF_KEYS
        if collect.confidence_dist.get(key, 0)
    )
    if confidence:
        rows.append(("신뢰도 분포", confidence))
    badge = '<span class="badge">WARNING</span>' if warning else ""
    table = "".join(
        f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>" for key, value in rows
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
    partial: list[str] = [
        f"{name} — 20-analysis 읽기 실패" for name in collect.failed_files
    ]
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

    def listing(items: list[str]) -> str:
        if not items:
            return '<p class="none">없음</p>'
        return (
            '<ul class="plain">'
            + "".join(f"<li>{_e(item)}</li>" for item in items)
            + "</ul>"
        )

    trustbar = ""
    if units and any(collect.confidence_dist.get(key, 0) for key in _CONF_KEYS):
        high = collect.confidence_dist.get("high", 0)
        pct = min(100, round(100 * high / len(units)))
        trustbar = (
            f'<div class="trustbar" '
            f'title="high confidence {_e(high)}/{_e(len(units))}">'
            f'<span style="width:{pct}%"></span></div>'
        )
    return "".join(
        [
            '<div class="analysis-panel">',
            f"<p>파이프라인 상태{badge}</p>",
            '<div class="wrap"><table><thead><tr><th>항목</th><th>값</th></tr>'
            "</thead><tbody>" + table + "</tbody></table></div>",
            "<h4>분류 불확실 항목</h4>",
            listing(uncertain),
            "<h4>부분 실패 목록</h4>",
            listing(partial),
            trustbar,
            "</div>",
        ]
    )


def _appendix_panel(collect: Collect, units: tuple[CollectedUnit, ...]) -> str:
    """The criteria and the id mappings prose never carries."""

    criteria = "".join(
        f"<tr><td>{_e(name)}</td><td>{_e(rule)}</td></tr>" for name, rule in CRITERIA
    )
    mapping = "".join(
        f"<tr><td>{_e(unit.unit_id)}</td><td>{_e(unit.section)}</td>"
        f"<td>{_e(unit.excerpt_ref or '-')}</td></tr>"
        for unit in units
    )
    theme_rows = "".join(
        f"<tr><td>{_e(theme.theme_id)}</td><td>{_e(theme.title)}</td>"
        f"<td>{_e(', '.join(theme.units))}</td></tr>"
        for theme in collect.themes
    )
    return "".join(
        [
            '<div class="appendix-panel">',
            '<div class="eyebrow">REFERENCE</div>',
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
            "<h4>주제 · 유닛 대응</h4>",
            (
                '<div class="wrap"><table><thead><tr><th>theme_id</th>'
                "<th>주제</th><th>멤버 유닛</th></tr></thead><tbody>"
                + theme_rows
                + "</tbody></table></div>"
                if theme_rows
                else '<p class="none">없음</p>'
            ),
            "</div>",
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
    """One self-contained page — hero + 5 sections, inline CSS, no calls."""

    units, dropped = gate(collect)
    by_ref = {unit.unit_id: unit for unit in (excerpts.units if excerpts else ())}
    prose_failures, reasons = _pipeline_issues(collect, units, dropped)
    warning = bool(reasons)
    link = (
        f'<a href="{_e(diff_url)}">MR diff</a>' if diff_url else ""
    )
    files_html = "".join(
        _file_html(
            entry,
            tuple(unit for unit in units if unit.file == entry.file_id),
            by_ref,
            structure,
            index,
        )
        for index, entry in enumerate(collect.file_blocks, 1)
    )
    return "".join(
        [
            '<!doctype html><html lang="ko"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>MR !{_e(mr_iid)} — Document Change Intelligence</title>"
            f"<style>{_CSS}</style>",
            "</head><body>",
            '<main class="page">',
            '<div class="topbar"><div class="brand">MRDOC / CHANGE INTELLIGENCE</div>'
            '<nav class="toplinks"><a href="#matrix">Matrix</a>'
            '<a href="#changes">Changes</a><a href="#files">Files</a>'
            '<a href="#quality">Quality</a></nav></div>',
            _hero_html(collect, mr_iid, base_sha, head_sha, warning),
            _stats_html(collect, units, warning, reasons),
            _section(
                "01",
                "CHANGE MATRIX",
                "변경 매트릭스",
                "상세를 읽기 전에, MR 전체 변화의 분포부터 확인",
                _matrix_html(collect),
                "matrix",
            ),
            _section(
                "02",
                "WHAT CHANGED",
                "핵심 변경사항",
                "Diff보다 먼저, 사람이 알아야 할 변화부터",
                _keys_html(collect, units),
                "changes",
            ),
            _section(
                "03",
                "ATTENTION",
                "확인이 필요한 항목",
                "MR 내용 이슈와 분석 파이프라인 상태를 분리",
                _attention_html(collect, units, dropped),
            ),
            _section(
                "04",
                "FILE CHANGES",
                "파일별 상세",
                "각 변경 카드를 펼치면 Before / After와 설명을 확인할 수 있음",
                '<div class="file-list">'
                + (files_html or '<p class="none">변경된 파일 없음</p>')
                + "</div>",
                "files",
            ),
            _section(
                "05",
                "ANALYSIS QUALITY",
                "분석 상태",
                "MR 문제와 분석 파이프라인 품질을 별도 확인"
                + (f" · {link}" if link else ""),
                '<div class="analysis-wrap">'
                + _status_panel(collect, units, dropped, prose_failures, warning)
                + _appendix_panel(collect, units)
                + "</div>",
                "quality",
            ),
            "<footer>"
            f"<span>mrdoc — MR !{_e(mr_iid)} · navy-indigo / matrix-first</span>"
            "<span>Evidence-first · progressive disclosure · navy-indigo theme</span>"
            "</footer>",
            "</main></body></html>",
        ]
    )
