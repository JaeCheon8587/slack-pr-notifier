"""05-structure node — base/head section trees into paired change units.

Pairing follows the design's three rules, in order:
  (1) same heading path  -> pair (even fully rewritten content stays one
      section — a heading is an identity, not a diff),
  (2) hunk line correspondence -> candidates via the i-th old extent
      overlapping a base section and the i-th new extent a head section,
  (3) normalized similarity >= threshold -> renamed pair; below -> the
      section is recorded as delete + create so both sides get reviewed.

Moves are exact-only: a head section whose normalized-text hash matches a
base section living in a *different* file is a MOVED row and produces no
change unit (nothing to review — the reviewer sees the relocation).
Canonical ids prefer the head side, so a section keeps one id across pushes.

Both trees are emitted and both are scoped to the changeset's files: TREE is
the head revision, BASE_TREE the base revision (old_path side). Repo-wide
trees were the measured failure — 3,893 rows / 600KB landed in a satellite
READ list. CHANGED carries a 'kind' column naming the structural change
(none | heading_renamed | level_changed | reordered | added | removed), which
is what the report's structure section is written from.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass

from .changeset import Changeset, FileEntry
from .ids import file_id
from .ids import section_id as make_section_id
from .ids import unit_id as make_unit_id
from .markdown_tree import Section, parse_sections
from .tables import parse_table, render_table

DEFAULT_SIMILARITY = 0.5


@dataclass(frozen=True)
class TreeSection:
    """One section of the head snapshot — the TREE table's rows."""

    section_id: str
    file: str
    heading_path: str
    lines: tuple[int, int]


#: CHANGED.kind vocabulary, in the design's precedence order — at most one
#: value per row, the first that holds. 'moved' lives in the MOVED table
#: instead: a relocation produces no change unit.
STRUCTURE_KINDS = (
    "added",
    "removed",
    "heading_renamed",
    "level_changed",
    "reordered",
    "none",
)


@dataclass(frozen=True)
class ChangeUnit:
    """One reviewable section change — kind derived from line columns."""

    unit_id: str
    section_id: str  # canonical: head-side id when a head side exists
    file_id: str
    old_lines: tuple[int, int] | None  # None -> added
    new_lines: tuple[int, int] | None  # None -> removed
    structure_kind: str = "none"  # CHANGED.kind — the structural change

    @property
    def kind(self) -> str:
        """Operation axis (added/removed/modified) — not the kind column."""

        if self.old_lines and self.new_lines:
            return "modified"
        return "added" if self.new_lines else "removed"


@dataclass(frozen=True)
class Move:
    """Cross-file relocation with byte-identical normalized text."""

    section: str  # heading path
    from_file: str
    to_file: str


@dataclass(frozen=True)
class Structure:
    tree: tuple[TreeSection, ...]  # head revision, changeset files only
    base_tree: tuple[TreeSection, ...]  # base revision, changeset files only
    changed: tuple[ChangeUnit, ...]
    moved: tuple[Move, ...]


def norm_text(text: str) -> str:
    """Trim + collapse whitespace per line, keep markdown symbols."""

    return "\n".join(
        re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()
    )


def _norm_hash(text: str) -> str:
    return hashlib.sha256(norm_text(text).encode("utf-8")).hexdigest()


def _tree_sections(
    tree: dict[str, str], paths: list[str]
) -> list[tuple[str, Section]]:
    """(path, section) for the named files only, file order then doc order.

    The path list is the changeset's, never the repository's: a repo-wide
    tree is what put 600KB into a satellite's READ list in the measured run.
    """

    result: list[tuple[str, Section]] = []
    for path in sorted(dict.fromkeys(paths)):
        if not path.lower().endswith((".md", ".mdx")) or path not in tree:
            continue
        result.extend((path, section) for section in parse_sections(tree[path]))
    return result


def _sid(path: str, section: Section) -> str:
    return make_section_id(path, section.ordinal, section.heading_path)


def _overlaps(lines: tuple[int, int], extent: tuple[int, int]) -> bool:
    return lines[0] <= extent[1] and extent[0] <= lines[1]


