"""50-collect node — one deterministic assembly of every upstream artifact.

The design's six steps: parse every 20-analysis file, assemble each change
unit out of the tools' facts (05 kind + 06 literal diffs + 07 excerpt ref +
30 verified 성질 + 20 설명), check that the prose points at ids that really
exist, count the matrix the report's second section prints verbatim, group
by product folder -> file -> section, write the artifact.

Two rules shape the whole module. Nothing here judges — the only natural
language is the 설명 and FILE_SUMMARY the analyzer wrote. And partial failure
degrades to "a report without 설명", never to a wrong report: an unparsable
or missing analysis leaves the prose field saying so while structure, raw
text, literal values and the tool-determined 성질 all still render.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import classes
from .analysis import FileAnalysis, _bold_lines, _split_blocks
from .changeset import Changeset
from .excerpt import Excerpts
from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)
from .inventory import build_inventory
from .levelcheck import LevelCheck
from .literals import ChangedValue, Literals
from .structure import Structure
from .themes import Theme, parse_themes
from .verifier import verify_summary

#: What a prose field says when the analyzer produced nothing usable for it.
#: The design's words, not a paraphrase — the report prints them as-is and
#: the 리포트 신뢰도 block counts them.
EXPLANATION_FAILED = "설명 생성 실패"
SUMMARY_FAILED = "FILE_SUMMARY 생성 실패"

#: ChangeUnit.kind speaks the diff's vocabulary; the artifact speaks the
#: design's.
_KIND = {"modified": "changed", "added": "added", "removed": "removed"}

_OPS = ("추가", "삭제", "변경")

_CONF_KEYS = ("high", "medium", "low")


@dataclass(frozen=True)
class CollectedUnit:
    """One change unit as the report shows it — tool facts plus one 설명."""

    unit_id: str
    kind: str  # added | removed | changed
    structure_kind: str  # 05 CHANGED.kind — 'none' when purely textual
    klass: str  # 구조 | 의미 | 표현 | 미분류
    section: str
    file: str  # file_id
    removed: tuple[str, ...]
    added: tuple[str, ...]
    changed: tuple[ChangedValue, ...]
    excerpt_ref: str
    explanation: str
    vocab_violation: tuple[str, ...] = ()
    #: 성질/연산 columns this unit's atoms touch — axis order, from 06/05.
    axes: tuple[str, ...] = ()
    ops: tuple[str, ...] = ()
    textual: tuple[tuple[str, str], ...] = ()
    prose_added: tuple[str, ...] = ()
    prose_removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectedFile:
    """One changed file — its product folder and the file-level FILE_SUMMARY."""

    file_id: str
    path: str
    product_dir: str
    units: int
    summary: str


@dataclass(frozen=True)
class Collect:
    """Everything 50-collect.md carries — the render node's input contract."""

    mr_iid: int
    files: dict[str, int]
    matrix: dict[str, dict[str, dict[str, int]]]
    classes: dict[str, int]
    ops: dict[str, int]
    value_changes: int
    verify: dict[str, object]
    confidence_dist: dict[str, int]
    uncertain: tuple[str, ...]
    failed_files: tuple[str, ...]
    refs_dropped: int
    file_blocks: tuple[CollectedFile, ...]
    units: tuple[CollectedUnit, ...]
    #: The themes satellite's grouped output, gated here: members must be
    #: known units and a surviving theme holds at least two of them.
    themes: tuple[Theme, ...] = ()
    themes_dropped: int = 0
    themes_failed: bool = False

    def valid_ids(self) -> frozenset[str]:
        """Every id a block may point at — render's one gate."""

        return frozenset(
            [unit.unit_id for unit in self.units]
            + [entry.file_id for entry in self.file_blocks]
        )

    def units_of(self, file_id: str) -> tuple[CollectedUnit, ...]:
        return tuple(unit for unit in self.units if unit.file == file_id)


def _product_dir(path: str) -> str:
    """The folder a file belongs to — the report's 영향 제품 grouping key."""

    head, _, _ = path.rpartition("/")
    return head or "."


