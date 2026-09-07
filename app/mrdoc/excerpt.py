"""07-excerpts node — the one artifact that carries document text.

Every other artifact carries coordinates, values or counts; this one carries
the before/after the report prints and the analyzer reads. That is the point:
in the measured run the LLM transcribed the excerpts itself and interpretation
leaked into them ("과거 시점 값으로 되어 있다"), so the transcription path is
removed — the tool cuts the text, the LLM only explains it.

Cuts follow the design: a changed unit takes (hunk ranges ∩ section range)
plus one line of context on each side, clamped to the section so an excerpt
never spills into its neighbour; an added unit takes the whole head section,
a removed unit the whole base section. Several hunks inside one section stay
one excerpt, their segments joined by an ellipsis line. A cut that lands
inside a fenced code block grows to hold the whole block — half a fence is
not raw text, it is broken markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .changeset import Changeset
from .frontmatter import parse_frontmatter, render_frontmatter, render_section
from .structure import Structure, TreeSection

_TICK = chr(96)
_TICK_RUN = re.compile(_TICK + "+")
_FENCE = re.compile(r"^ {0,3}(" + _TICK + r"{3,}|~{3,})")
ELLIPSIS = "…"
DEFAULT_CONTEXT = 1
#: ChangeUnit.kind speaks the operation axis; 07 speaks the excerpt's shape.
_KINDS = {"modified": "changed", "added": "added", "removed": "removed"}

Range = tuple[int, int]


@dataclass(frozen=True)
class Excerpt:
    """One unit's raw text on both sides, with the lines it was cut from."""

    unit_id: str
    kind: str  # changed | added | removed
    file_id: str
    section: str  # "<basename> § <heading>" — what the report prints
    base_lines: tuple[Range, ...]
    head_lines: tuple[Range, ...]
    context: int
    before: str
    after: str


@dataclass(frozen=True)
class Excerpts:
    mr_iid: int
    units: tuple[Excerpt, ...]

    @property
    def files(self) -> int:
        return len({unit.file_id for unit in self.units})


def _intersect(ranges: tuple[Range, ...], window: Range) -> list[Range]:
    return [
        (max(start, window[0]), min(end, window[1]))
        for start, end in ranges
        if start <= window[1] and window[0] <= end
    ]


def _merge(segments: list[Range]) -> tuple[Range, ...]:
    """Sort, then fuse overlapping or line-adjacent segments."""

    merged: list[Range] = []
    for start, end in sorted(segments):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _fence_spans(text: str) -> list[Range]:
    """1-based (open line, close line) for every fenced block in the file.

    A block never contains a heading — the section parser hides fenced
    content — so every span lies inside exactly one section, which is what
    lets a cut absorb a whole block without leaving its section.
    """

    spans: list[Range] = []
    open_line = 0
    fence_char: str | None = None
    fence_len = 0
    lines = text.splitlines()
    for index, line in enumerate(lines, start=1):
        match = _FENCE.match(line)
        if fence_char is None:
            if match:
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                open_line = index
            continue
        stripped = line.strip()
        if (
            match
            and match.group(1)[0] == fence_char
            and len(match.group(1)) >= fence_len
            and set(stripped) == {fence_char}
        ):
            spans.append((open_line, index))
            fence_char = None
    if fence_char is not None:  # unterminated fence runs to end of file
        spans.append((open_line, len(lines)))
    return spans


def _snap_to_fences(segment: Range, spans: list[Range]) -> Range:
    """Grow a cut that starts or ends inside a fenced block to hold it whole.

    A window cut mid-block ships an opening ``` with no closing one; the
    excerpt then reads as broken markdown and its own outer fence has to
    guess a length. Absorbing the block is cheaper than explaining it.
    """

    start, end = segment
    for open_line, close_line in spans:
        if start <= close_line and open_line <= end:
            start = min(start, open_line)
            end = max(end, close_line)
    return (start, end)


def _cut(
    ranges: tuple[Range, ...],
    window: Range | None,
    context: int,
    spans: list[Range],
) -> tuple[Range, ...]:
    """Hunk ranges ∩ section, padded by context, clamped, fence-snapped.

    An empty intersection falls back to the whole section: a modified file
    the diff gave no hunk extents for still owes the report its text.
    """

    if window is None:
        return ()
    hit = _intersect(ranges, window)
    if not hit:
        return (window,)
    return _merge(
        [
            _snap_to_fences(
                (max(start - context, window[0]), min(end + context, window[1])),
                spans,
            )
            for start, end in hit
        ]
    )


def _text(tree: dict[str, str], path: str | None, segments: tuple[Range, ...]) -> str:
    """The segments' raw lines, joined by an ellipsis line between cuts."""

    if path is None or path not in tree or not segments:
        return ""
    lines = tree[path].splitlines()
    blocks = ["\n".join(lines[start - 1 : end]) for start, end in segments]
    return ("\n" + ELLIPSIS + "\n").join(blocks)


def _label(rows: tuple[TreeSection, ...], section_id: str) -> str:
    """'<basename> § <leaf heading>' — the design's human section address."""

    for row in rows:
        if row.section_id != section_id:
            continue
        basename = row.file.replace(chr(92), "/").rsplit("/", 1)[-1]
        return f"{basename} § {row.heading_path.rsplit(' > ', 1)[-1]}"
    return section_id