def _pair_by_heading(
    base: list[Section], head: list[Section]
) -> tuple[list[tuple[Section, Section]], list[Section], list[Section]]:
    """Rule (1): i-th occurrence of a heading path pairs with the i-th."""

    pairs: list[tuple[Section, Section]] = []
    base_left: list[Section] = list(base)
    head_left: list[Section] = list(head)
    by_path: dict[str, list[Section]] = {}
    for section in head:
        by_path.setdefault(section.heading_path, []).append(section)
    consumed: set[int] = set()
    for b in base_left:
        candidates = by_path.get(b.heading_path, [])
        for h in candidates:
            if id(h) in consumed:
                continue
            pairs.append((b, h))
            consumed.add(id(h))
            break
    paired_base = {id(b) for b, _ in pairs}
    base_left = [b for b in base_left if id(b) not in paired_base]
    head_left = [h for h in head_left if id(h) not in consumed]
    return pairs, base_left, head_left


def _pair_by_similarity(
    base_left: list[Section],
    head_left: list[Section],
    base_text: dict[int, str],
    head_text: dict[int, str],
    hunk_candidates: dict[int, set[int]],
    threshold: float,
) -> list[tuple[Section, Section]]:
    """Rules (2)+(3): score leftover candidates, greedy-match above threshold."""

    scored: list[tuple[float, Section, Section]] = []
    for b in base_left:
        allowed = hunk_candidates.get(id(b))
        for h in head_left:
            if allowed is not None and id(h) not in allowed:
                continue
            ratio = difflib.SequenceMatcher(
                None, base_text[id(b)], head_text[id(h)]
            ).ratio()
            if ratio >= threshold:
                scored.append((ratio, b, h))
    scored.sort(key=lambda item: (-item[0], item[1].start, item[2].start))
    pairs: list[tuple[Section, Section]] = []
    used_base: set[int] = set()
    used_head: set[int] = set()
    for _, b, h in scored:
        if id(b) in used_base or id(h) in used_head:
            continue
        pairs.append((b, h))
        used_base.add(id(b))
        used_head.add(id(h))
    return pairs


def _hunk_candidates(
    entry: FileEntry,
    base_sections: list[Section],
    head_sections: list[Section],
) -> dict[int, set[int]]:
    """Rule (2): base section -> head sections sharing a hunk index."""

    candidates: dict[int, set[int]] = {}
    for old_extent, new_extent in zip(
        entry.old_ranges, entry.new_ranges, strict=False
    ):
        heads = {
            id(h) for h in head_sections if _overlaps((h.start, h.end), new_extent)
        }
        if not heads:
            continue
        for b in base_sections:
            if _overlaps((b.start, b.end), old_extent):
                candidates.setdefault(id(b), set()).update(heads)
    return candidates


def _pair_ranks(
    pairs: list[tuple[Section, Section]],
) -> dict[int, tuple[int, int]]:
    """Per pair: (rank by base position, rank by head position) in this file.

    A differing rank is the design's "base 순서와 head 순서가 다르다" — the
    section sits at a different ordinal among its file's paired sections.
    """

    by_base = sorted(pairs, key=lambda pair: pair[0].start)
    by_head = sorted(pairs, key=lambda pair: pair[1].start)
    base_rank = {id(b): i for i, (b, _) in enumerate(by_base)}
    head_rank = {id(b): i for i, (b, _) in enumerate(by_head)}
    return {id(b): (base_rank[id(b)], head_rank[id(b)]) for b, _ in pairs}


def _pair_kind(
    base: Section, head: Section, ranks: dict[int, tuple[int, int]]
) -> str:
    """CHANGED.kind for a paired section — first match in precedence order."""

    if base.heading_path != head.heading_path:
        return "heading_renamed"
    if base.level != head.level:
        return "level_changed"
    before, after = ranks.get(id(base), (0, 0))
    return "reordered" if before != after else "none"


def _section_text(text: str, section: Section) -> str:
    lines = text.splitlines()
    return "\n".join(lines[section.start - 1 : section.end])


