"""30-levelcheck node — property verification from tool facts, plus the vocab gate.

Three inputs decide a unit's property, in this order of authority:
  05 kind      — added/removed makes the change 구조, whatever its body says;
  06 차집합     — a non-empty literal diff makes it 의미(값), full stop;
  20 class     — only then does the analyzer's 표현/의미 claim get a say.

Promotion only. 표현 survives exactly when all three literal diffs are empty;
any changed or removed/added value forces 의미(값). There is no path down:
demoting 의미 to 표현 would make a value change disappear from the report,
which is the one error this gate exists to prevent. A 의미(맥락) claim with no
literal evidence is kept with a warning instead — pure prose expresses
context changes without literals, and doc-verifier audits that case.

A unit nobody classified is 미분류, not 의미(값). The measured run defaulted
the missing class to L2 and printed a level that no one had determined —
빈 것과 틀린 것은 다른 실패다.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import classes, vocab
from .analysis import FileAnalysis
from .frontmatter import parse_frontmatter, parse_sections, render_frontmatter, render_section
from .literals import Literals
from .structure import Structure

#: CHANGED.kind values that make the unit a 구조 change on top of its body —
#: a renamed/re-levelled/reordered heading counts even when the section
#: keeps its content, which is what the matrix's 구조·변경 cell is for.
_STRUCTURAL_KINDS = frozenset(
    {"added", "removed", "heading_renamed", "level_changed", "reordered"}
)


@dataclass(frozen=True)
class LevelRow:
    """One unit's verified property — claimed values are never demoted."""

    unit_id: str
    claimed: str  # internal code, '' when the analyzer made no claim
    verified: str  # internal code: L1 | L2 | L3 | L0(미분류)
    basis: str
    classes: tuple[str, ...] = ()  # axis labels, e.g. ('구조', '의미')
    promoted: bool = False
    warning: str = ""
    vocab_violation: tuple[str, ...] = ()


@dataclass(frozen=True)
class SummaryRow:
    """A FILE_SUMMARY that tripped the vocab gate — nothing else to say."""

    file_id: str
    vocab_violation: tuple[str, ...]


@dataclass(frozen=True)
class LevelCheck:
    mr_iid: int
    units: tuple[LevelRow, ...]
    summaries: tuple[SummaryRow, ...] = ()

    @property
    def promoted(self) -> int:
        return sum(1 for row in self.units if row.promoted)

    @property
    def vocab_violations(self) -> int:
        return sum(1 for row in self.units if row.vocab_violation) + len(self.summaries)

    def verified_level(self, unit_id: str) -> str | None:
        for row in self.units:
            if row.unit_id == unit_id:
                return row.verified
        return None


def _verify(
    claimed: str, removed: int, added: int, changed: int, textual: int = 0
) -> tuple[str, str, bool, str]:
    """(verified code, basis, promoted, warning) for one unit's body."""

    if removed or added or changed:
        detail = f"changed {changed} · removed {removed} · added {added} 존재"
        if claimed == "L3":
            return "L2", f"{detail} — 표현 불성립, 의미(값)로 승격", True, ""
        return "L2", f"{detail} — 의미(값) 성립", False, ""
    if textual:
        # the tool found a rewritten run with identical literal sets —
        # 표현 is a tool-established fact, not an analyzer claim.
        return "L3", f"재작성 {textual}건 — 표현 성립(도구 확정)", False, ""
    if claimed == "L3":
        return "L3", "차집합 3종 전부 공집합 — 표현 성립", False, ""
    if claimed == "L1":
        return (
            "L1",
            "차집합 3종 전부 공집합 — 리터럴 근거 없음",
            False,
            "의미(맥락) 주장에 리터럴 근거 없음 — verifier 감사 대상",
        )
    return "L0", "차집합 공집합 · 분류 주장 없음 — 미분류", False, ""


