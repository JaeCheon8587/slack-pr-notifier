"""The seven revise artifacts — dataclasses, parsers, renderers, templates.

docs/revise-workflow.html Part 3 is the field contract; this module is its
only implementation. The empty prompt templates come out of the same
renderers (`intent_template` / `edit_template` / `verify_template`), which is
the design's "템플릿은 렌더러가" rule: a prompt and the parser that reads the
answer share one source, so drift between them is physically impossible
rather than merely discouraged.

Shape, common to every artifact (mrdoc's, unchanged):

    ---
    YAML frontmatter — machine fields + the four fixed uppercase fields
    ---

    ## <TYPE> <id>
    ```yaml
    machine fields
    ```
    **narrative** one or two lines

Three rules are enforced, not requested:

- `STATUS` / `UNCOVERED` / `UNCERTAIN` / `CONFIDENCE` are mandatory on every
  artifact and an empty value is the string `none`. Optional fields cannot
  tell "not applicable" from "did not do it", which is the whole point.
- list-of-object values are accepted in one-line flow form only
  (`key: [{a: 1}, {b: 2}]`). A block sequence is rejected: in the measured
  mrdoc run a prompt that asked for one made a whole artifact unparseable.
- a parser looks at `^## TYPE id$` plus the first yaml fence beneath it, and
  nothing else. Anything malformed raises `ValueError` (app/mrdoc/frontmatter
  convention) — an unparseable artifact aborts a round, it never degrades.

Field names are the artifact keys verbatim, Korean ones included
(`대상문서`, `범위`, `판정문`, …). The renderers and parsers therefore carry
no key mapping table to fall out of date; `from`/`to` are the one exception
because they are Python keywords (mrdoc's `ChangedValue` does the same).

Round-trip contract: `parse_x(render_x(x)) == x` for all seven. Parsers
coerce every scalar to the dataclass's declared type, so a numeric-looking
string field (`project_id="1009"`, a digits-only sha) survives the trip.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.mrdoc.frontmatter import parse_frontmatter, render_frontmatter

_TQ = chr(96) * 3  # triple backtick, assembled so patches carry no fence text
_SCALAR_SAFE = re.compile(r"^[A-Za-z0-9_./@+~-]+$")
_INT = re.compile(r"^-?\d+$")
_HEADER = re.compile(r"^## ([A-Za-z][A-Za-z0-9_]*)(?:\s+(\S+))?\s*$")
_FIELD = re.compile(r"^([^\s:]+):\s*(.*)$")
_BLOCK_SEQ = re.compile(r"^\s*-(\s|$)")

#: The four fixed uppercase fields, in render order.
REQUIRED_KEYS: tuple[str, ...] = ("STATUS", "UNCOVERED", "UNCERTAIN", "CONFIDENCE")

#: The change kinds 3a must commit to — the design's step 2 (생성/수정/삭제 분류).
#: A closed enum, enforced at parse: a stage that invents a fourth kind fails
#: the node rather than drifting the contract.
OP_KINDS: tuple[str, ...] = ("create", "modify", "delete")

#: What a LITERAL check can say about how many occurrences changed: nothing.
#: `extract_literals` dedups on (kind, key, value), so occurrence counts are
#: gone by the time the gate sees them — see `nodes.build_gate`.
OCCURRENCES_UNAVAILABLE = "unavailable"


# --------------------------------------------------------------------------
# yaml subset — the section fences (frontmatter reuses app/mrdoc/frontmatter)
# --------------------------------------------------------------------------


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
    return text if _SCALAR_SAFE.match(text) else _quote(text)


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
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def _split_flow(body: str) -> list[str]:
    """Split a flow body on top-level commas — strings may contain commas."""

    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    for i, char in enumerate(body):
        if in_str:
            buf.append(char)
            if char == '"' and (i == 0 or body[i - 1] != "\\"):
                in_str = False
            continue
        if char == '"':
            in_str = True
            buf.append(char)
        elif char in "{[":
            depth += 1
            buf.append(char)
        elif char in "}]":
            depth -= 1
            buf.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    if buf:
        parts.append("".join(buf))
    return [part for part in (raw.strip() for raw in parts) if part]


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


def _render_value(value: object) -> str:
    if isinstance(value, Mapping):
        inner = ", ".join(f"{k}: {_render_scalar(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    return _render_scalar(value)


def _parse_value(raw: str) -> object:
    raw = raw.strip()
    return _parse_flow(raw) if raw[:1] in ("[", "{") else _parse_scalar(raw)


# --------------------------------------------------------------------------
# block layer — '## TYPE id' + its first yaml fence + trailing narrative
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Block:
    type: str
    id: str
    fields: dict[str, object]
    narrative: str

    @property
    def where(self) -> str:
        return f"## {self.type} {self.id}".rstrip()


def _parse_fields(body: list[str], where: str) -> dict[str, object]:
    """Parse one yaml fence's `key: value` lines into a dict.

    Both of yaml's sequence styles are accepted, because they carry exactly
    the same data and a prompt cannot hold a model to one of them. The
    templates show one-line flow (`evidence: [{...}, {...}]`) and a stage
    will still hand back the block form:

        evidence:
        - {file: "docs/install.md", line: "9", before: "...", after: "..."}

    Rejecting that cost a measured smoke round its verify stage after the
    edit had already succeeded, which is a parser opinion masquerading as a
    contract violation. A `- item` line with no key above it is still an
    error -- there is nothing to attach it to.
    """

    fields: dict[str, object] = {}
    pending: str | None = None
    for line in body:
        if not line.strip():
            continue
        if _BLOCK_SEQ.match(line):
            if pending is None:
                raise ValueError(
                    f"{where}: block sequence item with no key above it ({line.strip()!r})"
                )
            item = line.strip()[1:].strip()
            if item:
                current = fields.get(pending)
                if not isinstance(current, list):
                    current = []
                    fields[pending] = current
                current.append(_parse_value(item))
            continue
        match = _FIELD.match(line)
        if not match:
            raise ValueError(f"{where}: unparseable field line: {line!r}")
        key, raw = match.group(1).strip(), match.group(2).strip()
        fields[key] = _parse_value(raw)
        # An empty value may be a bare scalar or the head of a block sequence;
        # which one it is is only known once the next line is read.
        pending = key if not raw else None
    return fields


def _skip_frontmatter(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    raise ValueError("frontmatter fence never closes")


def _parse_blocks(text: str) -> list[_Block]:
    """Every '## TYPE id' block in document order (duplicate ids kept apart).

    A dict keyed on the id would silently drop the second LITERAL block for a
    unit two opinions both touch, so blocks stay a list.
    """

    lines = text.splitlines()
    i = _skip_frontmatter(lines)
    blocks: list[_Block] = []
    while i < len(lines):
        header = _HEADER.match(lines[i])
        if not header:
            i += 1
            continue
        block_type, block_id = header.group(1), header.group(2) or ""
        where = f"## {block_type} {block_id}".rstrip()
        i += 1
        fields: dict[str, object] = {}
        narrative: list[str] = []
        fence_done = False
        while i < len(lines) and not _HEADER.match(lines[i]):
            if not fence_done and lines[i].startswith(_TQ):
                body: list[str] = []
                i += 1
                while i < len(lines) and not lines[i].startswith(_TQ):
                    body.append(lines[i])
                    i += 1
                if i >= len(lines):
                    raise ValueError(f"{where}: yaml fence never closes")
                i += 1
                fields = _parse_fields(body, where)
                fence_done = True
                continue
            narrative.append(lines[i])
            i += 1
        blocks.append(_Block(block_type, block_id, fields, "\n".join(narrative).strip()))
    return blocks


def _render_block(
    block_type: str, block_id: str, fields: Mapping[str, object], narrative: str = ""
) -> str:
    parts = [f"## {block_type} {block_id}".rstrip(), ""]
    if fields:
        body = "\n".join(f"{key}: {_render_value(value)}" for key, value in fields.items())
        parts.extend([f"{_TQ}yaml", body, _TQ])
        if narrative:
            parts.append("")
    if narrative:
        parts.append(narrative)
    return "\n".join(parts)


def _of_type(blocks: Sequence[_Block], block_type: str) -> list[_Block]:
    return [block for block in blocks if block.type == block_type]


def _one(blocks: Sequence[_Block], block_type: str, where: str) -> _Block:
    found = _of_type(blocks, block_type)
    if len(found) != 1:
        raise ValueError(f"{where}: expected exactly one '## {block_type}' block")
    return found[0]


# --------------------------------------------------------------------------
# coercion — parsers pin every value to the declared type (round-trip)
# --------------------------------------------------------------------------


def _need(fields: Mapping[str, object], key: str, where: str) -> object:
    if key not in fields:
        raise ValueError(f"{where}: missing field {key!r}")
    return fields[key]


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _as_op(value: object, where: str) -> str:
    op = _as_str(value)
    if op not in OP_KINDS:
        raise ValueError(f"{where}: op must be one of {OP_KINDS}, got {op!r}")
    return op


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"expected an integer, got {value!r}")
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"expected an integer, got {value!r}") from error


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value in ("true", "false"):
        return value == "true"
    raise ValueError(f"expected a boolean, got {value!r}")


def _as_list(value: object, where: str, key: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where}: {key} must be a flow list, got {value!r}")
    return value


def _as_strs(value: object, where: str, key: str) -> tuple[str, ...]:
    return tuple(_as_str(item) for item in _as_list(value, where, key))


def _as_ints(value: object, where: str, key: str) -> tuple[int, ...]:
    return tuple(_as_int(item) for item in _as_list(value, where, key))


def _as_maps(value: object, where: str, key: str) -> tuple[Mapping[str, object], ...]:
    rows = _as_list(value, where, key)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError(f"{where}: {key} entries must be flow maps, got {row!r}")
    return tuple(row for row in rows if isinstance(row, Mapping))


def _as_map(value: object, where: str, key: str) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: {key} must be a flow map or null, got {value!r}")
    return value


def _as_range(value: object, where: str, key: str) -> tuple[int, int]:
    text = _as_str(value)
    parts = text.split("-")
    if len(parts) != 2:
        raise ValueError(f"{where}: {key} must be '<start>-<end>', got {text!r}")
    return _as_int(parts[0]), _as_int(parts[1])


def _as_pair(value: object, where: str, key: str, sep: str = "/") -> tuple[int, int]:
    text = _as_str(value)
    parts = text.split(sep)
    if len(parts) != 2:
        raise ValueError(f"{where}: {key} must be '<n> {sep} <n>', got {text!r}")
    return _as_int(parts[0].strip()), _as_int(parts[1].strip())


def _check(meta: Mapping[str, object], key: str, expected: object, where: str) -> None:
    """Frontmatter counts are derived on render, so a mismatch means tampering."""

    if key in meta and meta[key] != expected:
        raise ValueError(f"{where}: frontmatter {key} is {meta[key]!r}, blocks say {expected!r}")


# --------------------------------------------------------------------------
# the four fixed fields
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Required:
    """STATUS / UNCOVERED / UNCERTAIN / CONFIDENCE — never omitted, never empty.

    An empty value is the string `none`; the renderer refuses `""` so a
    silent omission cannot reach an artifact.
    """

    STATUS: str
    UNCOVERED: str
    UNCERTAIN: str
    CONFIDENCE: str


def _render_required(required: Required) -> dict[str, object]:
    rendered: dict[str, object] = {}
    for key in REQUIRED_KEYS:
        value = _as_str(getattr(required, key))
        if not value:
            raise ValueError(f"required field {key} is empty — write 'none', never nothing")
        rendered[key] = value
    return rendered


def _parse_required(meta: Mapping[str, object], where: str) -> Required:
    missing = [key for key in REQUIRED_KEYS if meta.get(key) is None or meta.get(key) == ""]
    if missing:
        raise ValueError(f"{where}: required field(s) missing or empty: {', '.join(missing)}")
    return Required(**{key: _as_str(meta[key]) for key in REQUIRED_KEYS})


# --------------------------------------------------------------------------
# 00-intent.md — 3a
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Opinion:
    """One structured opinion — the 8 fields of §S4② plus `op`, `연관문서`
    and `search_terms`.

    `op` is the create/modify/delete classification the design's step 2 asks
    for; `연관문서` is 3a's suspicion-level list of documents the change may
    also touch — the impact node verifies those mechanically and reports the
    ones no evidence confirmed.
    """

    opinion_id: int
    op: str  # create | modify | delete — OP_KINDS
    대상문서: tuple[str, ...]
    연관문서: tuple[str, ...]
    범위: str  # local | cross-doc
    대상주장: str
    수정방향: str
    근거: str
    통합: tuple[str, ...]
    충돌해소: str
    search_terms: tuple[str, ...]


@dataclass(frozen=True)
class Intent:
    mr_iid: int
    project_id: str
    round: int
    base_sha: str
    opinions: tuple[Opinion, ...]
    required: Required


def render_intent(intent: Intent) -> str:
    """Render 00-intent.md — frontmatter plus one OPINION block per opinion."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": intent.mr_iid,
                "project_id": intent.project_id,
                "round": intent.round,
                "base_sha": intent.base_sha,
                "opinions": len(intent.opinions),
                **_render_required(intent.required),
            }
        )
    ]
    for opinion in intent.opinions:
        parts.append("")
        parts.append(
            _render_block(
                "OPINION",
                str(opinion.opinion_id),
                {
                    "opinion_id": opinion.opinion_id,
                    "op": opinion.op,
                    "대상문서": list(opinion.대상문서),
                    "연관문서": list(opinion.연관문서),
                    "범위": opinion.범위,
                    "대상주장": opinion.대상주장,
                    "수정방향": opinion.수정방향,
                    "근거": opinion.근거,
                    "통합": list(opinion.통합),
                    "충돌해소": opinion.충돌해소,
                    "search_terms": list(opinion.search_terms),
                },
            )
        )
    return "\n".join(parts) + "\n"


