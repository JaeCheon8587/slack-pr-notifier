"""40-verifier artifacts — parse + render the doc-verifier satellite's audit.

The verifier looks at one thing: whether each 설명 says what the source says.
fidelity is ok, invented (content the source does not carry) or omitted (a
change the 설명 dropped) — two different failures, because an invention puts
a false sentence in the report while an omission makes a real change vanish.
판정 필드가 없다 — 심각도 · 머지 의견을 담을 자리가 스키마에 없다. 이
계층은 MR 의 옳고 그름을 가리지 않고, 필드가 없으니 가릴 수도 없다.

The counts in the frontmatter are derived from the blocks, never read back
from the satellite's own tally. required_fixes in particular is the single
exit condition of the pipeline's one retry loop, so it is measured from the
FIX blocks that actually exist — a self-reported zero over three FIX blocks
would silently skip the round the design promises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)

FIDELITY_VALUES = ("ok", "invented", "omitted")
CLASS_OPINIONS = ("agree", "dispute")
#: FIX.reason vocabulary — one per audit failure, nothing else. The third
#: is the prose-vs-inventory gate: a FILE_SUMMARY that contradicts the
#: counts the tools measured.
FIX_REASONS = ("fidelity_invented", "fidelity_omitted", "counts_mismatch")

_BLOCK_HEADER = re.compile(r"^## ([A-Z][A-Z0-9_]*)(?: ([^\s]+))?")
_WHY = re.compile(r"^\*\*why\*\*\s*(.*)$")
_FENCE = chr(96) * 3


@dataclass(frozen=True)
class VerifierUnit:
    """One audited unit — fidelity plus an opinion on the claimed class."""

    unit_id: str
    fidelity: str  # ok | invented | omitted
    class_stated: str  # what 20-analysis claimed, '' when it claimed nothing
    class_opinion: str  # agree | dispute — never changes the class itself
    why: str = ""


@dataclass(frozen=True)
class Fix:
    """One unit whose 설명 has to be written again."""

    fix_id: str  # r-01
    target: str  # u-…
    field: str  # 설명 | FILE_SUMMARY
    reason: str  # fidelity_invented | fidelity_omitted | counts_mismatch


@dataclass(frozen=True)
class CountMismatch:
    """One prose sentence that contradicts the measured atom counts."""

    target: str  # unit_id or file_id the prose named
    stated: str  # what the prose claimed, e.g. '삭제 0'
    inventory: str  # what the atoms measure, e.g. '삭제 3'


@dataclass(frozen=True)
class CountsCheck:
    """One file's FILE_SUMMARY audited against its atom inventory."""

    file_id: str
    mismatches: tuple[CountMismatch, ...]
    why: str = ""


@dataclass(frozen=True)
class Verifier:
    mr_iid: int
    round: int = 1
    checked: int = 0  # units actually read, not units pointed at
    uncovered: str = "none"
    uncertain: str = "none"
    confidence: str = ""
    units: tuple[VerifierUnit, ...] = ()
    counts: tuple[CountsCheck, ...] = ()
    fixes: tuple[Fix, ...] = field(default_factory=tuple)

    @property
    def fidelity(self) -> dict[str, int]:
        return {
            value: sum(1 for unit in self.units if unit.fidelity == value)
            for value in FIDELITY_VALUES
        }

    @property
    def class_opinion(self) -> dict[str, int]:
        return {
            value: sum(1 for unit in self.units if unit.class_opinion == value)
            for value in CLASS_OPINIONS
        }

    @property
    def required_fixes(self) -> int:
        """Measured from the FIX blocks — the loop's only exit condition."""

        return len(self.fixes)

    def fix_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fix.target for fix in self.fixes if fix.target))

    @property
    def counts_mismatches(self) -> int:
        """Measured from the COUNTS blocks — never the satellite's tally."""

        return sum(len(check.mismatches) for check in self.counts)


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _split_blocks(text: str) -> list[tuple[tuple[str, str], list[str]]]:
    blocks: list[tuple[tuple[str, str], list[str]]] = []
    current: tuple[str, str] | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = _BLOCK_HEADER.match(line)
        if match:
            if current is not None:
                blocks.append((current, body))
            current = (match.group(1), match.group(2) or match.group(1))
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        blocks.append((current, body))
    return blocks


