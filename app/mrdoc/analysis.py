"""20-analysis artifacts — parse + render the doc-analyzer satellite's output.

The satellite writes one file per changed md (20-analysis/<file_id>.md).
Everything downstream (levelcheck, verifier, collect) parses through this
module so the format has exactly one reader contract.

The v2 schema is deliberately small: per unit one '**설명**' paragraph and an
optional class on 표현 candidates, per file one FILE_SUMMARY. 판정하는
필드는 없다 — 심각도 · 권고 · 머지 의견을 쓸 자리가 스키마에 없으니 쓸 수
없다. Bold marker lines are the satellite's prose; the
parser joins their wrapped continuation lines, and everything else stays in
the yaml fence the shared frontmatter module already understands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)

#: What the analyzer may claim. 의미(값)·구조 are tool-determined, so the
#: satellite leaves those units unclassified (null) instead of guessing.
CLASS_VALUES = ("의미", "표현")


@dataclass(frozen=True)
class AnalysisUnit:
    """One changed section: the satellite's 설명, plus a class when claimed."""

    unit_id: str
    section_id: str
    klass: str  # '의미' | '표현' | '' (null — tool-determined unit)
    explanation: str


@dataclass(frozen=True)
class FileAnalysis:
    """One 20-analysis file — frontmatter + every block."""

    file_id: str
    path: str
    units: tuple[AnalysisUnit, ...] = ()
    summary_refs: tuple[str, ...] = ()
    summary: str = ""
    status: str = "OK"
    uncovered: str = "none"
    uncertain: str = "none"
    confidence: str = ""

    def class_counts(self) -> dict[str, int]:
        """The frontmatter's classes block — 표현 claimed, everything else 의미.

        '후보판정' counts the calls the satellite actually made; a null class
        is not an abstention it can hide behind, it is the tool's territory.
        """

        expression = sum(1 for unit in self.units if unit.klass == "표현")
        judged = sum(1 for unit in self.units if unit.klass)
        return {
            "의미": len(self.units) - expression,
            "표현": expression,
            "후보판정": judged,
        }


_BLOCK_HEADER = re.compile(r"^## ([A-Z][A-Z0-9_]*)(?: ([^\s]+))?")
#: Closed marker set: an arbitrary '**...**' inside prose must not read as one.
_BOLD = re.compile(r"^\*\*(설명|FILE_SUMMARY|before|after)\*\*\s*(.*)$")
_FENCE = chr(96) * 3


def _text_str(value: object) -> str:
    return "" if value is None else str(value)


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


def _prose(block_lines: list[str]) -> str:
    """Everything under a block that is neither its yaml fence nor a marker."""

    out: list[str] = []
    inside = False
    for line in block_lines:
        if line.startswith(_FENCE):
            inside = not inside
            continue
        if inside or not line.strip():
            continue
        out.append(line.strip())
    return " ".join(out)


def parse_analysis(text: str) -> FileAnalysis:
    """Parse one 20-analysis file (ValueError on malformed frontmatter)."""

    meta = parse_frontmatter(text)
    sections = parse_sections(text)

    units: list[AnalysisUnit] = []
    summary_refs: tuple[str, ...] = ()
    summary = ""

    for header, block_lines in _split_blocks(text):
        if header[0] == "UNIT":
            fields = sections.get(header[1], {})
            units.append(
                AnalysisUnit(
                    unit_id=header[1],
                    section_id=_text_str(fields.get("section_id")),
                    klass=_text_str(fields.get("class")),
                    explanation=_bold_lines(block_lines).get("설명", ""),
                )
            )
        elif header[0] == "FILE_SUMMARY":
            fields = sections.get(header[1], {})
            raw = fields.get("refs")
            summary_refs = tuple(
                _text_str(ref) for ref in raw if _text_str(ref)
            ) if isinstance(raw, list) else ()
            summary = _prose(block_lines)
        # RECEIPT (and any other block) is accepted and ignored — the design
        # keeps the return copy in the artifact for 529 recovery.

    return FileAnalysis(
        file_id=_text_str(meta.get("file_id")),
        path=_text_str(meta.get("path")),
        units=tuple(units),
        summary_refs=summary_refs,
        summary=summary,
        status=_text_str(meta.get("STATUS")) or "OK",
        uncovered=_text_str(meta.get("UNCOVERED")) or "none",
        uncertain=_text_str(meta.get("UNCERTAIN")) or "none",
        confidence=_text_str(meta.get("CONFIDENCE")),
    )


def render_analysis(analysis: FileAnalysis) -> str:
    """Render 20-analysis markdown — and the analyzer prompt's own template.

    The satellite prompt is built by calling this with placeholder values, so
    prompt and parser share one source. The measured failure was the other
    way round: a hand-written template told the satellite to use block
    sequences, the parser only took inline flow, and the file was discarded
    whole while the pipeline reported complete.
    """

    parts = [
        render_frontmatter(
            {
                "file_id": analysis.file_id,
                "path": analysis.path,
                "units": len(analysis.units),
                "classes": analysis.class_counts(),
                "STATUS": analysis.status,
                "UNCOVERED": analysis.uncovered,
                "UNCERTAIN": analysis.uncertain,
                "CONFIDENCE": analysis.confidence,
            }
        )
    ]
    for unit in analysis.units:
        parts.append("")
        parts.append(
            render_section(
                "UNIT",
                unit.unit_id,
                {"section_id": unit.section_id, "class": unit.klass or None},
            )
        )
        parts.append(f"**설명** {unit.explanation}")
    if analysis.summary or analysis.summary_refs:
        parts.append("")
        parts.append(
            render_section("FILE_SUMMARY", "", {"refs": list(analysis.summary_refs)})
        )
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