def parse_intent(text: str) -> Intent:
    """Parse 00-intent.md back (ValueError on anything malformed)."""

    where = "00-intent.md"
    meta = parse_frontmatter(text)
    required = _parse_required(meta, where)
    opinions: list[Opinion] = []
    for block in _of_type(_parse_blocks(text), "OPINION"):
        fields = block.fields
        spot = block.where
        opinions.append(
            Opinion(
                opinion_id=_as_int(_need(fields, "opinion_id", spot)),
                op=_as_op(_need(fields, "op", spot), spot),
                대상문서=_as_strs(_need(fields, "대상문서", spot), spot, "대상문서"),
                연관문서=_as_strs(_need(fields, "연관문서", spot), spot, "연관문서"),
                범위=_as_str(_need(fields, "범위", spot)),
                대상주장=_as_str(_need(fields, "대상주장", spot)),
                수정방향=_as_str(_need(fields, "수정방향", spot)),
                근거=_as_str(_need(fields, "근거", spot)),
                통합=_as_strs(_need(fields, "통합", spot), spot, "통합"),
                충돌해소=_as_str(_need(fields, "충돌해소", spot)),
                search_terms=_as_strs(
                    _need(fields, "search_terms", spot), spot, "search_terms"
                ),
            )
        )
    _check(meta, "opinions", len(opinions), where)
    return Intent(
        mr_iid=_as_int(_need(meta, "mr_iid", where)),
        project_id=_as_str(_need(meta, "project_id", where)),
        round=_as_int(_need(meta, "round", where)),
        base_sha=_as_str(_need(meta, "base_sha", where)),
        opinions=tuple(opinions),
        required=required,
    )