def build_excerpts(
    structure: Structure,
    changeset: Changeset,
    base_tree: dict[str, str],
    head_tree: dict[str, str],
    *,
    mr_iid: int = 0,
    context: int = DEFAULT_CONTEXT,
) -> Excerpts:
    """Cut every change unit's before/after out of the two checked-out trees."""

    entries = {entry.fid: entry for entry in changeset.files}
    head_paths = {row.section_id: row.file for row in structure.tree}
    rows = structure.tree + structure.base_tree
    cache: dict[tuple[int, str], list[Range]] = {}

    def fences(tree: dict[str, str], path: str | None) -> list[Range]:
        if path is None or path not in tree:
            return []
        key = (id(tree), path)
        if key not in cache:
            cache[key] = _fence_spans(tree[path])
        return cache[key]

    units: list[Excerpt] = []
    for unit in structure.changed:
        entry = entries.get(unit.file_id)
        base_path = (entry.old_path or entry.path) if entry else None
        head_path = head_paths.get(unit.section_id)
        kind = _KINDS[unit.kind]
        whole = kind in ("added", "removed")
        base_segments = (
            (unit.old_lines,)
            if whole and unit.old_lines
            else _cut(
                entry.old_ranges if entry else (),
                unit.old_lines,
                context,
                fences(base_tree, base_path),
            )
        )
        head_segments = (
            (unit.new_lines,)
            if whole and unit.new_lines
            else _cut(
                entry.new_ranges if entry else (),
                unit.new_lines,
                context,
                fences(head_tree, head_path),
            )
        )
        units.append(
            Excerpt(
                unit_id=unit.unit_id,
                kind=kind,
                file_id=unit.file_id,
                section=_label(rows, unit.section_id),
                base_lines=base_segments,
                head_lines=head_segments,
                context=0 if whole else context,
                before=_text(base_tree, base_path, base_segments),
                after=_text(head_tree, head_path, head_segments),
            )
        )
    return Excerpts(mr_iid=mr_iid, units=tuple(units))


def _render_ranges(ranges: tuple[Range, ...]) -> str | None:
    if not ranges:
        return None
    return ",".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def _parse_ranges(value: object) -> tuple[Range, ...]:
    if value is None:
        return ()
    result: list[Range] = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        start, _, end = part.partition("-")
        result.append((int(start), int(end or start)))
    return tuple(result)


def _fence_for(body: str) -> str:
    """A fence one backtick longer than anything inside — excerpts nest fences."""

    longest = max((len(run) for run in _TICK_RUN.findall(body)), default=0)
    return _TICK * max(3, longest + 1)


def _render_block(label: str, body: str) -> str:
    fence = _fence_for(body)
    return f"{fence}{label}\n{body}\n{fence}"


def render_excerpts(excerpts: Excerpts) -> str:
    """Render 07-excerpts.md — frontmatter + yaml + before/after fences."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": excerpts.mr_iid,
                "units": len(excerpts.units),
                "files": excerpts.files,
            }
        )
    ]
    for unit in excerpts.units:
        parts.append("")
        parts.append(
            render_section(
                "EXCERPT",
                unit.unit_id,
                {
                    "kind": unit.kind,
                    "file": unit.file_id,
                    "section": unit.section,
                    "base_lines": _render_ranges(unit.base_lines),
                    "head_lines": _render_ranges(unit.head_lines),
                    "context": unit.context,
                },
            )
        )
        if unit.kind != "added":
            parts.append(_render_block("before", unit.before))
        if unit.kind != "removed":
            parts.append(_render_block("after", unit.after))
    return "\n".join(parts) + "\n"


def _read_block(lines: list[str], index: int) -> tuple[str, str, int]:
    """(label, body, next_index) for the fenced block starting at index."""

    opening = lines[index].strip()
    run = _TICK_RUN.match(opening)
    if not run:
        raise ValueError(f"expected a fenced block, got {lines[index]!r}")
    fence = run.group(0)
    label = opening[len(fence) :].strip()
    body: list[str] = []
    i = index + 1
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and set(stripped) == {_TICK} and len(stripped) >= len(fence):
            return label, "\n".join(body), i + 1
        body.append(lines[i])
        i += 1
    raise ValueError(f"unterminated {label!r} fence in 07-excerpts")


def parse_excerpts(text: str) -> Excerpts:
    """Parse 07-excerpts.md back — the round-trip contract for resume/tests."""

    meta = parse_frontmatter(text)
    lines = text.splitlines()
    units: list[Excerpt] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("## EXCERPT "):
            i += 1
            continue
        unit_id = lines[i].split(" ", 2)[2].strip()
        i += 1
        while i < len(lines) and not lines[i].startswith(_TICK * 3):
            i += 1
        label, body, i = _read_block(lines, i)
        if label != "yaml":
            raise ValueError(f"EXCERPT {unit_id} does not open with a yaml fence")
        fields = parse_frontmatter("---\n" + body + "\n---")
        blocks: dict[str, str] = {}
        while i < len(lines) and not lines[i].startswith("## "):
            if lines[i].startswith(_TICK * 3):
                name, block, i = _read_block(lines, i)
                blocks[name] = block
                continue
            i += 1
        units.append(
            Excerpt(
                unit_id=unit_id,
                kind=str(fields.get("kind", "")),
                file_id=str(fields.get("file", "")),
                section=str(fields.get("section", "")),
                base_lines=_parse_ranges(fields.get("base_lines")),
                head_lines=_parse_ranges(fields.get("head_lines")),
                context=int(fields.get("context") or 0),
                before=blocks.get("before", ""),
                after=blocks.get("after", ""),
            )
        )
    if meta.get("units") != len(units):
        raise ValueError("frontmatter unit count does not match blocks")
    return Excerpts(mr_iid=int(meta.get("mr_iid") or 0), units=tuple(units))
