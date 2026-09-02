"""20-analysis artifacts — parse + render the doc-analyzer satellite's output.

The satellite writes one file per changed md (20-analysis/<file_id>.md).
Everything downstream (levelcheck, verifier, collect) parses through this
module so the format has exactly one reader contract. Bold marker lines
('**before** …') are the satellite's two prose lines per unit — the parser
joins their wrapped continuation lines; everything else stays in the yaml
fence the shared frontmatter module already understands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import parse_frontmatter, parse_sections, render_section


@dataclass(frozen=True)
class Evidence:
    """One quoted source line a finding rests on."""

    role: str  # changed | conflicting | supporting …
    rev: str  # base | head — which tree the quote lives in
    file: str
    line: int
    quote: str


@dataclass(frozen=True)
class AnalysisUnit:
    """One changed section's claimed level + the satellite's two prose lines."""

    unit_id: str
    section_id: str
    level: str  # claimed L1 | L2 | L3
    before: str
    after: str


@dataclass(frozen=True)
class AnalysisFinding:
    """One contradiction/staleness observation with verbatim evidence."""

    finding_id: str
    unit_id: str
    category: str  # contradiction | stale_reference | terminology | completeness | dangling_link
    evidence: tuple[Evidence, ...]
    claim: str
    recommendation: str
    check_id: str | None = None  # 부가 checklist only — None in the 1st scope


@dataclass(frozen=True)
class FileAnalysis:
    """One 20-analysis file — frontmatter + every block."""

    file_id: str
    path: str
    units: tuple[AnalysisUnit, ...] = ()
    findings: tuple[AnalysisFinding, ...] = ()
    status: str = "OK"
    uncovered: str = "none"
    uncertain: str = "none"
    confidence: str = ""
    summary: str = ""


_BLOCK_HEADER = re.compile(r"^## ([A-Z][A-Z0-9_]*)(?: ([^\s]+))?")
_BOLD = re.compile(r"^\*\*(before|after|claim|recommendation)\*\*\s*(.*)$")
_FENCE = chr(96) * 3


def _text_str(value: object) -> str:
    return "" if value is None else str(value)


def _int_or_zero(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _parse_evidence(raw: object) -> tuple[Evidence, ...]:
    if not isinstance(raw, list):
        return ()
    items = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        items.append(
            Evidence(
                role=_text_str(row.get("role")),
                rev=_text_str(row.get("rev")),
                file=_text_str(row.get("file")),
                line=_int_or_zero(row.get("line")),
                quote=_text_str(row.get("quote")),
            )
        )
    return tuple(items)


def _bold_lines(lines: list[str]) -> dict[str, str]:
    """Join each '**marker** text' line with its wrapped continuations."""

    captured: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        match = _BOLD.match(line)
        if match:
            current = match.group(1)
            captured[current] = [match.group(2).strip()]
            continue
        if current is None:
            continue
        if line.startswith("## ") or line.startswith(_FENCE) or line.startswith("**"):
            current = None
            continue
        if line.strip():
            captured[current].append(line.strip())
    return {key: " ".join(part for part in parts if part) for key, parts in captured.items()}


def _split_blocks(text: str) -> list[tuple[tuple[str, str], list[str]]]:
    """[(('UNIT', 'u-x'), [body lines])] — headers keep their id."""

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


def parse_analysis(text: str) -> FileAnalysis:
    """Parse one 20-analysis file (ValueError on malformed frontmatter)."""

    meta = parse_frontmatter(text)
    sections = parse_sections(text)

    units: list[AnalysisUnit] = []
    findings: list[AnalysisFinding] = []
    summary = ""

    for header, block_lines in _split_blocks(text):
        bold = _bold_lines(block_lines)
        if header[0] == "UNIT":
            fields = sections.get(header[1], {})
            units.append(
                AnalysisUnit(
                    unit_id=header[1],
                    section_id=_text_str(fields.get("section_id")),
                    level=_text_str(fields.get("level")),
                    before=bold.get("before", ""),
                    after=bold.get("after", ""),
                )
            )
        elif header[0] == "FINDING":
            fields = sections.get(header[1], {})
            findings.append(
                AnalysisFinding(
                    finding_id=header[1],
                    unit_id=_text_str(fields.get("unit_id")),
                    category=_text_str(fields.get("category")),
                    evidence=_parse_evidence(fields.get("evidence")),
                    claim=bold.get("claim", ""),
                    recommendation=bold.get("recommendation", ""),
                    check_id=_text_str(fields.get("check_id")) or None,
                )
            )
        elif header[0] == "SUMMARY":
            summary = "\n".join(
                line for line in block_lines if line.strip() and not line.startswith(_FENCE)
            )

    return FileAnalysis(
        file_id=_text_str(meta.get("file_id")),
        path=_text_str(meta.get("path")),
        units=tuple(units),
        findings=tuple(findings),
        status=_text_str(meta.get("STATUS")) or "OK",
        uncovered=_text_str(meta.get("UNCOVERED")) or "none",
        uncertain=_text_str(meta.get("UNCERTAIN")) or "none",
        confidence=_text_str(meta.get("CONFIDENCE")),
        summary=summary,
    )


def _evidence_rows(evidence: tuple[Evidence, ...]) -> list[dict[str, object]]:
    return [
        {
            "role": item.role,
            "rev": item.rev,
            "file": item.file,
            "line": item.line,
            "quote": item.quote,
        }
        for item in evidence
    ]


def render_analysis(analysis: FileAnalysis) -> str:
    """Render 20-analysis markdown — test/double-side of the parse contract."""

    levels = {"L1": 0, "L2": 0, "L3": 0}
    for unit in analysis.units:
        if unit.level in levels:
            levels[unit.level] += 1
    parts = [
        "---",
        f"file_id: {analysis.file_id}",
        f'path: "{analysis.path}"',
        f"units: {len(analysis.units)}",
        "levels: {" + ", ".join(f"{k}: {v}" for k, v in levels.items()) + "}",
        f"findings: {len(analysis.findings)}",
        f"STATUS: {analysis.status}",
        f"UNCOVERED: {analysis.uncovered}",
        f"UNCERTAIN: {analysis.uncertain}",
        f"CONFIDENCE: {analysis.confidence}",
        "---",
    ]
    for unit in analysis.units:
        parts.append("")
        parts.append(
            render_section(
                "UNIT",
                unit.unit_id,
                {"section_id": unit.section_id, "level": unit.level},
            )
        )
        parts.append(f"**before** {unit.before}")
        parts.append(f"**after** {unit.after}")
    for finding in analysis.findings:
        parts.append("")
        fields: dict[str, object] = {
            "unit_id": finding.unit_id,
            "category": finding.category,
            "evidence": _evidence_rows(finding.evidence),
        }
        if finding.check_id:
            fields["check_id"] = finding.check_id
        parts.append(render_section("FINDING", finding.finding_id, fields))
        parts.append(f"**claim** {finding.claim}")
        parts.append(f"**recommendation** {finding.recommendation}")
    if analysis.summary:
        parts.append("")
        parts.append("## SUMMARY")
        parts.append(analysis.summary)
    return "\n".join(parts) + "\n"


def load_analyses(analysis_dir: Path) -> list[FileAnalysis]:
    """Parse every 20-analysis/*.md — unparseable files raise (caller decides)."""

    if not analysis_dir.is_dir():
        return []
    return [
        parse_analysis(path.read_text(encoding="utf-8"))
        for path in sorted(analysis_dir.glob("*.md"))
    ]


def load_analyses_split(analysis_dir: Path) -> tuple[list[FileAnalysis], tuple[str, ...]]:
    """Parse 20-analysis/*.md, splitting out files that violate the schema.

    The collector's step 1: a malformed file becomes a failed_files entry
    (visible in 50-collect's frontmatter) instead of aborting the whole
    pipeline — partial failure stays countable, the loop still ends.
    """

    if not analysis_dir.is_dir():
        return [], ()
    parsed: list[FileAnalysis] = []
    failed: list[str] = []
    for path in sorted(analysis_dir.glob("*.md")):
        try:
            parsed.append(parse_analysis(path.read_text(encoding="utf-8")))
        except ValueError:
            failed.append(path.name)
    return parsed, tuple(failed)