# --------------------------------------------------------------------------
# 10-anchor.md — tool
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorHit:
    term: str
    file: str
    section_id: str
    line: int


@dataclass(frozen=True)
class AnchorCandidate:
    """A near miss offered back to the human — the clarify question's body."""

    file: str
    section_id: str
    heading: str


@dataclass(frozen=True)
class AnchorWindow:
    """The anchor unit ±20 lines — everything 3b is allowed to read."""

    file: str
    start: int
    end: int


@dataclass(frozen=True)
class AnchorEntry:
    opinion_id: int
    status: str  # FOUND | MISSING
    hits: tuple[AnchorHit, ...]
    unit_id: str = ""
    window: AnchorWindow | None = None
    candidates: tuple[AnchorCandidate, ...] = ()


@dataclass(frozen=True)
class Anchor:
    mr_iid: int
    round: int
    entries: tuple[AnchorEntry, ...]
    required: Required

    @property
    def resolved(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "FOUND")

    @property
    def missing(self) -> int:
        return len(self.entries) - self.resolved

    @property
    def files(self) -> tuple[str, ...]:
        """Every file an anchor touches — half of the gate's whitelist."""

        names: list[str] = []
        for entry in self.entries:
            names.extend(hit.file for hit in entry.hits)
            if entry.window is not None:
                names.append(entry.window.file)
        return tuple(sorted(set(names)))


