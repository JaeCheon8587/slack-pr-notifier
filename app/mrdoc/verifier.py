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
from collections.abc import Iterable
from dataclasses import dataclass, field

from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)

FIDELITY_VALUES = ("ok", "invented", "omitted")
#: The two verdicts that owe a rewrite — with or without a FIX block.
FLAGGED_FIDELITY = ("invented", "omitted")
CLASS_OPINIONS = ("agree", "dispute")
#: 50-collect verify.status — 'failed' means nobody checked, never "0 found".
VERIFY_OK = "ok"
VERIFY_FAILED = "failed"
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
    #: 'OK' unless the orchestrator wrote this file as a FAILED stub for a
    #: satellite that produced nothing — rendered only when it is not OK, so
    #: the prompt's template (and every real audit) carries no such field.
    status: str = "OK"

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

    def flagged_targets(self) -> tuple[str, ...]:
        """FIX targets plus every unit judged invented/omitted.

        fidelity is the verdict; a FIX block is its copy. A verifier that
        wrote 'invented' and forgot the FIX used to get no rewrite and count
        as zero outstanding — the copy going missing must not erase the
        verdict it copies.
        """

        return tuple(
            dict.fromkeys(
                [
                    *self.fix_targets(),
                    *(
                        unit.unit_id
                        for unit in self.units
                        if unit.fidelity in FLAGGED_FIDELITY
                    ),
                ]
            )
        )

    @property
    def is_stub(self) -> bool:
        """True for the orchestrator's FAILED/BLOCKED placeholder."""

        return self.status.strip().upper().startswith(("FAILED", "BLOCKED"))

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

    meta: dict[str, object] = {
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
    if verifier.status != "OK":
        meta["status"] = verifier.status
    parts = [render_frontmatter(meta)]
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
        status=_text(meta.get("status")) or "OK",
    )


def normalize_targets(targets: Iterable[str], files: Iterable[str] = ()) -> tuple[str, ...]:
    """Targets with the f- prefix dropped where it names a real file, deduped.

    doc-verifier's template once showed file ids that way; counted raw, one
    FILE_SUMMARY finding written as both 'f-X' and 'X' was two findings.
    """

    known = set(files)
    return tuple(
        dict.fromkeys(
            target[2:] if target.startswith("f-") and target[2:] in known else target
            for target in targets
        )
    )


def audit_units(verifier: Verifier | None) -> dict[str, VerifierUnit]:
    """unit_id -> its UNIT block, for blocks whose fidelity is a real verdict.

    'OK', 'pass' or an empty fidelity is not a verdict the pipeline can act
    on, so such a block audits nothing — the unit reads as 미검증.
    """

    if verifier is None:
        return {}
    return {
        unit.unit_id: unit for unit in verifier.units if unit.fidelity in FIDELITY_VALUES
    }


def verify_summary(
    text: str, auditable: Iterable[str] = (), files: Iterable[str] = ()
) -> dict[str, object]:
    """The 50-collect 'verify' block — read from the file, never from a return.

    Coverage is measured, not reported: `auditable` is every unit that had a
    설명 to audit, and a unit counts as audited when a UNIT block with a real
    fidelity names it — or a FIX does, since a unit the verifier asked to
    rewrite is one it looked at. The satellite's own 'checked' is never read.
    `files` lets an f-prefixed FIX target count once, as the file it names.

    An audit that cannot be read — unparseable, the orchestrator's FAILED
    stub, or one that judged nothing it was owed and found nothing either —
    is status 'failed', not zero findings. Collect still renders; the report
    prints '—' where a number would claim a check that never happened.
    """

    owed = tuple(dict.fromkeys(auditable))
    try:
        verifier: Verifier | None = parse_verifier(text)
    except ValueError:
        verifier = None
    flagged = normalize_targets(verifier.flagged_targets(), files) if verifier else ()
    audited = (set(audit_units(verifier)) | set(flagged)) & set(owed)
    failed = (
        verifier is None
        or verifier.is_stub
        or (bool(owed) and not audited and not flagged and not verifier.counts_mismatches)
    )
    if failed:
        return {
            "status": VERIFY_FAILED,
            "rounds": verifier.round if verifier else 1,
            "outstanding": 0,
            "counts_mismatch": 0,
            "audited": 0,
            "unaudited": len(owed),
        }
    assert verifier is not None
    return {
        "status": VERIFY_OK,
        "rounds": verifier.round,
        "outstanding": len(flagged),
        "counts_mismatch": verifier.counts_mismatches,
        "audited": len(audited),
        "unaudited": len(owed) - len(audited),
    }
