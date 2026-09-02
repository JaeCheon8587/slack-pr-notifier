"""30-levelcheck node — deterministic level verification from the literal diffs.

The design's gate: L3 (표현 변경) survives only when all three literal
diffs are empty; any changed value or removed/added fact forces promotion
to L2. Demotion never happens — an L1/L2 claim stays even when its literal
evidence is missing (pure-prose sections express context changes without
literals), which is exactly what the doc-verifier satellite audits.
"""

from __future__ import annotations

from dataclasses import dataclass

from .analysis import FileAnalysis
from .frontmatter import parse_frontmatter, parse_sections, render_frontmatter, render_section
from .literals import Literals


@dataclass(frozen=True)
class LevelCheck:
    """Verified level per unit — claimed values never demoted."""

    mr_iid: int
    units: tuple[LevelRow, ...]

    @property
    def promoted(self) -> int:
        return sum(1 for row in self.units if row.promoted)

    def verified_level(self, unit_id: str) -> str | None:
        for row in self.units:
            if row.unit_id == unit_id:
                return row.verified
        return None


@dataclass(frozen=True)
class LevelRow:
    unit_id: str
    claimed: str
    verified: str
    basis: str
    promoted: bool = False
    warning: str = ""


def _verify(unit_id: str, claimed: str, removed: int, added: int, changed: int) -> LevelRow:
    """The design's four rules — one row's verdict."""

    has_any = bool(removed or added or changed)
    replaced = removed > 0 and added > 0
    if not has_any:
        if claimed == "L3":
            return LevelRow(unit_id, claimed, "L3", "차집합 3종 전부 공집합 → L3 성립")
        if claimed == "L1":
            return LevelRow(
                unit_id, claimed, "L1", "차집합 공집합 — 리터럴 근거 없음 (순수 서술문 가능)",
                warning="L1 주장에 리터럴 근거 없음 — verifier 감사 대상",
            )
        return LevelRow(unit_id, claimed, claimed, "차집합 공집합이나 강등은 없음")
    if claimed == "L3":
        detail = f"removed {removed} + added {added}, changed {changed} → L3 불성립, L2 강제 승격"
        return LevelRow(unit_id, claimed, "L2", detail, promoted=True)
    if claimed == "L1":
        if replaced:
            detail = "removed + added 동시 존재 → 사실 교체, L1 근거"
        else:
            detail = "차집합 존재 — L1 유지 (강등 없음)"
        return LevelRow(unit_id, claimed, "L1", detail)
    detail = f"changed {changed}건 존재" if changed else "removed/added 존재"
    return LevelRow(unit_id, claimed, "L2", detail + " → L2 성립")


def build_levelcheck(mr_iid: int, literals: Literals, analyses: list[FileAnalysis]) -> LevelCheck:
    """Verify every unit in 06-literals against its claimed level."""

    claimed_by_unit: dict[str, str] = {}
    for analysis in analyses:
        for unit in analysis.units:
            claimed_by_unit[unit.unit_id] = unit.level
    rows: list[LevelRow] = []
    for unit in literals.units:
        claimed = claimed_by_unit.get(unit.unit_id, "L2")
        rows.append(
            _verify(
                unit.unit_id,
                claimed,
                len(unit.removed),
                len(unit.added),
                len(unit.changed),
            )
        )
    return LevelCheck(mr_iid=mr_iid, units=tuple(rows))


def level_counts(levelcheck: LevelCheck) -> dict[str, int]:
    counts = {"L1": 0, "L2": 0, "L3": 0}
    for row in levelcheck.units:
        if row.verified in counts:
            counts[row.verified] += 1
    return counts


def render_levelcheck(levelcheck: LevelCheck) -> str:
    counts = level_counts(levelcheck)
    parts = [
        render_frontmatter(
            {
                "mr_iid": levelcheck.mr_iid,
                "levels": counts,
                "promoted": levelcheck.promoted,
                "warnings": sum(1 for row in levelcheck.units if row.warning),
            }
        ),
    ]
    for row in levelcheck.units:
        fields: dict[str, object] = {
            "claimed": row.claimed,
            "verified": row.verified,
            "basis": row.basis,
        }
        if row.promoted:
            fields["promoted"] = True
        if row.warning:
            fields["warning"] = row.warning
        parts.append("")
        parts.append(render_section("UNIT", row.unit_id, fields))
    return "\n".join(parts) + "\n"


def parse_levelcheck(text: str) -> LevelCheck:
    """Parse 30-levelcheck.md back — resume/tests round-trip."""

    meta = parse_frontmatter(text)
    rows: list[LevelRow] = []
    for unit_id, fields in parse_sections(text).items():
        if not unit_id.startswith("u-"):
            continue
        rows.append(
            LevelRow(
                unit_id=unit_id,
                claimed=str(fields.get("claimed", "")),
                verified=str(fields.get("verified", "")),
                basis=str(fields.get("basis", "")),
                promoted=bool(fields.get("promoted", False)),
                warning=str(fields.get("warning", "") or ""),
            )
        )
    return LevelCheck(mr_iid=int(meta.get("mr_iid", 0)), units=tuple(rows))