def render_anchor(anchor: Anchor) -> str:
    """Render 10-anchor.md — FOUND carries unit_id/window, MISSING carries candidates."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": anchor.mr_iid,
                "round": anchor.round,
                "opinions": len(anchor.entries),
                "resolved": anchor.resolved,
                "missing": anchor.missing,
                **_render_required(anchor.required),
            }
        )
    ]
    for entry in anchor.entries:
        fields: dict[str, object] = {
            "opinion_id": entry.opinion_id,
            "status": entry.status,
            "hits": [
                {
                    "term": hit.term,
                    "file": hit.file,
                    "section_id": hit.section_id,
                    "line": hit.line,
                }
                for hit in entry.hits
            ],
        }
        if entry.unit_id:
            fields["unit_id"] = entry.unit_id
        if entry.window is not None:
            fields["window"] = {
                "file": entry.window.file,
                "lines": f"{entry.window.start}-{entry.window.end}",
            }
        if entry.candidates:
            fields["candidates"] = [
                {
                    "file": candidate.file,
                    "section_id": candidate.section_id,
                    "heading": candidate.heading,
                }
                for candidate in entry.candidates
            ]
        parts.append("")
        parts.append(_render_block("ANCHOR", str(entry.opinion_id), fields))
    return "\n".join(parts) + "\n"


def parse_anchor(text: str) -> Anchor:
    """Parse 10-anchor.md back."""

    where = "10-anchor.md"
    meta = parse_frontmatter(text)
    required = _parse_required(meta, where)
    entries: list[AnchorEntry] = []
    for block in _of_type(_parse_blocks(text), "ANCHOR"):
        fields = block.fields
        spot = block.where
        window_map = _as_map(fields.get("window"), spot, "window")
        window = None
        if window_map is not None:
            start, end = _as_range(window_map.get("lines"), spot, "window.lines")
            window = AnchorWindow(file=_as_str(window_map.get("file")), start=start, end=end)
        entries.append(
            AnchorEntry(
                opinion_id=_as_int(_need(fields, "opinion_id", spot)),
                status=_as_str(_need(fields, "status", spot)),
                hits=tuple(
                    AnchorHit(
                        term=_as_str(row.get("term")),
                        file=_as_str(row.get("file")),
                        section_id=_as_str(row.get("section_id")),
                        line=_as_int(row.get("line")),
                    )
                    for row in _as_maps(_need(fields, "hits", spot), spot, "hits")
                ),
                unit_id=_as_str(fields.get("unit_id")),
                window=window,
                candidates=tuple(
                    AnchorCandidate(
                        file=_as_str(row.get("file")),
                        section_id=_as_str(row.get("section_id")),
                        heading=_as_str(row.get("heading")),
                    )
                    for row in _as_maps(fields.get("candidates"), spot, "candidates")
                ),
            )
        )
    anchor = Anchor(
        mr_iid=_as_int(_need(meta, "mr_iid", where)),
        round=_as_int(_need(meta, "round", where)),
        entries=tuple(entries),
        required=required,
    )
    _check(meta, "opinions", len(anchor.entries), where)
    _check(meta, "resolved", anchor.resolved, where)
    _check(meta, "missing", anchor.missing, where)
    return anchor


# --------------------------------------------------------------------------
# 20-impact.md — tool
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactLiteral:
    kind: str
    key: str
    value: str


@dataclass(frozen=True)
class LinkIn:
    file: str
    line: int
    target: str


@dataclass(frozen=True)
class ImpactCandidate:
    """A companion-change candidate. `via`/`match` are its evidence — a
    candidate without evidence is never emitted."""

    file: str
    section_id: str
    unit_id: str
    line: int
    via: str  # literal | link
    match: str


@dataclass(frozen=True)
class AnchorRef:
    file: str
    section_id: str
    unit_id: str


@dataclass(frozen=True)
class ImpactEntry:
    opinion_id: int
    anchor: AnchorRef | None
    literals: tuple[ImpactLiteral, ...]
    links_in: tuple[LinkIn, ...]
    candidates: tuple[ImpactCandidate, ...]
    #: 3a가 연관 문서로 지목했으나 리터럴·링크 증거를 찾지 못한 경로들 — 후보가
    #: 되지 못한 의심으로, 버리지 않고 보고로 넘긴다.
    related_unverified: tuple[str, ...] = ()


@dataclass(frozen=True)
class Impact:
    mr_iid: int
    round: int
    entries: tuple[ImpactEntry, ...]
    required: Required

    @property
    def candidate_count(self) -> int:
        return sum(len(entry.candidates) for entry in self.entries)

    @property
    def files(self) -> tuple[str, ...]:
        """Every candidate file — the other half of the gate's whitelist."""

        names = [
            candidate.file for entry in self.entries for candidate in entry.candidates
        ]
        return tuple(sorted(set(names)))


def render_impact(impact: Impact) -> str:
    """Render 20-impact.md — one IMPACT block per anchored opinion."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": impact.mr_iid,
                "round": impact.round,
                "anchors": len(impact.entries),
                "candidates": impact.candidate_count,
                **_render_required(impact.required),
            }
        )
    ]
    for entry in impact.entries:
        anchor = entry.anchor
        parts.append("")
        parts.append(
            _render_block(
                "IMPACT",
                str(entry.opinion_id),
                {
                    "opinion_id": entry.opinion_id,
                    "anchor": (
                        None
                        if anchor is None
                        else {
                            "file": anchor.file,
                            "section_id": anchor.section_id,
                            "unit_id": anchor.unit_id,
                        }
                    ),
                    "literals": [
                        {"kind": lit.kind, "key": lit.key, "value": lit.value}
                        for lit in entry.literals
                    ],
                    "links_in": [
                        {"file": link.file, "line": link.line, "target": link.target}
                        for link in entry.links_in
                    ],
                    "candidates": [
                        {
                            "file": candidate.file,
                            "section_id": candidate.section_id,
                            "unit_id": candidate.unit_id,
                            "line": candidate.line,
                            "via": candidate.via,
                            "match": candidate.match,
                        }
                        for candidate in entry.candidates
                    ],
                    "related_unverified": list(entry.related_unverified),
                },
            )
        )
    return "\n".join(parts) + "\n"


def parse_impact(text: str) -> Impact:
    """Parse 20-impact.md back."""

    where = "20-impact.md"
    meta = parse_frontmatter(text)
    required = _parse_required(meta, where)
    entries: list[ImpactEntry] = []
    for block in _of_type(_parse_blocks(text), "IMPACT"):
        fields = block.fields
        spot = block.where
        anchor_map = _as_map(_need(fields, "anchor", spot), spot, "anchor")
        entries.append(
            ImpactEntry(
                opinion_id=_as_int(_need(fields, "opinion_id", spot)),
                anchor=(
                    None
                    if anchor_map is None
                    else AnchorRef(
                        file=_as_str(anchor_map.get("file")),
                        section_id=_as_str(anchor_map.get("section_id")),
                        unit_id=_as_str(anchor_map.get("unit_id")),
                    )
                ),
                literals=tuple(
                    ImpactLiteral(
                        kind=_as_str(row.get("kind")),
                        key=_as_str(row.get("key")),
                        value=_as_str(row.get("value")),
                    )
                    for row in _as_maps(_need(fields, "literals", spot), spot, "literals")
                ),
                links_in=tuple(
                    LinkIn(
                        file=_as_str(row.get("file")),
                        line=_as_int(row.get("line")),
                        target=_as_str(row.get("target")),
                    )
                    for row in _as_maps(_need(fields, "links_in", spot), spot, "links_in")
                ),
                candidates=tuple(
                    ImpactCandidate(
                        file=_as_str(row.get("file")),
                        section_id=_as_str(row.get("section_id")),
                        unit_id=_as_str(row.get("unit_id")),
                        line=_as_int(row.get("line")),
                        via=_as_str(row.get("via")),
                        match=_as_str(row.get("match")),
                    )
                    for row in _as_maps(_need(fields, "candidates", spot), spot, "candidates")
                ),
                related_unverified=_as_strs(
                    fields.get("related_unverified"), spot, "related_unverified"
                ),
            )
        )
    impact = Impact(
        mr_iid=_as_int(_need(meta, "mr_iid", where)),
        round=_as_int(_need(meta, "round", where)),
        entries=tuple(entries),
        required=required,
    )
    _check(meta, "anchors", len(impact.entries), where)
    _check(meta, "candidates", impact.candidate_count, where)
    return impact


# --------------------------------------------------------------------------
# 30-edit.md — 3b receipt (a claim, never evidence)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EditOp:
    file: str
    line: int
    from_value: str  # artifact key 'from' — a Python keyword
    to_value: str  # artifact key 'to'


@dataclass(frozen=True)
class EditSelection:
    """One 20-impact candidate's verdict — the judgment left to 3b."""

    file: str
    line: int
    via: str
    decision: str  # apply | skip
    reason: str = ""