def _why_line(lines: list[str]) -> str:
    """'**why** …' plus its wrapped continuations — the verifier's one prose field."""

    captured: list[str] | None = None
    for line in lines:
        match = _WHY.match(line)
        if match:
            captured = [match.group(1).strip()]
            continue
        if captured is None:
            continue
        if line.startswith("## ") or line.startswith(_FENCE) or line.startswith("**"):
            break
        if line.strip():
            captured.append(line.strip())
    return " ".join(part for part in (captured or []) if part)


def render_verifier(verifier: Verifier) -> str:
    """Render 40-verifier.md — and the verifier prompt's own template."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": verifier.mr_iid,
                "round": verifier.round,
                "checked": verifier.checked,
                "fidelity": verifier.fidelity,
                "class_opinion": verifier.class_opinion,
                "counts": {
                    "checked": len(verifier.counts),
                    "mismatch": verifier.counts_mismatches,
                },
                "required_fixes": verifier.required_fixes,
                "uncovered": verifier.uncovered,
                "uncertain": verifier.uncertain,
                "confidence": verifier.confidence,
            }
        )
    ]
    for unit in verifier.units:
        parts.append("")
        parts.append(
            render_section(
                "UNIT",
                unit.unit_id,
                {
                    "fidelity": unit.fidelity,
                    "class_stated": unit.class_stated or None,
                    "class_opinion": unit.class_opinion,
                },
            )
        )
        if unit.why:
            parts.append(f"**why** {unit.why}")
    for fix in verifier.fixes:
        parts.append("")
        parts.append(
            render_section(
                "FIX",
                fix.fix_id,
                {"target": fix.target, "field": fix.field, "reason": fix.reason},
            )
        )
    for check in verifier.counts:
        parts.append("")
        parts.append(
            render_section(
                "COUNTS",
                check.file_id,
                {
                    "mismatch": [
                        {
                            "target": item.target,
                            "stated": item.stated,
                            "inventory": item.inventory,
                        }
                        for item in check.mismatches
                    ],
                    "why": check.why or None,
                },
            )
        )
    return "\n".join(parts) + "\n"


def parse_verifier(text: str) -> Verifier:
    """Parse 40-verifier.md back (ValueError on malformed frontmatter)."""

    meta = parse_frontmatter(text)
    sections = parse_sections(text)
    units: list[VerifierUnit] = []
    fixes: list[Fix] = []
    counts: list[CountsCheck] = []
    for header, body in _split_blocks(text):
        fields = sections.get(header[1], {})
        if header[0] == "UNIT":
            units.append(
                VerifierUnit(
                    unit_id=header[1],
                    fidelity=_text(fields.get("fidelity")),
                    class_stated=_text(fields.get("class_stated")),
                    class_opinion=_text(fields.get("class_opinion")),
                    why=_why_line(body),
                )
            )
        elif header[0] == "FIX":
            fixes.append(
                Fix(
                    fix_id=header[1],
                    target=_text(fields.get("target")),
                    field=_text(fields.get("field")),
                    reason=_text(fields.get("reason")),
                )
            )
        elif header[0] == "COUNTS":
            counts.append(
                CountsCheck(
                    file_id=header[1],
                    mismatches=tuple(
                        CountMismatch(
                            _text(row.get("target")),
                            _text(row.get("stated")),
                            _text(row.get("inventory")),
                        )
                        for row in fields.get("mismatch") or []
                        if isinstance(row, dict)
                    ),
                    why=_text(fields.get("why")),
                )
            )
        # RECEIPT (and anything else) is accepted and ignored.
    return Verifier(
        mr_iid=_int(meta.get("mr_iid")),
        round=_int(meta.get("round"), 1),
        checked=_int(meta.get("checked")),
        uncovered=_text(meta.get("uncovered")) or "none",
        uncertain=_text(meta.get("uncertain")) or "none",
        confidence=_text(meta.get("confidence")),
        units=tuple(units),
        counts=tuple(counts),
        fixes=tuple(fixes),
    )


def verify_summary(text: str) -> dict[str, object]:
    """The 50-collect 'verify' block — read from the file, never from a return.

    An unparseable report degrades to round 1 / nothing outstanding rather
    than aborting: collect's job is to render what exists, and a missing
    audit is already visible as an empty fidelity census.
    """

    try:
        verifier = parse_verifier(text)
    except ValueError:
        return {"rounds": 1, "outstanding": 0, "counts_mismatch": 0}
    return {
        "rounds": verifier.round,
        "outstanding": verifier.required_fixes,
        "counts_mismatch": verifier.counts_mismatches,
    }