def build_structure(
    base_tree: dict[str, str],
    head_tree: dict[str, str],
    changeset: Changeset,
    threshold: float = DEFAULT_SIMILARITY,
) -> Structure:
    """Pair sections per changeset entry; collect TREE/CHANGED/MOVED.

    Two passes: per-entry pairing first (rules 1-3), then one cross-file
    move pass over the leftovers, so detection never depends on the order
    in which entries iterate.
    """

    def rows(tree: dict[str, str], paths: list[str]) -> tuple[TreeSection, ...]:
        return tuple(
            TreeSection(
                section_id=_sid(path, section),
                file=path,
                heading_path=section.heading_path,
                lines=(section.start, section.end),
            )
            for path, section in _tree_sections(tree, paths)
        )

    tree = rows(head_tree, [entry.path for entry in changeset.files])
    base_rows = rows(
        base_tree, [entry.old_path or entry.path for entry in changeset.files]
    )
    tree_ids = {row.section_id for row in tree}

    changed: list[ChangeUnit] = []
    moved: list[Move] = []
    pair_map: dict[str, tuple[str, Section, Section]] = {}  # entry path -> data
    head_leftovers: dict[str, list[Section]] = {}
    base_leftovers: dict[str, list[Section]] = {}

    for entry in changeset.files:
        base_path = entry.old_path or entry.path
        base_sections = parse_sections(base_tree.get(base_path, ""))
        head_sections = parse_sections(head_tree.get(entry.path, ""))

        if entry.status == "added":
            head_leftovers[entry.path] = head_sections
            continue
        if entry.status == "removed":
            base_leftovers[entry.path] = base_sections
            continue

        base_text = {
            id(s): norm_text(_section_text(base_tree.get(base_path, ""), s))
            for s in base_sections
        }
        head_text = {
            id(s): norm_text(_section_text(head_tree.get(entry.path, ""), s))
            for s in head_sections
        }

        pairs, base_left, head_left = _pair_by_heading(base_sections, head_sections)
        pairs += _pair_by_similarity(
            base_left,
            head_left,
            base_text,
            head_text,
            _hunk_candidates(entry, base_sections, head_sections),
            threshold,
        )

        paired_head = {id(h) for _, h in pairs}
        paired_base = {id(b) for b, _ in pairs}
        pair_map[entry.path] = (base_path, pairs, base_text, head_text)
        head_leftovers[entry.path] = [
            s for s in head_sections if id(s) not in paired_head
        ]
        base_leftovers[entry.path] = [
            s for s in base_sections if id(s) not in paired_base
        ]

    # cross-file move pass: exact norm-hash + heading match between leftovers
    claimed_heads: dict[str, list[Section]] = {}  # to-file -> moved head sections
    claimed_base_ids: set[int] = set()  # id() of base sections consumed by moves
    leftover_base_rows: list[tuple[str, str, Section]] = []
    all_base_leftovers: list[tuple[str, str, Section]] = []
    for entry_path, (base_path, _pairs, _bt, _ht) in pair_map.items():
        all_base_leftovers.extend(
            (entry_path, base_path, b) for b in base_leftovers.get(entry_path, [])
        )
    all_base_leftovers.extend(
        (entry_path, entry_path, b)
        for entry_path, sections in base_leftovers.items()
        if entry_path not in pair_map
        for b in sections
    )
    for entry_path, base_path, b in all_base_leftovers:
        b_hash = _norm_hash(_section_text(base_tree.get(base_path, ""), b))
        match = next(
            (
                (other_path, h)
                for other_path, sections in head_leftovers.items()
                if other_path != entry_path
                for h in sections
                if id(h) not in {id(x) for x in claimed_heads.get(other_path, [])}
                and h.heading_path == b.heading_path
                and _norm_hash(_section_text(head_tree.get(other_path, ""), h))
                == b_hash
            ),
            None,
        )
        if match:
            other_path, h = match
            claimed_heads.setdefault(other_path, []).append(h)
            claimed_base_ids.add(id(b))
            moved.append(Move(b.heading_path, entry_path, other_path))
        else:
            leftover_base_rows.append((entry_path, base_path, b))

    def emit_head(
        path: str, section: Section, old: Section | None, structure_kind: str
    ) -> None:
        sid = _sid(path, section)
        if sid not in tree_ids:
            raise ValueError(f"canonical id {sid} missing from head TREE")
        changed.append(
            ChangeUnit(
                unit_id=make_unit_id(sid),
                section_id=sid,
                file_id=file_id(path),
                old_lines=(old.start, old.end) if old else None,
                new_lines=(section.start, section.end),
                structure_kind=structure_kind,
            )
        )

    for entry_path, (_base_path, pairs, base_text, head_text) in pair_map.items():
        ranks = _pair_ranks(pairs)
        for b, h in pairs:
            if base_text[id(b)] == head_text[id(h)]:
                continue  # pairing shift only — nothing to review
            emit_head(entry_path, h, b, _pair_kind(b, h, ranks))
        for section in head_leftovers.get(entry_path, []):
            if id(section) in {id(x) for x in claimed_heads.get(entry_path, [])}:
                continue  # moved away — relocation recorded, no unit
            emit_head(entry_path, section, None, "added")

    for entry_path, base_path, b in leftover_base_rows:
        # canonical id falls back to the base side (no head exists)
        sid = _sid(base_path, b)
        changed.append(
            ChangeUnit(
                unit_id=make_unit_id(sid),
                section_id=sid,
                file_id=file_id(entry_path),
                old_lines=(b.start, b.end),
                new_lines=None,
                structure_kind="removed",
            )
        )
    for entry_path, sections in head_leftovers.items():
        if entry_path in pair_map:
            continue
        for section in sections:
            if id(section) in {id(x) for x in claimed_heads.get(entry_path, [])}:
                continue
            emit_head(entry_path, section, None, "added")

    return Structure(
        tree=tree,
        base_tree=base_rows,
        changed=tuple(changed),
        moved=tuple(moved),
    )