def _one_line(text: str) -> str:
    return " ".join(text.split())


def build_collect(
    *,
    mr_iid: int,
    analyses: list[FileAnalysis],
    failed_files: tuple[str, ...],
    levelcheck: LevelCheck,
    literals: Literals,
    structure: Structure,
    changeset: Changeset,
    excerpts: Excerpts,
    verifier_text: str,
    themes_text: str = "",
) -> Collect:
    """Run the design's six steps over parsed artifacts — no LLM anywhere."""

    # 1. parse — the caller did it; failed_files is what could not be read.
    known = {unit.unit_id for unit in structure.changed}
    explanation: dict[str, str] = {}
    refs_dropped = 0
    for analysis in analyses:
        for unit in analysis.units:
            # 3. refs — a 설명 for a unit 05 never produced points at nothing.
            # The design drops it rather than asking for a rewrite, so the
            # loop keeps exactly one exit condition.
            if unit.unit_id not in known:
                refs_dropped += 1
                continue
            text = _one_line(unit.explanation)
            if text:
                explanation[unit.unit_id] = text

    # 2. assemble — every unit 05 found, covered by the analyzer or not.
    lit_by_unit = {unit.unit_id: unit for unit in literals.units}
    excerpt_by_unit = {unit.unit_id: unit for unit in excerpts.units}
    vocab_by_unit = {row.unit_id: row.vocab_violation for row in levelcheck.units}
    inventory = build_inventory(structure, literals, changeset)
    units: list[CollectedUnit] = []
    for unit in structure.changed:
        kind = _KIND.get(unit.kind, unit.kind)
        lit = lit_by_unit.get(unit.unit_id)
        excerpt = excerpt_by_unit.get(unit.unit_id)
        verified = levelcheck.verified_level(unit.unit_id) or ""
        units.append(
            CollectedUnit(
                unit_id=unit.unit_id,
                kind=kind,
                structure_kind=unit.structure_kind,
                klass=(
                    classes.axis(verified) if kind == "changed" else classes.STRUCTURE
                ),
                section=excerpt.section if excerpt else unit.section_id,
                file=unit.file_id,
                removed=lit.removed if lit else (),
                added=lit.added if lit else (),
                changed=lit.changed if lit else (),
                excerpt_ref=unit.unit_id if excerpt else "",
                explanation=explanation.get(unit.unit_id, EXPLANATION_FAILED),
                vocab_violation=vocab_by_unit.get(unit.unit_id, ()),
                axes=inventory.unit_axes.get(unit.unit_id, (classes.UNCLASSIFIED,)),
                ops=inventory.unit_ops.get(unit.unit_id, ()),
                textual=lit.textual if lit else (),
                prose_added=lit.prose_added if lit else (),
                prose_removed=lit.prose_removed if lit else (),
            )
        )

    # 3. refs (cont.) — a FILE_SUMMARY naming an id that does not exist goes
    # the same way as an orphan 설명: dropped, counted, shown as failed.
    summaries: dict[str, str] = {}
    for analysis in analyses:
        if any(ref not in known for ref in analysis.summary_refs):
            refs_dropped += 1
            continue
        text = _one_line(analysis.summary)
        if text:
            summaries[analysis.file_id] = text

    # 5. group + sort — product folder, then file, then the section order 05
    # emitted (document order inside a file).
    entries = sorted(
        changeset.files, key=lambda entry: (_product_dir(entry.path), entry.path)
    )
    file_blocks = tuple(
        CollectedFile(
            file_id=entry.fid,
            path=entry.path,
            product_dir=_product_dir(entry.path),
            units=sum(1 for unit in units if unit.file == entry.fid),
            summary=summaries.get(entry.fid, SUMMARY_FAILED),
        )
        for entry in entries
    )
    order = {entry.fid: index for index, entry in enumerate(entries)}
    ordered_units = tuple(
        sorted(units, key=lambda unit: order.get(unit.file, len(order)))
    )

    # 4. counts — measured from the atom inventory (05/06 facts), not from
    # unit-level tags: a mixed unit lights every column its atoms touch.

    confidence_dist = dict.fromkeys(_CONF_KEYS, 0)
    for analysis in analyses:
        head = analysis.confidence.split()[0].lower() if analysis.confidence else ""
        if head in confidence_dist:
            confidence_dist[head] += 1

    themes: tuple[Theme, ...] = ()
    themes_dropped = 0
    themes_failed = False
    if themes_text:
        try:
            parsed = parse_themes(themes_text)
        except ValueError:
            parsed = None
            themes_failed = True  # shown in 분석 상태, the report still renders
        if parsed is not None:
            if parsed.status.strip().upper().startswith(("FAILED", "BLOCKED")):
                parsed = None
                themes_failed = True
            else:
                kept: list[Theme] = []
                for theme in parsed.themes:
                    members = tuple(u for u in theme.units if u in known)
                    if len(members) < 2:
                        themes_dropped += 1
                        continue
                    kept.append(replace(theme, units=members))
                themes = tuple(kept)

    return Collect(
        mr_iid=mr_iid,
        files={
            "added": sum(1 for e in changeset.files if e.status == "added"),
            "deleted": sum(1 for e in changeset.files if e.status == "removed"),
            "modified": sum(1 for e in changeset.files if e.status == "modified"),
            "moved_sections": len(structure.moved),
        },
        matrix=inventory.matrix_by_file,
        classes=inventory.axis_counts,
        ops=inventory.op_counts,
        value_changes=inventory.value_changes,
        verify=verify_summary(verifier_text),
        confidence_dist=confidence_dist,
        uncertain=tuple(
            analysis.uncertain
            for analysis in analyses
            if analysis.uncertain and analysis.uncertain != "none"
        ),
        failed_files=failed_files,
        refs_dropped=refs_dropped,
        file_blocks=file_blocks,
        units=ordered_units,
        themes=themes,
        themes_dropped=themes_dropped,
        themes_failed=themes_failed,
    )


