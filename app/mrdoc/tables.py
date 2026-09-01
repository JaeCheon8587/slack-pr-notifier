"""Shared pipe-table render/parse for artifact sections."""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("|", "\\|")


def _split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes; '\\|' becomes a literal pipe."""

    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf or line.endswith("|"):
        cells.append("".join(buf).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    return cells[:-1] if cells and cells[-1] == "" and len(cells) > 1 else cells


def render_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(_escape(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def parse_table(text: str, start_marker: str) -> list[list[str]]:
    """Rows of the pipe table that follows a '## MARKER' heading."""

    lines = text.splitlines()
    rows: list[list[str]] = []
    active = False
    header_seen = False
    for line in lines:
        if line.startswith("## "):
            active = line[3:].strip() == start_marker
            header_seen = False
            continue
        if not active or not line.startswith("|"):
            continue
        cells = _split_row(line.strip())
        if not header_seen:
            header_seen = True  # first pipe row is the header
            continue
        if all(set(cell) <= {"-", ""} for cell in cells):
            continue  # separator row
        rows.append(cells)
    return rows
