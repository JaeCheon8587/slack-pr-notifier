"""50-collect node — deterministic finding gate over every upstream artifact.

The design's 9-step collector: parse every 20-analysis file, drop findings
whose unit reference or quoted evidence does not survive a direct check
against the source trees, dedup by content hash, assign severity by rule
(never by the satellite's opinion), aggregate levels from 30-levelcheck's
*verified* column, and compute the verdict. Dropped findings leave a count
in 'gate' — partial failure stays visible but never re-prompts: the loop's
only exit condition remains "every artifact exists", so it always ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .analysis import AnalysisFinding, Evidence, FileAnalysis, _bold_lines, _split_blocks
from .changeset import Changeset
from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)
from .ids import sha8
from .levelcheck import LevelCheck
from .literals import ChangedValue, Literals, extract_literals
from .structure import Structure

_MAJOR_CATEGORIES = frozenset({"stale_reference", "dangling_link", "contradiction"})
_NORM = re.compile(r"\s+")
_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def _norm(text: str) -> str:
    return _NORM.sub(" ", text).strip()


@dataclass(frozen=True)
class CollectedUnit:
    """One change unit as the report shows it — verified level + tool facts."""

    unit_id: str
    level: str
    section: str
    file: str
    removed: tuple[str, ...]
    added: tuple[str, ...]
    changed: tuple[ChangedValue, ...]
    before: str
    after: str
    confidence: str = ""


@dataclass(frozen=True)
class CollectedFinding:
    """One finding that survived the gate — severity assigned here, by rule."""

    finding_id: str
    severity: str  # BLOCKER | MAJOR | MINOR
    category: str
    unit_id: str
    evidence: tuple[Evidence, ...]
    claim: str
    recommendation: str
    confidence: str = ""


@dataclass(frozen=True)
class DroppedFinding:
    """One finding the gate rejected — count-only, never re-prompted."""

    finding_id: str
    reason: str  # bad_ref | quote_mismatch | merged_dup
    detail: str


@dataclass(frozen=True)
class Collect:
    """Everything 50-collect.md carries — the reporter/render input contract."""

    mr_iid: int
    verdict: str  # BLOCK | REVIEW | PASS
    reason: str
    levels: dict[str, int]
    files: dict[str, int]
    counts: dict[str, int]
    gate: dict[str, int]
    verify: dict[str, object]
    coverage: dict[str, object]
    confidence_dist: dict[str, int]
    uncertain: tuple[str, ...]
    failed_files: tuple[str, ...]
    must_read: tuple[str, ...]
    units: tuple[CollectedUnit, ...]
    findings: tuple[CollectedFinding, ...]
    dropped: tuple[DroppedFinding, ...]

    def valid_ids(self) -> frozenset[str]:
        """Every u-/f- id the reporter may point at (render's ref gate)."""

        return frozenset(
            [unit.unit_id for unit in self.units]
            + [finding.finding_id for finding in self.findings]
        )


def _confidence_head(text: str) -> str:
    return text.split()[0].lower() if text.split() else ""


def _rank(text: str) -> int:
    return _CONF_RANK.get(_confidence_head(text), 0)


def _quote_matches(
    evidence: Evidence, base_tree: dict[str, str], head_tree: dict[str, str]
) -> bool:
    """Step 3 — the quote must appear in its declared rev's line ±3 window."""

    if evidence.rev == "head":
        tree = head_tree
    elif evidence.rev == "base":
        tree = base_tree
    else:
        return False
    source = tree.get(evidence.file)
    if source is None or evidence.line <= 0:
        return False
    quote = _norm(evidence.quote)
    if not quote:
        return False
    rows = source.splitlines()
    lo = max(evidence.line - 3, 1)
    hi = min(evidence.line + 3, len(rows))
    return any(quote in _norm(row) for row in rows[lo - 1 : hi])


def _dedup_key(finding: AnalysisFinding) -> str:
    quotes = sorted(sha8(_norm(item.quote)) for item in finding.evidence)
    return sha8(finding.category + "|" + "|".join(quotes))


def _has_conflicting_literal(quotes: tuple[str, ...]) -> bool:
    """BLOCKER test — same literal key, different values, across two quotes."""

    by_key: dict[str, set[str]] = {}
    for quote in quotes:
        for lit in extract_literals(quote):
            by_key.setdefault(f"{lit.kind}:{lit.key}", set()).add(lit.value)
    return any(len(values) > 1 for values in by_key.values())


def _severity(finding: AnalysisFinding) -> str:
    quotes = tuple(item.quote for item in finding.evidence)
    if finding.category == "contradiction" and _has_conflicting_literal(quotes):
        return "BLOCKER"
    if finding.category in _MAJOR_CATEGORIES:
        return "MAJOR"
    return "MINOR"


def _section_label(structure: Structure, section_id: str) -> tuple[str, str]:
    for row in structure.tree:
        if row.section_id == section_id:
            return row.file, f"{row.file} § {row.heading_path}"
    return "", section_id


def _verify_summary(verifier_text: str) -> dict[str, object]:
    """The verify block — parsed from 40-verifier.md, never from returns."""

    meta = parse_frontmatter(verifier_text)
    return {
        "rounds": int(meta.get("round", 1) or 1),
        "verdict": str(meta.get("verdict", "")),
        "outstanding": int(meta.get("required_fixes", 0) or 0),
    }


def _must_read(
    units: tuple[CollectedUnit, ...], findings: tuple[CollectedFinding, ...]
) -> tuple[str, ...]:
    """BLOCKER finding → L1 절 → MAJOR finding → finding 걸린 L2 절; top 5."""

    units_by_id = {unit.unit_id: unit for unit in units}

    def unit_key(unit: CollectedUnit) -> tuple[int, str]:
        return (-_rank(unit.confidence), unit.section)

    def finding_key(finding: CollectedFinding) -> tuple[int, str]:
        unit = units_by_id.get(finding.unit_id)
        section = unit.section if unit else finding.finding_id
        return (-_rank(finding.confidence or (unit.confidence if unit else "")), section)

    blocker = sorted(
        (f for f in findings if f.severity == "BLOCKER"), key=finding_key
    )
    l1 = sorted((u for u in units if u.level == "L1"), key=unit_key)
    major = sorted((f for f in findings if f.severity == "MAJOR"), key=finding_key)
    with_findings = {
        finding.unit_id for finding in findings if finding.severity != "BLOCKER"
    }
    l2 = sorted(
        (u for u in units if u.level == "L2" and u.unit_id in with_findings),
        key=unit_key,
    )
    ordered: list[str] = []
    for item in (*blocker, *l1, *major, *l2):
        ident = item.finding_id if isinstance(item, CollectedFinding) else item.unit_id
        if ident not in ordered:
            ordered.append(ident)
    return tuple(ordered[:5])


def build_collect(
    *,
    mr_iid: int,
    analyses: list[FileAnalysis],
    failed_files: tuple[str, ...],
    levelcheck: LevelCheck,
    literals: Literals,
    structure: Structure,
    changeset: Changeset,
    verifier_text: str,
    base_tree: dict[str, str],
    head_tree: dict[str, str],
) -> Collect:
    """Run the 9 gate steps over parsed artifacts — no LLM anywhere."""

    claimed: dict[str, tuple[object, FileAnalysis]] = {}
    for analysis in analyses:
        for unit in analysis.units:
            claimed[unit.unit_id] = (unit, analysis)

    units: list[CollectedUnit] = []
    for lit in literals.units:
        entry = claimed.get(lit.unit_id)
        unit = entry[0] if entry else None
        analysis = entry[1] if entry else None
        file, label = _section_label(structure, lit.section_id)
        units.append(
            CollectedUnit(
                unit_id=lit.unit_id,
                level=levelcheck.verified_level(lit.unit_id) or "L2",
                section=label,
                file=file or (analysis.path if analysis else lit.section_id),
                removed=lit.removed,
                added=lit.added,
                changed=lit.changed,
                before=str(getattr(unit, "before", "") or ""),
                after=str(getattr(unit, "after", "") or ""),
                confidence=str(getattr(analysis, "confidence", "") or ""),
            )
        )

    known_units = {unit.unit_id for unit in units} | set(claimed)
    findings: list[CollectedFinding] = []
    dropped: list[DroppedFinding] = []
    findings_in = 0
    seen_dedup: dict[str, str] = {}
    unit_confidence = {
        unit.unit_id: unit.confidence for unit in units
    }
    for analysis in analyses:
        for candidate in analysis.findings:
            findings_in += 1
            if candidate.unit_id not in known_units:
                dropped.append(
                    DroppedFinding(
                        candidate.finding_id,
                        "bad_ref",
                        f"unit_id {candidate.unit_id} 이 05-structure/20-analysis 에 없음",
                    )
                )
                continue
            if not candidate.evidence or not all(
                _quote_matches(item, base_tree, head_tree) for item in candidate.evidence
            ):
                dropped.append(
                    DroppedFinding(
                        candidate.finding_id,
                        "quote_mismatch",
                        "evidence quote 가 rev 지정 트리의 line ±3 창에서 불일치",
                    )
                )
                continue
            key = _dedup_key(candidate)
            if key in seen_dedup:
                dropped.append(
                    DroppedFinding(
                        candidate.finding_id,
                        "merged_dup",
                        f"{seen_dedup[key]} 와 같은 절 쌍·같은 리터럴",
                    )
                )
                continue
            seen_dedup[key] = candidate.finding_id
            findings.append(
                CollectedFinding(
                    finding_id=candidate.finding_id,
                    severity=_severity(candidate),
                    category=candidate.category,
                    unit_id=candidate.unit_id,
                    evidence=candidate.evidence,
                    claim=candidate.claim,
                    recommendation=candidate.recommendation,
                    confidence=unit_confidence.get(candidate.unit_id, ""),
                )
            )

    levels = {
        "L1": sum(1 for unit in units if unit.level == "L1"),
        "L2": sum(1 for unit in units if unit.level == "L2"),
        "L3": sum(1 for unit in units if unit.level == "L3"),
        "promoted": levelcheck.promoted,
    }
    counts = {
        "blocker": sum(1 for f in findings if f.severity == "BLOCKER"),
        "major": sum(1 for f in findings if f.severity == "MAJOR"),
        "minor": sum(1 for f in findings if f.severity == "MINOR"),
    }
    gate = {
        "files_parsed": len(analyses),
        "findings_in": findings_in,
        "dropped_bad_check_ref": sum(1 for d in dropped if d.reason == "bad_ref"),
        "dropped_quote_mismatch": sum(1 for d in dropped if d.reason == "quote_mismatch"),
        "merged_dup": sum(1 for d in dropped if d.reason == "merged_dup"),
        "findings_out": len(findings),
    }

    confidence_dist = {"high": 0, "medium": 0, "low": 0}
    for analysis in analyses:
        head = _confidence_head(analysis.confidence)
        if head in confidence_dist:
            confidence_dist[head] += 1
    uncertain = tuple(
        analysis.uncertain
        for analysis in analyses
        if analysis.uncertain and analysis.uncertain != "none"
    )

    if counts["blocker"] > 0:
        verdict = "BLOCK"
        reason = f"모순 {counts['blocker']}건 — 같은 키의 리터럴 값이 서로 충돌"
    elif counts["major"] > 0 or levels["L1"] > 0:
        parts = []
        if levels["L1"]:
            parts.append(f"맥락이 바뀐 절 {levels['L1']}건")
        if counts["major"]:
            parts.append(f"주의할 finding {counts['major']}건")
        verdict = "REVIEW"
        reason = " + ".join(parts)
    else:
        verdict = "PASS"
        reason = "값/표현 변경만 있고 finding 없음"

    return Collect(
        mr_iid=mr_iid,
        verdict=verdict,
        reason=reason,
        levels=levels,
        files={
            "added": sum(1 for e in changeset.files if e.status == "added"),
            "deleted": sum(1 for e in changeset.files if e.status == "removed"),
            "moved_sections": len(structure.moved),
        },
        counts=counts,
        gate=gate,
        verify=_verify_summary(verifier_text),
        coverage={"checks": 0, "answered": 0, "missing": []},
        confidence_dist=confidence_dist,
        uncertain=uncertain,
        failed_files=failed_files,
        must_read=_must_read(tuple(units), tuple(findings)),
        units=tuple(units),
        findings=tuple(findings),
        dropped=tuple(dropped),
    )


def render_collect(collect: Collect) -> str:
    """Render 50-collect.md — the reporter/render parse contract."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": collect.mr_iid,
                "verdict": collect.verdict,
                "reason": collect.reason,
                "levels": collect.levels,
                "files": collect.files,
                "counts": collect.counts,
                "gate": collect.gate,
                "verify": collect.verify,
                "coverage": collect.coverage,
                "confidence_dist": collect.confidence_dist,
                "uncertain": list(collect.uncertain),
                "failed_files": list(collect.failed_files),
                "must_read": list(collect.must_read),
            }
        ),
    ]
    for unit in collect.units:
        parts.append("")
        parts.append(
            render_section(
                "UNIT",
                unit.unit_id,
                {
                    "level": unit.level,
                    "section": unit.section,
                    "file": unit.file,
                    "removed": list(unit.removed),
                    "added": list(unit.added),
                    "changed": [
                        {"key": c.key, "from": c.from_value, "to": c.to_value}
                        for c in unit.changed
                    ],
                },
            )
        )
        parts.append(f"**before** {unit.before}")
        parts.append(f"**after** {unit.after}")
    for finding in collect.findings:
        parts.append("")
        parts.append(
            render_section(
                "FINDING",
                finding.finding_id,
                {
                    "severity": finding.severity,
                    "category": finding.category,
                    "unit_id": finding.unit_id,
                    "evidence": [
                        {
                            "role": item.role,
                            "rev": item.rev,
                            "file": item.file,
                            "line": item.line,
                            "quote": item.quote,
                        }
                        for item in finding.evidence
                    ],
                },
            )
        )
        parts.append(f"**claim** {finding.claim}")
        parts.append(f"**recommendation** {finding.recommendation}")
    for item in collect.dropped:
        parts.append("")
        parts.append(
            render_section(
                "DROPPED",
                item.finding_id,
                {"reason": item.reason, "detail": item.detail},
            )
        )
    return "\n".join(parts) + "\n"


def parse_collect(text: str) -> Collect:
    """Parse 50-collect.md back — the render node's input contract."""

    meta = parse_frontmatter(text)
    sections = parse_sections(text)

    def as_int(value: object, default: int = 0) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def dict_of(key: str) -> dict[str, object]:
        raw = meta.get(key)
        return dict(raw) if isinstance(raw, dict) else {}

    units: list[CollectedUnit] = []
    findings: list[CollectedFinding] = []
    dropped: list[DroppedFinding] = []
    for header, block_lines in _split_blocks(text):
        bold = _bold_lines(block_lines)
        fields = sections.get(header[1], {})
        if header[0] == "UNIT":
            changed_raw = fields.get("changed") or []
            units.append(
                CollectedUnit(
                    unit_id=header[1],
                    level=str(fields.get("level", "")),
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
                        for row in changed_raw
                        if isinstance(row, dict)
                    ),
                    before=bold.get("before", ""),
                    after=bold.get("after", ""),
                )
            )
        elif header[0] == "FINDING":
            evidence_raw = fields.get("evidence") or []
            findings.append(
                CollectedFinding(
                    finding_id=header[1],
                    severity=str(fields.get("severity", "")),
                    category=str(fields.get("category", "")),
                    unit_id=str(fields.get("unit_id", "")),
                    evidence=tuple(
                        Evidence(
                            role=str(row.get("role", "")),
                            rev=str(row.get("rev", "")),
                            file=str(row.get("file", "")),
                            line=as_int(row.get("line")),
                            quote=str(row.get("quote", "")),
                        )
                        for row in evidence_raw
                        if isinstance(row, dict)
                    ),
                    claim=bold.get("claim", ""),
                    recommendation=bold.get("recommendation", ""),
                )
            )
        elif header[0] == "DROPPED":
            dropped.append(
                DroppedFinding(
                    finding_id=header[1],
                    reason=str(fields.get("reason", "")),
                    detail=str(fields.get("detail", "")),
                )
            )

    def str_list(key: str) -> tuple[str, ...]:
        raw = meta.get(key)
        if isinstance(raw, list):
            return tuple(str(item) for item in raw)
        return ()

    return Collect(
        mr_iid=as_int(meta.get("mr_iid")),
        verdict=str(meta.get("verdict", "")),
        reason=str(meta.get("reason", "")),
        levels={k: as_int(v) for k, v in dict_of("levels").items()},
        files={k: as_int(v) for k, v in dict_of("files").items()},
        counts={k: as_int(v) for k, v in dict_of("counts").items()},
        gate={k: as_int(v) for k, v in dict_of("gate").items()},
        verify=dict_of("verify"),
        coverage=dict_of("coverage"),
        confidence_dist={k: as_int(v) for k, v in dict_of("confidence_dist").items()},
        uncertain=str_list("uncertain"),
        failed_files=str_list("failed_files"),
        must_read=str_list("must_read"),
        units=tuple(units),
        findings=tuple(findings),
        dropped=tuple(dropped),
    )