@dataclass(frozen=True)
class EditEntry:
    opinion_id: int
    result: str  # applied | unapplied
    edits: tuple[EditOp, ...]
    selected: tuple[EditSelection, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Unprocessed:
    opinion_id: int
    reason: str


@dataclass(frozen=True)
class EditReceipt:
    mr_iid: int
    round: int
    processed: tuple[int, ...]
    unprocessed: tuple[Unprocessed, ...]
    files_touched: tuple[str, ...]
    entries: tuple[EditEntry, ...]
    required: Required


def render_edit(receipt: EditReceipt) -> str:
    """Render 30-edit.md. Silence is treated as omission, so `reason` rides along."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": receipt.mr_iid,
                "round": receipt.round,
                "processed": list(receipt.processed),
                "unprocessed": [
                    {"opinion_id": item.opinion_id, "reason": item.reason}
                    for item in receipt.unprocessed
                ],
                "files_touched": list(receipt.files_touched),
                **_render_required(receipt.required),
            }
        )
    ]
    for entry in receipt.entries:
        fields: dict[str, object] = {
            "opinion_id": entry.opinion_id,
            "result": entry.result,
        }
        if entry.reason:
            fields["reason"] = entry.reason
        fields["edits"] = [
            {
                "file": edit.file,
                "line": edit.line,
                "from": edit.from_value,
                "to": edit.to_value,
            }
            for edit in entry.edits
        ]
        if entry.selected:
            fields["selected"] = [
                {
                    "file": selection.file,
                    "line": selection.line,
                    "via": selection.via,
                    "decision": selection.decision,
                    **({"reason": selection.reason} if selection.reason else {}),
                }
                for selection in entry.selected
            ]
        parts.append("")
        parts.append(_render_block("EDIT", str(entry.opinion_id), fields))
    return "\n".join(parts) + "\n"


def parse_edit(text: str) -> EditReceipt:
    """Parse 30-edit.md back."""

    where = "30-edit.md"
    meta = parse_frontmatter(text)
    required = _parse_required(meta, where)
    entries: list[EditEntry] = []
    for block in _of_type(_parse_blocks(text), "EDIT"):
        fields = block.fields
        spot = block.where
        entries.append(
            EditEntry(
                opinion_id=_as_int(_need(fields, "opinion_id", spot)),
                result=_as_str(_need(fields, "result", spot)),
                edits=tuple(
                    EditOp(
                        file=_as_str(row.get("file")),
                        line=_as_int(row.get("line")),
                        from_value=_as_str(row.get("from")),
                        to_value=_as_str(row.get("to")),
                    )
                    for row in _as_maps(_need(fields, "edits", spot), spot, "edits")
                ),
                selected=tuple(
                    EditSelection(
                        file=_as_str(row.get("file")),
                        line=_as_int(row.get("line")),
                        via=_as_str(row.get("via")),
                        decision=_as_str(row.get("decision")),
                        reason=_as_str(row.get("reason")),
                    )
                    for row in _as_maps(fields.get("selected"), spot, "selected")
                ),
                reason=_as_str(fields.get("reason")),
            )
        )
    return EditReceipt(
        mr_iid=_as_int(_need(meta, "mr_iid", where)),
        round=_as_int(_need(meta, "round", where)),
        processed=_as_ints(_need(meta, "processed", where), where, "processed"),
        unprocessed=tuple(
            Unprocessed(
                opinion_id=_as_int(row.get("opinion_id")),
                reason=_as_str(row.get("reason")),
            )
            for row in _as_maps(_need(meta, "unprocessed", where), where, "unprocessed")
        ),
        files_touched=_as_strs(_need(meta, "files_touched", where), where, "files_touched"),
        entries=tuple(entries),
        required=required,
    )


# --------------------------------------------------------------------------
# 35-gate.md — tool, the design's core
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GatePaths:
    changed: tuple[str, ...]
    allowed: tuple[str, ...]
    non_md: tuple[str, ...]
    outside: tuple[str, ...]
    reverted: tuple[str, ...]
    result: str  # pass | fail


@dataclass(frozen=True)
class LiteralSets:
    """The three-term difference, assembled straight from `extract_literals`.

    `UnitLiterals.changed` is not used: it zips a key's leftover values by
    sorted position, so from→to can pair the wrong two values.
    """

    a_in_base: bool
    a_not_in_head: bool
    b_in_head: bool


@dataclass(frozen=True)
class LiteralText:
    """`section_text` + `in` — the only check that sees plain-text swaps."""

    a_in_head_text: bool
    b_in_head_text: bool


@dataclass(frozen=True)
class LiteralCheck:
    unit_id: str
    opinion_id: int
    from_value: str
    to_value: str
    sets: LiteralSets | None  # null when A is no literal at all — plain text
    text: LiteralText
    occurrences: str  # always OCCURRENCES_UNAVAILABLE — dedup lost the counts
    result: str  # pass | fail


@dataclass(frozen=True)
class GateSize:
    files: int
    max_files: int
    lines: int
    max_lines: int
    result: str


@dataclass(frozen=True)
class GateResidue:
    untracked: tuple[str, ...]
    result: str


@dataclass(frozen=True)
class Gate:
    """What the caller acts on.

    `reverted` is the complete revert set — the out-of-whitelist files, or
    every changed path when `revert_all` is set by a size overflow, so a
    caller can revert `gate.reverted` and be done. `GatePaths.reverted` stays
    the path gate's own finding. `kind` is "failed" only for a whole-round
    failure (size overflow); a per-file revert leaves it "ok".
    `failed_opinion_ids` are the LITERAL failures — the design's 「구현 누락」,
    which is what gets re-injected into 3b.
    """

    mr_iid: int
    round: int
    kind: str  # ok | failed
    reverted: tuple[str, ...]
    revert_all: bool
    failed_opinion_ids: tuple[int, ...]
    paths: GatePaths
    literals: tuple[LiteralCheck, ...]
    size: GateSize
    residue: GateResidue
    required: Required


def render_gate(gate: Gate) -> str:
    """Render 35-gate.md — PATHS, one LITERAL block per check, SIZE, RESIDUE."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": gate.mr_iid,
                "round": gate.round,
                "changed_files": gate.size.files,
                "changed_lines": gate.size.lines,
                "limits": {
                    "max_changed_files": gate.size.max_files,
                    "max_changed_lines": gate.size.max_lines,
                },
                "reverted": list(gate.reverted),
                "revert_all": gate.revert_all,
                "failed_opinion_ids": list(gate.failed_opinion_ids),
                "kind": gate.kind,
                **_render_required(gate.required),
            }
        ),
        "",
        _render_block(
            "PATHS",
            "",
            {
                "changed": list(gate.paths.changed),
                "allowed": list(gate.paths.allowed),
                "non_md": list(gate.paths.non_md),
                "outside": list(gate.paths.outside),
                "reverted": list(gate.paths.reverted),
                "result": gate.paths.result,
            },
        ),
    ]
    for check in gate.literals:
        parts.append("")
        parts.append(
            _render_block(
                "LITERAL",
                check.unit_id,
                {
                    "opinion_id": check.opinion_id,
                    "from": check.from_value,
                    "to": check.to_value,
                    "sets": (
                        None
                        if check.sets is None
                        else {
                            "a_in_base": check.sets.a_in_base,
                            "a_not_in_head": check.sets.a_not_in_head,
                            "b_in_head": check.sets.b_in_head,
                        }
                    ),
                    "text": {
                        "a_in_head_text": check.text.a_in_head_text,
                        "b_in_head_text": check.text.b_in_head_text,
                    },
                    "occurrences": check.occurrences,
                    "result": check.result,
                },
            )
        )
    parts.extend(
        [
            "",
            _render_block(
                "SIZE",
                "",
                {
                    "files": f"{gate.size.files} / {gate.size.max_files}",
                    "lines": f"{gate.size.lines} / {gate.size.max_lines}",
                    "result": gate.size.result,
                },
            ),
            "",
            _render_block(
                "RESIDUE",
                "",
                {
                    "untracked": list(gate.residue.untracked),
                    "result": gate.residue.result,
                },
            ),
        ]
    )
    return "\n".join(parts) + "\n"


