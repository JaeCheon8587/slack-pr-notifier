"""Minimal YAML subset for mrdoc artifacts — render + parse, zero dependencies.

The design pins artifact formats to a constrained frontmatter vocabulary
('key: scalar', flow maps '{k: v}', flow lists '[a, b]'); a full YAML
parser would be a new dependency for a format we fully control. This module
renders exactly what it can parse back, and refuses anything else loudly
(an unparseable artifact must abort the pipeline, not silently degrade).
"""

from __future__ import annotations

import re

_TQ = chr(96) * 3  # triple backtick, assembled so patches carry no fence text
_SCALAR_SAFE = re.compile(r"^[A-Za-z0-9_./@+~-]+$")
_INT = re.compile(r"^-?\d+$")
_SECTION_HEADER = re.compile(r"^## ([A-Z][A-Z0-9_]*)(?: ([^\s]+))?$")



def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if _SCALAR_SAFE.match(text):
        return text
    return _quote(text)


def _parse_scalar(text: str) -> object:
    text = text.strip()
    if text in ("null", "~", ""):
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if _INT.match(text):
        return int(text)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        body = text[1:-1]
        return body.replace('\\\"', '"').replace("\\\\", "\\")
    return text


def _split_flow(body: str) -> list[str]:
    """Split a flow body on top-level commas (strings may contain commas)."""

    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    for i, ch in enumerate(body):
        if in_str:
            buf.append(ch)
            if ch == '"' and (i == 0 or body[i - 1] != "\\"):
                in_str = False
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch in "{[":
            depth += 1
            buf.append(ch)
        elif ch in "}]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in (part.strip() for part in parts) if p]


def _parse_flow(text: str) -> object:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return [_parse_flow(item) for item in _split_flow(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        result: dict[str, object] = {}
        for item in _split_flow(text[1:-1]):
            if ":" not in item:
                raise ValueError(f"flow map entry without colon: {item!r}")
            key, _, value = item.partition(":")
            result[key.strip()] = _parse_scalar(value)
        return result
    return _parse_scalar(text)


def _render_flow(value: dict[str, object] | list[object]) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_render_flow_item(item) for item in value) + "]"
    return "{" + ", ".join(f"{k}: {_render_scalar(v)}" for k, v in value.items()) + "}"


def _render_flow_item(item: object) -> str:
    if isinstance(item, dict):
        return _render_flow(item)
    return _render_scalar(item)


def _render_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return _render_flow(value)
    return _render_scalar(value)


def render_frontmatter(data: dict[str, object]) -> str:
    """Render the leading '---' fence — one line per key, flow style for nests."""

    lines = ["---"]
    lines.extend(f"{key}: {_render_value(value)}" for key, value in data.items())
    lines.append("---")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parse the leading '---' fence into a dict (ValueError if malformed)."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("artifact does not start with a frontmatter fence")
    result: dict[str, object] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return result
        if not line.strip():
            continue
        match = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if not match:
            raise ValueError(f"unparseable frontmatter line: {line!r}")
        key, raw = match.groups()
        result[key] = _parse_flow(raw) if raw[:1] in "{[" else _parse_scalar(raw)
    raise ValueError("frontmatter fence never closes")


def render_section(section_type: str, identifier: str, fields: dict[str, object]) -> str:
    """Render one '## TYPE id' block with its first yaml fence (parser scope)."""

    header = f"## {section_type} {identifier}".rstrip()
    fence = "\n".join(f"{k}: {_render_value(v)}" for k, v in fields.items())
    return f"{header}\n\n{_TQ}yaml\n{fence}\n{_TQ}"


def parse_sections(text: str) -> dict[str, dict[str, object]]:
    """Extract every '## TYPE id' block's first yaml fence.

    Parser scope per the design: the header regex plus the first fenced block
    under it — nothing more (narrative between blocks is human text).
    Returns {identifier: fields}; blocks without an id use the TYPE as key.
    """

    result: dict[str, dict[str, object]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _SECTION_HEADER.match(lines[i])
        if not match:
            i += 1
            continue
        identifier = match.group(2) or match.group(1)
        j = i + 1
        while j < len(lines) and not lines[j].startswith(_TQ):
            if _SECTION_HEADER.match(lines[j]):
                break
            j += 1
        if j < len(lines) and lines[j].startswith(_TQ):
            fence_lines: list[str] = []
            k = j + 1
            while k < len(lines) and not lines[k].startswith(_TQ):
                fence_lines.append(lines[k])
                k += 1
            result[identifier] = parse_frontmatter(
                "\n".join(["---", *fence_lines, "---"])
            )
            i = k
        else:
            i = j
    return result
