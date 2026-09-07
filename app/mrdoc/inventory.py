"""원자 인벤토리 — counts computed from atoms, not from unit-level tags.

The !22 failure mode: a unit carrying one heading rename plus four value
edits counted once, under one axis, because aggregation walked the units
and their single verified tag. Every fact inside the unit collapsed into
one cell and the overview said '삭제 0' while a deleted paragraph sat in
the report body. Here each atom — one heading change, one value, one
rewritten run, one prose run — counts as itself, so mixed units light up
every column they actually touch.

축/연산 vocabulary is the design's: 구조 · 의미 · 표현 over 추가 · 삭제 ·
변경. 미분류 is unit-level only (a unit no atom covers) and never gets a
matrix column — it stays a count and an appendix listing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import classes
from .changeset import Changeset
from .literals import Literals
from .structure import Structure

#: 05 CHANGED.kind -> (axis, op) for heading atoms. 'none' maps to nothing:
#: a section whose structure did not change has no structural atom.
_HEADING_ATOMS: dict[str, tuple[str, str]] = {
    "added": (classes.STRUCTURE, "추가"),
    "removed": (classes.STRUCTURE, "삭제"),
    "heading_renamed": (classes.STRUCTURE, "변경"),
    "level_changed": (classes.STRUCTURE, "변경"),
    "reordered": (classes.STRUCTURE, "변경"),
}

#: 06 UnitLiterals fields -> op for value atoms (modified units only — an
#: added section's values are informational, its atom is the heading).
_VALUE_OPS: dict[str, str] = {
    "changed": "변경",
    "removed": "삭제",
    "added": "추가",
}

_AXES = (classes.STRUCTURE, classes.MEANING, classes.EXPRESSION)
_OPS = ("추가", "삭제", "변경")


@dataclass(frozen=True)
class Atom:
    """One countable fact — the smallest thing the overview can count."""

    unit_id: str
    file_id: str
    axis: str  # 구조 | 의미 | 표현
    op: str  # 추가 | 삭제 | 변경
    kind: str  # heading | value | textual | prose
    detail: str = ""


@dataclass(frozen=True)
class Inventory:
    """Everything the counts are measured from — units never aggregate."""

    atoms: tuple[Atom, ...] = ()
    axis_counts: dict[str, int] = field(default_factory=dict)
    op_counts: dict[str, int] = field(default_factory=dict)
    matrix_by_file: dict[str, dict[str, dict[str, int]]] = field(
        default_factory=dict
    )
    unit_axes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unit_ops: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 의미+변경 atoms — the overview's '(값 n)'.
    value_changes: int = 0


def build_inventory(
    structure: Structure, literals: Literals, changeset: Changeset
) -> Inventory:
    """Measure every atom 05/06 carry — deterministic, no LLM."""

    lit_by_unit = {unit.unit_id: unit for unit in literals.units}
    atoms: list[Atom] = []
    for unit in structure.changed:
        heading = _HEADING_ATOMS.get(unit.structure_kind)
        if heading:
            atoms.append(
                Atom(
                    unit.unit_id,
                    unit.file_id,
                    heading[0],
                    heading[1],
                    "heading",
                )
            )
        lit = lit_by_unit.get(unit.unit_id)
        if lit is None or unit.kind != "modified":
            continue
        for change in lit.changed:
            atoms.append(
                Atom(
                    unit.unit_id,
                    unit.file_id,
                    classes.MEANING,
                    "변경",
                    "value",
                    f"{change.key} {change.from_value} → {change.to_value}",
                )
            )
        for value in lit.removed:
            atoms.append(
                Atom(unit.unit_id, unit.file_id, classes.MEANING, "삭제", "value", value)
            )
        for value in lit.added:
            atoms.append(
                Atom(unit.unit_id, unit.file_id, classes.MEANING, "추가", "value", value)
            )
        for before, after in lit.textual:
            atoms.append(
                Atom(
                    unit.unit_id,
                    unit.file_id,
                    classes.EXPRESSION,
                    "변경",
                    "textual",
                    f"{before} → {after}",
                )
            )
        for text in lit.prose_added:
            atoms.append(
                Atom(unit.unit_id, unit.file_id, classes.MEANING, "추가", "prose", text)
            )
        for text in lit.prose_removed:
            atoms.append(
                Atom(unit.unit_id, unit.file_id, classes.MEANING, "삭제", "prose", text)
            )

    axis_counts = dict.fromkeys(classes.AXIS_ORDER, 0)
    op_counts = dict.fromkeys(_OPS, 0)
    matrix: dict[str, dict[str, dict[str, int]]] = {
        entry.fid: {axis: dict.fromkeys(_OPS, 0) for axis in _AXES}
        for entry in changeset.files
    }
    unit_axes: dict[str, list[str]] = {}
    unit_ops: dict[str, list[str]] = {}
    value_changes = 0
    for atom in atoms:
        axis_counts[atom.axis] += 1
        op_counts[atom.op] += 1
        if atom.axis is classes.MEANING and atom.op == "변경":
            value_changes += 1
        if atom.file_id in matrix and atom.axis in _AXES:
            matrix[atom.file_id][atom.axis][atom.op] += 1
        axes = unit_axes.setdefault(atom.unit_id, [])
        if atom.axis not in axes:
            axes.append(atom.axis)
        ops = unit_ops.setdefault(atom.unit_id, [])
        if atom.op not in ops:
            ops.append(atom.op)

    final_axes: dict[str, tuple[str, ...]] = {}
    for unit in structure.changed:
        axes = tuple(
            axis
            for axis in classes.AXIS_ORDER
            if axis in unit_axes.get(unit.unit_id, ())
        )
        if not axes:
            # a unit no atom covers is 미분류 — counted, never matrixed
            axes = (classes.UNCLASSIFIED,)
            axis_counts[classes.UNCLASSIFIED] += 1
        final_axes[unit.unit_id] = axes
        unit_ops[unit.unit_id] = tuple(
            op for op in _OPS if op in unit_ops.get(unit.unit_id, ())
        )

    return Inventory(
        atoms=tuple(atoms),
        axis_counts=axis_counts,
        op_counts=op_counts,
        matrix_by_file=matrix,
        unit_axes=final_axes,
        unit_ops=dict(unit_ops),
        value_changes=value_changes,
    )
