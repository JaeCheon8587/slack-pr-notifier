"""Markdown section parser — the coordinate system every later node stands on.

The design calls this node's parsing "the real workload": ATX ('#') AND
setext ('===' underlines) must both count, '#' inside fenced code blocks
must NOT, and '---' is simultaneously a frontmatter delimiter, a setext h2
underline and a thematic break. A wrong tree propagates deterministically
into every downstream LLM node, so all ambiguity rules live here, tested,
in one place (see docs/mrdoc-pipeline.html "구현 주의").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BACKTICK = chr(96)
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:\s+(.*))?$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^ {0,3}((" + re.escape(_BACKTICK) + "{3,}|~{3,}))" + r"(\S*.*)$")
_FENCE_OPEN = re.compile(
    r"^ {0,3}(" + re.escape(_BACKTICK) + "{3,}|~{3,})" + r"\s*[^\s]*\s*$"
)


@dataclass(frozen=True)
class Section:
    """One heading-delimited block, 1-based inclusive line range."""

    heading: str
    level: int
    heading_path: str
    start: int
    end: int
    ordinal: int


def _strip_atx_closing(title: str) -> str:
    """Drop a trailing ' ###' closing sequence (CommonMark ATX)."""

    return re.sub(r"\s+#+\s*$", "", title).strip()


def _frontmatter_end(lines: list[str]) -> int:
    """Index just past the leading '---' fence, or 0 when there is none."""

    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0  # unterminated: treat whole file as plain markdown


def parse_sections(text: str) -> list[Section]:
    """Split markdown into sections with full heading paths and ordinals.

    Rules (CommonMark subset, tightened for docs repos):
    - headings: ATX with <=3 leading spaces; setext via '=' (h1) / '-' (h2)
      underline under a paragraph line;
    - fenced blocks (backtick or tilde, 3+) hide everything inside, and the
      closing fence must be same-char, at least as long, nothing after;
    - '---' after a blank line is a thematic break, never a heading;
    - 4-space indented lines are code, never headings;
    - content before the first heading is ignored (no section).
    """

    lines = text.splitlines()
    n = len(lines)
    start_idx = _frontmatter_end(lines)

    events: list[tuple[int, str, int]] = []  # (start_line_1based, title, level)
    fence_char: str | None = None
    fence_len = 0
    para_start: int | None = None

    i = start_idx
    while i < n:
        line = lines[i]
        if fence_char is not None:
            stripped = line.strip()
            if (
                stripped
                and stripped[0] == fence_char
                and len(stripped) >= fence_len
                and all(ch == fence_char for ch in stripped)
            ):
                fence_char = None
            i += 1
            continue

        fence = _FENCE_OPEN.match(line)
        if fence:
            fence_char = fence.group(1)[0]
            fence_len = len(fence.group(1))
            para_start = None
            i += 1
            continue

        atx = _ATX.match(line)
        if atx and (atx.group(2) or "").strip():
            title = _strip_atx_closing(atx.group(2) or "")
            if title:
                events.append((i + 1, title, len(atx.group(1))))
                para_start = None
                i += 1
                continue

        setext = _SETEXT.match(line)
        if setext and para_start is not None and i > 0 and lines[i - 1].strip():
            level = 1 if setext.group(1)[0] == "=" else 2
            events.append((para_start + 1, lines[i - 1].strip(), level))
            para_start = None
            i += 1
            continue

        if line.strip():
            if para_start is None:
                para_start = i
        else:
            para_start = None
        i += 1

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    for idx, (start, title, level) in enumerate(events):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(title for _, title in stack)
        ordinal = seen.get(path, 0) + 1
        seen[path] = ordinal
        end = events[idx + 1][0] - 1 if idx + 1 < len(events) else n
        sections.append(
            Section(
                heading=title,
                level=level,
                heading_path=path,
                start=start,
                end=max(end, start),
                ordinal=ordinal,
            )
        )
    return sections


def section_lines(text: str, section: Section) -> list[str]:
    """The section's raw lines, heading line included (quote contrast source)."""

    return text.splitlines()[section.start - 1 : section.end]


def section_text(text: str, section: Section) -> str:
    return "\n".join(section_lines(text, section))
