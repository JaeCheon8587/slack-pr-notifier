"""06-literals node — spec-bearing tokens per change unit, for cross-checking.

Five deterministic extractors (design spec): '키: 값', number+unit
('초/분/시간/일/ms/s/m/h/KB/MB/GB/%/회/개...'), booleans, code spans and link
targets. Normalization is trim + whitespace collapse with markdown symbols
kept, so '`30초`' and '30초' normalize identically. Diffing groups by key:
same key with a different value becomes changed[{key, from, to}] and those
values are then excluded from removed/added — the design's rule that makes
the verifier's quote cross-check unambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .changeset import Changeset
from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)
from .structure import Structure

_TICK = chr(96)
_KV = re.compile(r"^\s*[-*]?\s*([A-Za-z0-9_가-힣][A-Za-z0-9_가-힣 .\-/]*):\s+(.+?)\s*$")
_NUM_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(초|분|시간|일|ms|s|m|h|KB|MB|GB|TB|%|회|개|건|명|배)(?!\w)"
)
_BOOL = re.compile(r"\b(true|false)\b", re.IGNORECASE)
_CODE = re.compile(_TICK + r"([^" + _TICK + r"]+)" + _TICK)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


@dataclass(frozen=True)
class Literal:
    kind: str  # kv | unit | bool | code | link
    key: str
    value: str


@dataclass(frozen=True)
class ChangedValue:
    key: str
    from_value: str
    to_value: str


@dataclass(frozen=True)
class UnitLiterals:
    """One unit's literal diff — the 06 artifact's per-unit block."""

    unit_id: str
    section_id: str
    removed: tuple[str, ...]
    added: tuple[str, ...]
    changed: tuple[ChangedValue, ...]

    @property
    def total(self) -> int:
        return len(self.removed) + len(self.added) + len(self.changed)


@dataclass(frozen=True)
class Literals:
    units: tuple[UnitLiterals, ...]


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_literals(text: str) -> list[Literal]:
    """Run all five extractors; deterministic order, exact dedup."""

    found: list[Literal] = []
    for line in text.splitlines():
        if not (match := _KV.match(line)):
            continue
        key, value = match.groups()
        if "://" not in key:  # skip 'https:' style accidental keys
            found.append(Literal("kv", _collapse(key), _collapse(value)))
    for number, unit in _NUM_UNIT.findall(text):
        found.append(Literal("unit", unit, number))
    for value in _BOOL.findall(text):
        found.append(Literal("bool", "bool", value.lower()))
    for span in _CODE.findall(text):
        token = re.split(r"[=:\s]", span.strip(), maxsplit=1)[0]
        found.append(Literal("code", token or "code", _collapse(span)))
    for label, target in _LINK.findall(text):
        found.append(Literal("link", _collapse(label), _collapse(target)))

    unique = {(lit.kind, lit.key, lit.value): lit for lit in found}
    return [unique[k] for k in sorted(unique)]


def _diff(
    base: list[Literal], head: list[Literal]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[ChangedValue, ...]]:
    """Group by (kind, key): moved values -> changed, leftovers -> removed/added."""

    def grouped(literals: list[Literal]) -> dict[tuple[str, str], list[str]]:
        result: dict[tuple[str, str], list[str]] = {}
        for lit in literals:
            result.setdefault((lit.kind, lit.key), []).append(lit.value)
        return result

    base_groups = grouped(base)
    head_groups = grouped(head)
    removed: list[str] = []
    added: list[str] = []
    changed: list[ChangedValue] = []
    for group in sorted(set(base_groups) | set(head_groups)):
        b_values = sorted(base_groups.get(group, []))
        h_values = sorted(head_groups.get(group, []))
        shared = set(b_values) & set(h_values)
        b_rest = [v for v in b_values if v not in shared]
        h_rest = [v for v in h_values if v not in shared]
        for old, new in zip(b_rest, h_rest, strict=False):
            changed.append(ChangedValue(group[1], old, new))
        removed.extend(b_rest[len(h_rest) :])
        added.extend(h_rest[len(b_rest) :])
    return (
        tuple(sorted(removed)),
        tuple(sorted(added)),
        tuple(sorted(changed, key=lambda c: (c.key, c.from_value, c.to_value))),
    )


def _section_text(tree: dict[str, str], path: str | None, lines: tuple[int, int] | None) -> str:
    if path is None or lines is None or path not in tree:
        return ""
    file_lines = tree[path].splitlines()
    return "\n".join(file_lines[lines[0] - 1 : lines[1]])


def build_literals(
    structure: Structure, changeset: Changeset, base_tree: dict[str, str], head_tree: dict[str, str]
) -> Literals:
    """Diff literals per change unit using the same trees 05 was built from."""

    head_paths = {row.section_id: row.file for row in structure.tree}
    old_paths = {
        entry.fid: (entry.old_path or entry.path) for entry in changeset.files
    }
    units: list[UnitLiterals] = []
    for unit in structure.changed:
        base_path = old_paths.get(unit.file_id) if unit.old_lines else None
        head_path = head_paths.get(unit.section_id) if unit.new_lines else None
        base = extract_literals(_section_text(base_tree, base_path, unit.old_lines))
        head = extract_literals(_section_text(head_tree, head_path, unit.new_lines))
        removed, added, changed = _diff(base, head)
        units.append(
            UnitLiterals(
                unit_id=unit.unit_id,
                section_id=unit.section_id,
                removed=removed,
                added=added,
                changed=changed,
            )
        )
    return Literals(units=tuple(units))


def average_per_unit(literals: Literals) -> float:
    """Phase-0 density metric — footer gate uses >=3 keep / <1 redesign."""

    if not literals.units:
        return 0.0
    return sum(unit.total for unit in literals.units) / len(literals.units)


def render_literals(literals: Literals) -> str:
    """Render 06-literals.md — frontmatter + one yaml block per unit."""

    parts = [
        render_frontmatter(
            {
                "units": len(literals.units),
                "totals": {
                    "removed": sum(len(u.removed) for u in literals.units),
                    "added": sum(len(u.added) for u in literals.units),
                    "changed": sum(len(u.changed) for u in literals.units),
                },
            }
        ),
    ]
    for unit in literals.units:
        changed_rows = [
            {"key": c.key, "from": c.from_value, "to": c.to_value} for c in unit.changed
        ]
        parts.append("")
        parts.append(
            render_section(
                "UNIT",
                unit.unit_id,
                {
                    "section_id": unit.section_id,
                    "removed": list(unit.removed),
                    "added": list(unit.added),
                    "changed": changed_rows,
                },
            )
        )
    return "\n".join(parts) + "\n"


def parse_literals(text: str) -> Literals:
    """Parse 06-literals.md back — round-trip contract for resume/tests."""

    def value_str(value: object) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        return str(value)

    sections = parse_sections(text)
    units: list[UnitLiterals] = []
    for unit_id, fields in sections.items():
        if not unit_id.startswith("u-"):
            continue
        changed_raw = fields.get("changed") or []
        changed = tuple(
            ChangedValue(
                value_str(row.get("key", "")),
                value_str(row.get("from", "")),
                value_str(row.get("to", "")),
            )
            for row in changed_raw
            if isinstance(row, dict)
        )
        units.append(
            UnitLiterals(
                unit_id=unit_id,
                section_id=str(fields.get("section_id", "")),
                removed=tuple(value_str(v) for v in fields.get("removed") or []),
                added=tuple(value_str(v) for v in fields.get("added") or []),
                changed=changed,
            )
        )
    meta = parse_frontmatter(text)
    if meta.get("units") != len(units):
        raise ValueError("frontmatter unit count does not match blocks")
    return Literals(units=tuple(units))