def _lines_text(lines: tuple[int, int] | None) -> str:
    if lines is None:
        return ""
    a, b = lines
    return f"{a}-{b}" if a != b else f"{a}"


def _parse_lines(text: str) -> tuple[int, int] | None:
    if not text:
        return None
    a, _, b = text.partition("-")
    return (int(a), int(b or a))


_TREE_HEADER = ["section_id", "file", "heading_path", "lines"]


def _tree_rows(rows: tuple[TreeSection, ...]) -> list[list[str]]:
    return [
        [row.section_id, row.file, row.heading_path, _lines_text(row.lines)]
        for row in rows
    ]


def render_structure(structure: Structure) -> str:
    """Render 05-structure.md — TREE, BASE_TREE, CHANGED, MOVED tables."""

    changed_rows = [
        [
            unit.unit_id,
            unit.section_id,
            unit.file_id,
            _lines_text(unit.old_lines),
            _lines_text(unit.new_lines),
            unit.structure_kind,
        ]
        for unit in structure.changed
    ]
    moved_rows = [
        [moved.section, moved.from_file, moved.to_file] for moved in structure.moved
    ]
    parts = [
        "## TREE\n\n" + render_table(_TREE_HEADER, _tree_rows(structure.tree)),
        "",
        "## BASE_TREE\n\n"
        + render_table(_TREE_HEADER, _tree_rows(structure.base_tree)),
        "",
        "## CHANGED\n\n"
        + render_table(
            ["unit_id", "section_id", "file_id", "old_lines", "new_lines", "kind"],
            changed_rows,
        ),
        "",
        f"## MOVED\n\n{render_table(['section', 'from', 'to'], moved_rows)}",
    ]
    return "\n".join(parts) + "\n"


def parse_structure(text: str) -> Structure:
    """Parse 05-structure.md back — the round-trip contract for resume."""

    def tree_rows(marker: str) -> tuple[TreeSection, ...]:
        return tuple(
            TreeSection(
                section_id=row[0],
                file=row[1],
                heading_path=row[2],
                lines=_parse_lines(row[3]) or (0, 0),
            )
            for row in parse_table(text, marker)
        )

    tree = tree_rows("TREE")
    base_tree = tree_rows("BASE_TREE")
    changed = tuple(
        ChangeUnit(
            unit_id=row[0],
            section_id=row[1],
            file_id=row[2],
            old_lines=_parse_lines(row[3]),
            new_lines=_parse_lines(row[4]),
            structure_kind=row[5] if len(row) > 5 and row[5] else "none",
        )
        for row in parse_table(text, "CHANGED")
    )
    moved = tuple(
        Move(section=row[0], from_file=row[1], to_file=row[2])
        for row in parse_table(text, "MOVED")
    )
    return Structure(tree=tree, base_tree=base_tree, changed=changed, moved=moved)