def build_levelcheck(
    mr_iid: int,
    literals: Literals,
    analyses: list[FileAnalysis],
    structure: Structure | None = None,
) -> LevelCheck:
    """Verify every unit in 06-literals, then run the vocab gate over the prose."""

    claimed_by_unit: dict[str, str] = {}
    explanation_by_unit: dict[str, str] = {}
    for analysis in analyses:
        for unit in analysis.units:
            claimed_by_unit[unit.unit_id] = classes.CLAIM_CODES.get(unit.klass, "")
            explanation_by_unit[unit.unit_id] = unit.explanation
    kind_by_unit = {
        unit.unit_id: unit.structure_kind for unit in (structure.changed if structure else ())
    }

    rows: list[LevelRow] = []
    for unit in literals.units:
        claimed = claimed_by_unit.get(unit.unit_id, "")
        textual = len(unit.textual)
        verified, basis, promoted, warning = _verify(
            claimed,
            len(unit.removed),
            len(unit.added),
            len(unit.changed),
            textual,
        )
        tags: list[str] = []
        if kind_by_unit.get(unit.unit_id) in _STRUCTURAL_KINDS:
            tags.append(classes.STRUCTURE)
        tags.append(classes.axis(verified))
        if verified == "L2" and textual:
            # values changed AND words were rewritten in the same unit —
            # the unit is both; counts come from the atom inventory.
            tags.append(classes.EXPRESSION)
        rows.append(
            LevelRow(
                unit_id=unit.unit_id,
                claimed=claimed,
                verified=verified,
                basis=basis,
                classes=tuple(dict.fromkeys(tags)),
                promoted=promoted,
                warning=warning,
                vocab_violation=tuple(
                    vocab.find_violations(explanation_by_unit.get(unit.unit_id, ""))
                ),
            )
        )

    summaries = tuple(
        SummaryRow(analysis.file_id, tuple(hits))
        for analysis in analyses
        if (hits := vocab.find_violations(analysis.summary))
    )
    return LevelCheck(mr_iid=mr_iid, units=tuple(rows), summaries=summaries)


def class_counts(levelcheck: LevelCheck) -> dict[str, int]:
    """Units per axis label — a unit tagged 구조+의미 counts in both columns."""

    counts = dict.fromkeys(classes.AXIS_ORDER, 0)
    for row in levelcheck.units:
        for tag in row.classes:
            if tag in counts:
                counts[tag] += 1
    return counts


def render_levelcheck(levelcheck: LevelCheck) -> str:
    parts = [
        render_frontmatter(
            {
                "mr_iid": levelcheck.mr_iid,
                "classes": class_counts(levelcheck),
                "promoted": levelcheck.promoted,
                "vocab_violations": levelcheck.vocab_violations,
            }
        ),
    ]
    for row in levelcheck.units:
        fields: dict[str, object] = {
            "claimed": classes.claim_label(row.claimed) or None,
            "verified": classes.label(row.verified),
            "basis": row.basis,
            "classes": list(row.classes),
        }
        if row.promoted:
            fields["promoted"] = True
        if row.warning:
            fields["warning"] = row.warning
        if row.vocab_violation:
            fields["vocab_violation"] = list(row.vocab_violation)
        parts.append("")
        parts.append(render_section("UNIT", row.unit_id, fields))
    for summary in levelcheck.summaries:
        parts.append("")
        parts.append(
            render_section(
                "FILE_SUMMARY",
                summary.file_id,
                {"vocab_violation": list(summary.vocab_violation)},
            )
        )
    return "\n".join(parts) + "\n"


def _str_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def parse_levelcheck(text: str) -> LevelCheck:
    """Parse 30-levelcheck.md back — resume/tests round-trip."""

    meta = parse_frontmatter(text)
    rows: list[LevelRow] = []
    summaries: list[SummaryRow] = []
    for identifier, fields in parse_sections(text).items():
        if identifier.startswith("u-") and "verified" in fields:
            rows.append(
                LevelRow(
                    unit_id=identifier,
                    claimed=classes.CLAIM_CODES.get(str(fields.get("claimed") or ""), ""),
                    verified=classes.code(str(fields.get("verified", ""))),
                    basis=str(fields.get("basis", "")),
                    classes=_str_tuple(fields.get("classes")),
                    promoted=bool(fields.get("promoted", False)),
                    warning=str(fields.get("warning", "") or ""),
                    vocab_violation=_str_tuple(fields.get("vocab_violation")),
                )
            )
        elif "vocab_violation" in fields:
            summaries.append(
                SummaryRow(identifier, _str_tuple(fields.get("vocab_violation")))
            )
    return LevelCheck(
        mr_iid=int(meta.get("mr_iid", 0)),
        units=tuple(rows),
        summaries=tuple(summaries),
    )