def parse_gate(text: str) -> Gate:
    """Parse 35-gate.md back."""

    where = "35-gate.md"
    meta = parse_frontmatter(text)
    required = _parse_required(meta, where)
    blocks = _parse_blocks(text)

    paths_fields = _one(blocks, "PATHS", where).fields
    paths = GatePaths(
        changed=_as_strs(_need(paths_fields, "changed", where), where, "changed"),
        allowed=_as_strs(_need(paths_fields, "allowed", where), where, "allowed"),
        non_md=_as_strs(_need(paths_fields, "non_md", where), where, "non_md"),
        outside=_as_strs(_need(paths_fields, "outside", where), where, "outside"),
        reverted=_as_strs(_need(paths_fields, "reverted", where), where, "reverted"),
        result=_as_str(_need(paths_fields, "result", where)),
    )

    literals: list[LiteralCheck] = []
    for block in _of_type(blocks, "LITERAL"):
        fields = block.fields
        spot = block.where
        sets_map = _as_map(_need(fields, "sets", spot), spot, "sets")
        text_map = _as_map(_need(fields, "text", spot), spot, "text")
        if text_map is None:
            raise ValueError(
                f"{spot}: text must be a flow map — the plain-text check is not optional"
            )
        literals.append(
            LiteralCheck(
                unit_id=block.id,
                opinion_id=_as_int(_need(fields, "opinion_id", spot)),
                from_value=_as_str(_need(fields, "from", spot)),
                to_value=_as_str(_need(fields, "to", spot)),
                sets=(
                    None
                    if sets_map is None
                    else LiteralSets(
                        a_in_base=_as_bool(sets_map.get("a_in_base")),
                        a_not_in_head=_as_bool(sets_map.get("a_not_in_head")),
                        b_in_head=_as_bool(sets_map.get("b_in_head")),
                    )
                ),
                text=LiteralText(
                    a_in_head_text=_as_bool(text_map.get("a_in_head_text")),
                    b_in_head_text=_as_bool(text_map.get("b_in_head_text")),
                ),
                occurrences=_as_str(_need(fields, "occurrences", spot)),
                result=_as_str(_need(fields, "result", spot)),
            )
        )

    size_fields = _one(blocks, "SIZE", where).fields
    files, max_files = _as_pair(_need(size_fields, "files", where), where, "files")
    lines, max_lines = _as_pair(_need(size_fields, "lines", where), where, "lines")
    size = GateSize(
        files=files,
        max_files=max_files,
        lines=lines,
        max_lines=max_lines,
        result=_as_str(_need(size_fields, "result", where)),
    )

    residue_fields = _one(blocks, "RESIDUE", where).fields
    residue = GateResidue(
        untracked=_as_strs(_need(residue_fields, "untracked", where), where, "untracked"),
        result=_as_str(_need(residue_fields, "result", where)),
    )

    _check(meta, "changed_files", size.files, where)
    _check(meta, "changed_lines", size.lines, where)
    return Gate(
        mr_iid=_as_int(_need(meta, "mr_iid", where)),
        round=_as_int(_need(meta, "round", where)),
        kind=_as_str(_need(meta, "kind", where)),
        reverted=_as_strs(_need(meta, "reverted", where), where, "reverted"),
        revert_all=_as_bool(_need(meta, "revert_all", where)),
        failed_opinion_ids=_as_ints(
            _need(meta, "failed_opinion_ids", where), where, "failed_opinion_ids"
        ),
        paths=paths,
        literals=tuple(literals),
        size=size,
        residue=residue,
        required=required,
    )