def render_collect(collect: Collect) -> str:
    """Render 50-collect.md — the render node's parse contract."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": collect.mr_iid,
                "files": collect.files,
               "matrix": collect.matrix,
               "classes": collect.classes,
                "ops": collect.ops,
                "value_changes": collect.value_changes,
               "verify": collect.verify,
                "confidence_dist": collect.confidence_dist,
                "uncertain": list(collect.uncertain),
                "failed_files": list(collect.failed_files),
                "refs_dropped": collect.refs_dropped,
                "themes_dropped": collect.themes_dropped,
                "themes_failed": collect.themes_failed,
            }
        ),
    ]
    for entry in collect.file_blocks:
        parts.append("")
        parts.append(
            render_section(
                "FILE",
                entry.file_id,
                {
                    "path": entry.path,
                    "product_dir": entry.product_dir,
                    "units": entry.units,
                },
            )
        )
        parts.append("**FILE_SUMMARY** " + entry.summary)
        for unit in collect.units_of(entry.file_id):
            fields: dict[str, object] = {
                "kind": unit.kind,
                "structure_kind": unit.structure_kind,
                "class": unit.klass,
                "section": unit.section,
                "file": unit.file,
                "removed": list(unit.removed),
                "added": list(unit.added),
                "changed": [
                    {"key": c.key, "from": c.from_value, "to": c.to_value}
                    for c in unit.changed
                ],
                "excerpt_ref": unit.excerpt_ref,
                "axes": list(unit.axes),
                "ops": list(unit.ops),
                "textual": [
                    {"before": before, "after": after}
                    for before, after in unit.textual
                ],
                "prose_added": list(unit.prose_added),
                "prose_removed": list(unit.prose_removed),
            }
            if unit.vocab_violation:
                fields["vocab_violation"] = list(unit.vocab_violation)
            parts.append("")
            parts.append(render_section("UNIT", unit.unit_id, fields))
            parts.append("**설명** " + unit.explanation)
    for theme in collect.themes:
        parts.append("")
        parts.append(
            render_section(
                "THEME",
                theme.theme_id,
                {"title": theme.title, "units": list(theme.units)},
            )
        )
        parts.append("**한 줄** " + theme.line)
    return "\n".join(parts) + "\n"


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _as_int(item) for key, item in value.items()}


def parse_collect(text: str) -> Collect:
    """Parse 50-collect.md back — render reads the artifact, not the builder."""

    meta = parse_frontmatter(text)
    sections = parse_sections(text)

    def dict_of(key: str) -> dict[str, object]:
        raw = meta.get(key)
        return dict(raw) if isinstance(raw, dict) else {}

    def str_list(key: str) -> tuple[str, ...]:
        raw = meta.get(key)
        return tuple(str(item) for item in raw) if isinstance(raw, list) else ()

    matrix: dict[str, dict[str, dict[str, int]]] = {}
    for file_id, axes in dict_of("matrix").items():
        if isinstance(axes, dict):
            matrix[str(file_id)] = {
                str(axis): _int_map(ops) for axis, ops in axes.items()
            }

    file_blocks: list[CollectedFile] = []
    units: list[CollectedUnit] = []
    themes: list[Theme] = []
    for header, block_lines in _split_blocks(text):
        bold = _bold_lines(block_lines)
        fields = sections.get(header[1], {})
        if header[0] == "FILE":
            file_blocks.append(
                CollectedFile(
                    file_id=header[1],
                    path=str(fields.get("path", "")),
                    product_dir=str(fields.get("product_dir", "")),
                    units=_as_int(fields.get("units")),
                    summary=bold.get("FILE_SUMMARY", ""),
                )
            )
        elif header[0] == "UNIT":
            units.append(
                CollectedUnit(
                    unit_id=header[1],
                    kind=str(fields.get("kind", "")),
                    structure_kind=str(fields.get("structure_kind", "")),
                    klass=str(fields.get("class", "")),
                    section=str(fields.get("section", "")),
                    file=str(fields.get("file", "")),
                    removed=tuple(str(v) for v in fields.get("removed") or []),
                    added=tuple(str(v) for v in fields.get("added") or []),
                    changed=tuple(
                        ChangedValue(
                            str(row.get("key", "")),
                            str(row.get("from", "")),
                            str(row.get("to", "")),
                        )
                        for row in fields.get("changed") or []
                        if isinstance(row, dict)
                    ),
                    excerpt_ref=str(fields.get("excerpt_ref") or ""),
                    axes=tuple(str(v) for v in fields.get("axes") or []),
                    ops=tuple(str(v) for v in fields.get("ops") or []),
                    textual=tuple(
                        (str(row.get("before", "")), str(row.get("after", "")))
                        for row in fields.get("textual") or []
                        if isinstance(row, dict)
                    ),
                    prose_added=tuple(
                        str(v) for v in fields.get("prose_added") or []
                    ),
                    prose_removed=tuple(
                        str(v) for v in fields.get("prose_removed") or []
                    ),
                    explanation=bold.get("설명", ""),
                    vocab_violation=tuple(
                        str(v) for v in fields.get("vocab_violation") or []
                    ),
                )
            )
        elif header[0] == "THEME":
            raw_units = fields.get("units")
            themes.append(
                Theme(
                    theme_id=header[1],
                    title=str(fields.get("title") or ""),
                    line=bold.get("한 줄", ""),
                    units=tuple(str(u) for u in raw_units or []),
                )
            )

    return Collect(
        mr_iid=_as_int(meta.get("mr_iid")),
        files=_int_map(dict_of("files")),
        matrix=matrix,
        classes=_int_map(dict_of("classes")),
        ops=_int_map(dict_of("ops")),
        value_changes=_as_int(meta.get("value_changes")),
        verify=dict_of("verify"),
        confidence_dist=_int_map(dict_of("confidence_dist")),
        uncertain=str_list("uncertain"),
        failed_files=str_list("failed_files"),
        refs_dropped=_as_int(meta.get("refs_dropped")),
        file_blocks=tuple(file_blocks),
        units=tuple(units),
        themes=tuple(themes),
        themes_dropped=_as_int(meta.get("themes_dropped")),
        themes_failed=bool(meta.get("themes_failed")),
    )
