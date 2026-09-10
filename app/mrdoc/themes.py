"""45-themes artifact — parse + render the themes satellite's output.

The themes satellite sees every changed unit of the MR in one compact
view and groups them into cross-cutting subjects. Its write surface is
deliberately three fields — a title, one line, the member unit ids —
because everything else (file counts, coverage) is measured downstream
from the members rather than trusted from prose. The design's evidence
boundary applies to the line just as it does to 설명: 무엇이 / 어떻게
까지, never 왜 · 그래서 · 평가.
"""

from __future__ import annotations

from dataclasses import dataclass

from .analysis import _bold_lines, _split_blocks
from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)


@dataclass(frozen=True)
class Theme:
    """One cross-cutting subject — members listed, counts measured later."""

    theme_id: str
    title: str
    line: str
    units: tuple[str, ...] = ()


@dataclass(frozen=True)
class Themes:
    """One 45-themes file — frontmatter + every THEME block."""

    mr_iid: int
    status: str = "OK"
    uncovered: str = "none"
    uncertain: str = "none"
    confidence: str = ""
    themes: tuple[Theme, ...] = ()


def render_themes(themes: Themes) -> str:
    """Render 45-themes markdown — and the themes prompt's own template."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": themes.mr_iid,
                "themes": len(themes.themes),
                "STATUS": themes.status,
                "UNCOVERED": themes.uncovered,
                "UNCERTAIN": themes.uncertain,
                "CONFIDENCE": themes.confidence,
            }
        )
    ]
    for theme in themes.themes:
        parts.append("")
        parts.append(
            render_section(
                "THEME",
                theme.theme_id,
                {"title": theme.title, "units": list(theme.units)},
            )
        )
        parts.append(f"**한 줄** {theme.line}")
    return "\n".join(parts) + "\n"


def parse_themes(text: str) -> Themes:
    """Parse 45-themes.md back (ValueError on malformed frontmatter)."""

    meta = parse_frontmatter(text)
    sections = parse_sections(text)

    themes: list[Theme] = []
    for header, block_lines in _split_blocks(text):
        if header[0] != "THEME":
            continue  # RECEIPT (and any other block) is accepted and ignored
        fields = sections.get(header[1], {})
        raw = fields.get("units")
        themes.append(
            Theme(
                theme_id=header[1],
                title=str(fields.get("title") or ""),
                line=_bold_lines(block_lines).get("한 줄", ""),
                units=tuple(str(u) for u in raw if str(u))
                if isinstance(raw, list)
                else (),
            )
        )

    count = meta.get("themes")
    if isinstance(count, int) and count != len(themes):
        raise ValueError("frontmatter theme count does not match blocks")
    return Themes(
        mr_iid=int(meta.get("mr_iid") or 0),
        status=str(meta.get("STATUS") or "OK"),
        uncovered=str(meta.get("UNCOVERED") or "none"),
        uncertain=str(meta.get("UNCERTAIN") or "none"),
        confidence=str(meta.get("CONFIDENCE") or ""),
        themes=tuple(themes),
    )