# --------------------------------------------------------------------------
# 40-verify.md — 3c
# --------------------------------------------------------------------------

_VERDICT_MARK = "**판정문**"
_VERDICT_KINDS: tuple[str, ...] = ("applied", "partial", "unapplied")


@dataclass(frozen=True)
class Evidence:
    file: str
    line: int
    before: str
    after: str


@dataclass(frozen=True)
class Verdict:
    opinion_id: int
    verdict: str  # applied | partial | unapplied
    evidence: tuple[Evidence, ...]
    판정문: str = ""


@dataclass(frozen=True)
class Verify:
    mr_iid: int
    round: int
    verdicts: tuple[Verdict, ...]
    summary: str
    required: Required

    def counts(self) -> dict[str, object]:
        return {
            kind: sum(1 for row in self.verdicts if row.verdict == kind)
            for kind in _VERDICT_KINDS
        }

    def by_verdict(self, kind: str) -> tuple[Verdict, ...]:
        return tuple(row for row in self.verdicts if row.verdict == kind)


def render_verify(verify: Verify, *, counts: Mapping[str, object] | None = None) -> str:
    """Render 40-verify.md.

    `counts` overrides the derived verdict tally and exists for
    `verify_template` alone — a template's placeholder verdict cannot be
    tallied, and a wrong-looking `applied: 0` in a prompt teaches the model
    the wrong thing.
    """

    parts = [
        render_frontmatter(
            {
                "mr_iid": verify.mr_iid,
                "round": verify.round,
                "opinions": len(verify.verdicts),
                "verdicts": dict(counts) if counts is not None else verify.counts(),
                **_render_required(verify.required),
            }
        )
    ]
    for row in verify.verdicts:
        parts.append("")
        parts.append(
            _render_block(
                "VERDICT",
                str(row.opinion_id),
                {
                    "opinion_id": row.opinion_id,
                    "verdict": row.verdict,
                    "evidence": [
                        {
                            "file": item.file,
                            "line": item.line,
                            "before": item.before,
                            "after": item.after,
                        }
                        for item in row.evidence
                    ],
                },
                f"{_VERDICT_MARK} {row.판정문}" if row.판정문 else "",
            )
        )
    parts.append("")
    parts.append(_render_block("SUMMARY", "", {}, verify.summary))
    return "\n".join(parts) + "\n"


def parse_verify(text: str) -> Verify:
    """Parse 40-verify.md back."""

    where = "40-verify.md"
    meta = parse_frontmatter(text)
    required = _parse_required(meta, where)
    blocks = _parse_blocks(text)
    verdicts: list[Verdict] = []
    for block in _of_type(blocks, "VERDICT"):
        fields = block.fields
        spot = block.where
        narrative = block.narrative
        if narrative.startswith(_VERDICT_MARK):
            narrative = narrative[len(_VERDICT_MARK) :].strip()
        verdicts.append(
            Verdict(
                opinion_id=_as_int(_need(fields, "opinion_id", spot)),
                verdict=_as_str(_need(fields, "verdict", spot)),
                evidence=tuple(
                    Evidence(
                        file=_as_str(row.get("file")),
                        line=_as_int(row.get("line")),
                        before=_as_str(row.get("before")),
                        after=_as_str(row.get("after")),
                    )
                    for row in _as_maps(_need(fields, "evidence", spot), spot, "evidence")
                ),
                판정문=narrative,
            )
        )
    summary_blocks = _of_type(blocks, "SUMMARY")
    verify = Verify(
        mr_iid=_as_int(_need(meta, "mr_iid", where)),
        round=_as_int(_need(meta, "round", where)),
        verdicts=tuple(verdicts),
        summary=summary_blocks[0].narrative if summary_blocks else "",
        required=required,
    )
    _check(meta, "opinions", len(verify.verdicts), where)
    return verify


# --------------------------------------------------------------------------
# 50-summary.md — tool, the report's three slots
# --------------------------------------------------------------------------

_NONE_SLOT = "(none)"


@dataclass(frozen=True)
class Summary:
    """Duck-types `render_review_report`'s review argument (app/report_html.py:72):
    `summary` / `key_changes` / `points_to_watch` are the only attributes it reads.
    """

    mr_iid: int
    round: int
    applied: tuple[int, ...]
    partial: tuple[int, ...]
    unapplied: tuple[int, ...]
    commit_paths: tuple[str, ...]
    summary: str
    key_changes: tuple[str, ...]
    points_to_watch: tuple[str, ...]
    required: Required


def _render_slot(name: str, items: Sequence[str]) -> str:
    body = "\n".join(f"- {item}" for item in items) if items else _NONE_SLOT
    return _render_block(name, "", {}, body)


def _parse_slot(block: _Block | None, where: str) -> tuple[str, ...]:
    if block is None:
        return ()
    items: list[str] = []
    for line in block.narrative.splitlines():
        stripped = line.strip()
        if not stripped or stripped == _NONE_SLOT:
            continue
        if not stripped.startswith("- "):
            raise ValueError(f"{where}: list slot line must be a '- ' bullet: {line!r}")
        items.append(stripped[2:].strip())
    return tuple(items)


def render_summary(summary: Summary) -> str:
    """Render 50-summary.md — the three slots as markdown, not yaml."""

    parts = [
        render_frontmatter(
            {
                "mr_iid": summary.mr_iid,
                "round": summary.round,
                "applied": list(summary.applied),
                "partial": list(summary.partial),
                "unapplied": list(summary.unapplied),
                "commit_paths": list(summary.commit_paths),
                **_render_required(summary.required),
            }
        ),
        "",
        _render_block("summary", "", {}, summary.summary),
        "",
        _render_slot("key_changes", summary.key_changes),
        "",
        _render_slot("points_to_watch", summary.points_to_watch),
    ]
    return "\n".join(parts) + "\n"


def parse_summary(text: str) -> Summary:
    """Parse 50-summary.md back."""

    where = "50-summary.md"
    meta = parse_frontmatter(text)
    required = _parse_required(meta, where)
    blocks = _parse_blocks(text)
    slots = {block.type: block for block in blocks}
    summary_block = slots.get("summary")
    return Summary(
        mr_iid=_as_int(_need(meta, "mr_iid", where)),
        round=_as_int(_need(meta, "round", where)),
        applied=_as_ints(_need(meta, "applied", where), where, "applied"),
        partial=_as_ints(_need(meta, "partial", where), where, "partial"),
        unapplied=_as_ints(_need(meta, "unapplied", where), where, "unapplied"),
        commit_paths=_as_strs(_need(meta, "commit_paths", where), where, "commit_paths"),
        summary=summary_block.narrative if summary_block is not None else "",
        key_changes=_parse_slot(slots.get("key_changes"), where),
        points_to_watch=_parse_slot(slots.get("points_to_watch"), where),
        required=required,
    )


# --------------------------------------------------------------------------
# templates — the prompts' copy, produced by the renderers above
# --------------------------------------------------------------------------


def _ph(name: str) -> Any:
    """A `<placeholder>` slot. Typed `Any` so int fields can hold one.

    Identity fields (`mr_iid`, `round`, `project_id`, `base_sha`) deliberately
    do *not* get one: a stage shown `mr_iid: "<mr_iid>"` has to supply the
    value, and measurement says it gets that wrong in two separate ways. 3b
    once wrote the *project* id into `mr_iid` and the mr iid into `round`;
    3c later copied the placeholder through verbatim, which the strict int
    coercion rejected — losing a verify stage after its edit had already
    landed. Python knows all four, so the template arrives already filled and
    the stage has nothing to invent. `runner._stamp_identity` overwrites them
    again after parsing, so a stage that edits them anyway cannot matter.
    """

    return f"<{name}>"


def _required_template() -> Required:
    return Required(
        STATUS=_ph("OK | EMPTY | PARTIAL — 잘린 범위 | BLOCKED — 이유 | FAILED — 이유"),
        UNCOVERED=_ph("처리하지 못한 id 목록 | none"),
        UNCERTAIN=_ph("애매한 점 한 줄 | none"),
        CONFIDENCE=_ph("high|medium|low — 직접 확인한 사실만 근거로"),
    )


def intent_template(*, mr_iid: int, project_id: str, round: int, base_sha: str) -> str:
    """The empty 00-intent.md the 3a prompt carries, identity already filled."""

    return render_intent(
        Intent(
            mr_iid=mr_iid,
            project_id=project_id,
            round=round,
            base_sha=base_sha,
            opinions=(
                Opinion(
                    opinion_id=_ph("opinion_id"),
                    op=_ph("create | modify | delete"),
                    대상문서=(_ph("목차에 있는 경로"),),
                    연관문서=(),
                    범위=_ph("local | cross-doc"),
                    대상주장=_ph("의견이 지목한 현재 서술"),
                    수정방향=_ph("A → B"),
                    근거=_ph("의견 원문 인용"),
                    통합=(),
                    충돌해소=_ph("none"),
                    search_terms=(_ph("원문에 그대로 있을 문자열"),),
                ),
            ),
            required=_required_template(),
        )
    )


def edit_template(*, mr_iid: int, round: int) -> str:
    """The empty 30-edit.md the 3b prompt carries, identity already filled."""

    return render_edit(
        EditReceipt(
            mr_iid=mr_iid,
            round=round,
            processed=(_ph("opinion_id"),),
            unprocessed=(),
            files_touched=(_ph("만진 파일"),),
            entries=(
                EditEntry(
                    opinion_id=_ph("opinion_id"),
                    result=_ph("applied | unapplied"),
                    edits=(
                        EditOp(
                            file=_ph("파일"),
                            line=_ph("라인"),
                            from_value=_ph("바꾸기 전"),
                            to_value=_ph("바꾼 후"),
                        ),
                    ),
                    selected=(
                        EditSelection(
                            file=_ph("후보 파일"),
                            line=_ph("라인"),
                            via=_ph("literal | link"),
                            decision=_ph("apply | skip"),
                            reason=_ph("skip 이면 이유 필수"),
                        ),
                    ),
                    reason=_ph("unapplied 이면 사유 필수 — 침묵은 누락"),
                ),
            ),
            required=_required_template(),
        )
    )


def verify_template(*, mr_iid: int, round: int) -> str:
    """The empty 40-verify.md the 3c prompt carries, identity already filled."""

    return render_verify(
        Verify(
            mr_iid=mr_iid,
            round=round,
            verdicts=(
                Verdict(
                    opinion_id=_ph("opinion_id"),
                    verdict=_ph("applied | partial | unapplied"),
                    evidence=(
                        Evidence(
                            file=_ph("파일"),
                            line=_ph("라인"),
                            before=_ph("before 원문"),
                            after=_ph("after 원문"),
                        ),
                    ),
                    판정문=_ph("의견 원문을 축으로 한 판정 서술 두어 줄"),
                ),
            ),
            summary=_ph("50-summary 의 summary 슬롯이 될 한두 줄"),
            required=_required_template(),
        ),
        counts={kind: _ph("n") for kind in _VERDICT_KINDS},
    )


def render_key_change(label: str, key: str, from_value: str, to_value: str) -> str:
    """One `key_changes` bullet — '<label> — <key>: <from> → <to>'."""

    return f"{label} — {key}: {from_value} → {to_value}"


def join_ids(ids: Iterable[int]) -> str:
    """`UNCOVERED` bodies: '41, 42' or the literal 'none' when empty."""

    text = ", ".join(str(value) for value in ids)
    return text or "none"
