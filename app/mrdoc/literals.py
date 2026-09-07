"""06-literals node — spec-bearing tokens per change unit, for cross-checking.

Six deterministic extractors (design spec): '키: 값', number+unit
('초/분/시간/일/ms/s/m/h/KB/MB/GB/%/회/개...'), versions ('3.12.4', '3.8+'),
booleans, code spans and link targets. Normalization is trim + whitespace
collapse with markdown symbols kept, so '`30초`' and '30초' normalize
identically. Diffing groups by key: same key with a different value becomes
changed[{key, from, to}] and those values are then excluded from
removed/added — the design's rule that keeps '60분 → 30분' one fact instead
of a lost value plus a new one.

Everything is extracted line by line, which is what the measured run got
wrong three ways: a code span crossed newlines and swallowed a paragraph, a
whole fenced block became one literal keyed on its info string, and version
strings matched no extractor at all ('3.12.4' carries no unit suffix). Fenced
blocks are scanned per line for the value extractors — never as one blob —
and their delimiter lines are skipped so 'powershell' is not a value.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .changeset import Changeset
from .frontmatter import (
    parse_frontmatter,
    parse_sections,
    render_frontmatter,
    render_section,
)
from .structure import Structure

_TICK = chr(96)
_KV = re.compile(r"^\s*[-*]?\s*([A-Za-z0-9_가-힣][A-Za-z0-9_가-힣 .\-/]*):\s+(.+?)\s*$")
_NUM_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(초|분|시간|일|ms|s|m|h|KB|MB|GB|TB|%|회|개|건|명|배)(?!\w)"
)
#: Versions carry no unit suffix, so they fall outside _NUM_UNIT entirely —
#: the single most common value shape in these docs went unextracted.
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?\+?")
#: What sits glued to the left of a version ('Python.Python.', 'python@').
_VERSION_PREFIX = re.compile(r"[A-Za-z0-9_.@-]+$")
_BOOL = re.compile(r"\b(true|false)\b", re.IGNORECASE)
#: Bounded to one line: '[^`]+' spanned newlines and ate whole paragraphs.
_CODE = re.compile(_TICK + r"([^" + _TICK + r"\n]{1,60})" + _TICK)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_FENCE = re.compile(r"^ {0,3}(" + _TICK + r"{3,}|~{3,})")


@dataclass(frozen=True)
class Literal:
    kind: str  # kv | unit | version | bool | code | link
    key: str
    value: str


@dataclass(frozen=True)
class ChangedValue:
    key: str
    from_value: str
    to_value: str


@dataclass(frozen=True)
class UnitLiterals:
    """One unit's literal diff — the 06 artifact's per-unit block."""

    unit_id: str
    section_id: str
    removed: tuple[str, ...]
    added: tuple[str, ...]
    changed: tuple[ChangedValue, ...]
    #: Rewritten runs whose literal sets are identical — 표현 atoms.
    textual: tuple[tuple[str, str], ...] = ()
    #: Literal-free sentence runs added/removed — 의미 prose atoms.
    prose_added: tuple[str, ...] = ()
    prose_removed: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.removed) + len(self.added) + len(self.changed)


@dataclass(frozen=True)
class Literals:
    units: tuple[UnitLiterals, ...]


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _content_lines(text: str) -> list[tuple[str, bool]]:
    """(line, inside_a_fence) — fence delimiter lines dropped entirely.

    The delimiter's info string ('```powershell') is not a value; the lines
    it wraps are, so they come back flagged instead of skipped.
    """

    result: list[tuple[str, bool]] = []
    fence_char: str | None = None
    fence_len = 0
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence_char is None:
            if match:
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                continue
            result.append((line, False))
            continue
        stripped = line.strip()
        if (
            match
            and match.group(1)[0] == fence_char
            and len(match.group(1)) >= fence_len
            and set(stripped) == {fence_char}
        ):
            fence_char = None
            continue
        result.append((line, True))
    return result


def _version_key(line: str, start: int) -> str:
    """The token glued to a version's left, or 'version' when it stands alone.

    'Python.Python.3.11' keys on 'Python.Python' (the trailing dot is the
    version's own separator), 'python@3.11' on 'python@'. A version preceded
    by whitespace or punctuation has no owner and keys on 'version', which is
    what groups '3.12.4' with '3.12.7' across a rewritten sentence.
    """

    match = _VERSION_PREFIX.search(line[:start])
    if not match:
        return "version"
    token = match.group(0).removesuffix(".")
    return token if any(char.isalpha() for char in token) else "version"


def extract_literals(text: str) -> list[Literal]:
    """Run all six extractors line by line; document order, exact dedup.

    Document order is load-bearing: _diff pairs a group's leftover base and
    head values positionally, so '3.12.4 → 3.12.7' only stays one fact while
    the values keep the order they appear in.
    """

    found: list[Literal] = []
    for line, in_fence in _content_lines(text):
        if (match := _KV.match(line)) and "://" not in match.group(1):
            found.append(
                Literal("kv", _collapse(match.group(1)), _collapse(match.group(2)))
            )
        claimed: list[tuple[int, int]] = []
        for hit in _NUM_UNIT.finditer(line):
            claimed.append(hit.span())
            found.append(Literal("unit", hit.group(2), hit.group(1)))
        for hit in _VERSION.finditer(line):
            if any(a < hit.end() and hit.start() < b for a, b in claimed):
                continue  # number+unit owns it — never extract twice
            found.append(
                Literal("version", _version_key(line, hit.start()), hit.group(0))
            )
        for value in _BOOL.findall(line):
            found.append(Literal("bool", "bool", value.lower()))
        if in_fence:
            continue  # backticks and brackets inside a block are not markup
        for span in _CODE.findall(line):
            token = re.split(r"[=:\s]", span.strip(), maxsplit=1)[0]
            found.append(Literal("code", token or "code", _collapse(span)))
        for label, target in _LINK.findall(line):
            found.append(Literal("link", _collapse(label), _collapse(target)))

    unique: dict[tuple[str, str, str], Literal] = {}
    for lit in found:
        unique.setdefault((lit.kind, lit.key, lit.value), lit)
    return list(unique.values())


def _diff(
    base: list[Literal], head: list[Literal]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[ChangedValue, ...]]:
    """Group by (kind, key): moved values -> changed, leftovers -> removed/added.

    Leftovers pair in document order, not sorted order: '3.12.4' and '3.8+'
    must meet '3.12.7' and '3.10+' the way they sit in the sentence. Sorting
    first pairs '3.12.4' with '3.10+' — deterministic, and wrong.
    """

    def grouped(literals: list[Literal]) -> dict[tuple[str, str], list[str]]:
        result: dict[tuple[str, str], list[str]] = {}
        for lit in literals:
            result.setdefault((lit.kind, lit.key), []).append(lit.value)
        return result

    base_groups = grouped(base)
    head_groups = grouped(head)
    removed: list[str] = []
    added: list[str] = []
    changed: list[ChangedValue] = []
    for group in sorted(set(base_groups) | set(head_groups)):
        b_values = base_groups.get(group, [])
        h_values = head_groups.get(group, [])
        shared = set(b_values) & set(h_values)
        b_rest = [v for v in b_values if v not in shared]
        h_rest = [v for v in h_values if v not in shared]
        for old, new in zip(b_rest, h_rest, strict=False):
            changed.append(ChangedValue(group[1], old, new))
        removed.extend(b_rest[len(h_rest) :])
        added.extend(h_rest[len(b_rest) :])
    return (
        tuple(sorted(removed)),
        tuple(sorted(added)),
        tuple(sorted(changed, key=lambda c: (c.key, c.from_value, c.to_value))),
    )


def _section_text(tree: dict[str, str], path: str | None, lines: tuple[int, int] | None) -> str:
    if path is None or lines is None or path not in tree:
        return ""
    file_lines = tree[path].splitlines()
    return "\n".join(file_lines[lines[0] - 1 : lines[1]])


def _prose_lines(text: str) -> list[str]:
    """Content lines for the prose diff — headings and blanks dropped.

    A heading is 05's vocabulary, not a sentence: leaving it in would make
    every heading rename a textual atom too. Inside a fence '#' is a comment
    and stays — _content_lines already carries that flag.
    """

    result: list[str] = []
    for line, in_fence in _content_lines(text):
        if not in_fence and line.lstrip().startswith("#"):
            continue
        if not line.strip():
            continue
        result.append(line.rstrip())
    return result


def _literal_set(lines: list[str]) -> set[tuple[str, str, str]]:
    return {
        (lit.kind, lit.key, lit.value)
        for line in lines
        for lit in extract_literals(line)
    }


def _prose_runs(lines: list[str]) -> list[str]:
    """Maximal runs of literal-free lines — one prose atom per run.

    A line carrying a value belongs to the value atoms; it splits the run
    instead of joining it, so '설치한 뒤 / 3.12.7 필요 / 완료됩니다' adds one
    prose atom on each side of the version, not one blurred blob.
    """

    runs: list[str] = []
    run: list[str] = []
    for line in lines:
        if extract_literals(line):
            if run:
                runs.append(_collapse(" ".join(run)))
                run = []
            continue
        run.append(line)
    if run:
        runs.append(_collapse(" ".join(run)))
    return runs


def _prose_diff(
    base_text: str, head_text: str
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[str, ...]]:
    """(textual, prose_added, prose_removed) from a line diff of one section.

    A replace run whose two literal sets are identical kept every value and
    rewrote the words around them — one 표현 atom, before/after collapsed.
    Replace runs with different literal sets are the value atoms' territory
    and are not double-counted here. Insert/delete runs contribute one 의미
    prose atom per literal-free run; lines that carry values stay with the
    value atoms for the same reason.
    """

    before = _prose_lines(base_text)
    after = _prose_lines(head_text)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    textual: list[tuple[str, str]] = []
    prose_added: list[str] = []
    prose_removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            b_run = before[i1:i2]
            h_run = after[j1:j2]
            if _literal_set(b_run) != _literal_set(h_run):
                continue  # values moved — the value atoms own this run
            b_text = _collapse(" ".join(b_run))
            h_text = _collapse(" ".join(h_run))
            if b_text != h_text:
                textual.append((b_text, h_text))
        elif tag == "insert":
            prose_added.extend(_prose_runs(after[j1:j2]))
        else:
            prose_removed.extend(_prose_runs(before[i1:i2]))
    return (
        tuple(textual),
        tuple(dict.fromkeys(prose_added)),
        tuple(dict.fromkeys(prose_removed)),
    )


def build_literals(
    structure: Structure, changeset: Changeset, base_tree: dict[str, str], head_tree: dict[str, str]
) -> Literals:
    """Diff literals per change unit using the same trees 05 was built from."""

    head_paths = {row.section_id: row.file for row in structure.tree}
    old_paths = {
        entry.fid: (entry.old_path or entry.path) for entry in changeset.files
    }
    units: list[UnitLiterals] = []
    for unit in structure.changed:
        base_path = old_paths.get(unit.file_id) if unit.old_lines else None
        head_path = head_paths.get(unit.section_id) if unit.new_lines else None
        base_text = _section_text(base_tree, base_path, unit.old_lines)
        head_text = _section_text(head_tree, head_path, unit.new_lines)
        base = extract_literals(base_text)
        head = extract_literals(head_text)
        removed, added, changed = _diff(base, head)
        textual: tuple[tuple[str, str], ...] = ()
        prose_added: tuple[str, ...] = ()
        prose_removed: tuple[str, ...] = ()
        if unit.kind == "modified":
            textual, prose_added, prose_removed = _prose_diff(base_text, head_text)
        units.append(
            UnitLiterals(
                unit_id=unit.unit_id,
                section_id=unit.section_id,
                removed=removed,
                added=added,
                changed=changed,
                textual=textual,
                prose_added=prose_added,
                prose_removed=prose_removed,
            )
        )
    return Literals(units=tuple(units))


def average_per_unit(literals: Literals) -> float:
    """Phase-0 density metric — footer gate uses >=3 keep / <1 redesign."""

    if not literals.units:
        return 0.0
    return sum(unit.total for unit in literals.units) / len(literals.units)


def render_literals(literals: Literals) -> str:
    """Render 06-literals.md — frontmatter + one yaml block per unit."""

    parts = [
        render_frontmatter(
            {
                "units": len(literals.units),
                "totals": {
                    "removed": sum(len(u.removed) for u in literals.units),
                    "added": sum(len(u.added) for u in literals.units),
                    "changed": sum(len(u.changed) for u in literals.units),
                },
                "atoms": {
                    "textual": sum(len(u.textual) for u in literals.units),
                    "prose_added": sum(len(u.prose_added) for u in literals.units),
                    "prose_removed": sum(len(u.prose_removed) for u in literals.units),
                },
            }
        ),
    ]
    for unit in literals.units:
        changed_rows = [
            {"key": c.key, "from": c.from_value, "to": c.to_value} for c in unit.changed
        ]
        parts.append("")
        parts.append(
            render_section(
                "UNIT",
                unit.unit_id,
                {
                    "section_id": unit.section_id,
                    "removed": list(unit.removed),
                    "added": list(unit.added),
                    "changed": changed_rows,
                    "textual": [
                        {"before": before, "after": after}
                        for before, after in unit.textual
                    ],
                    "prose_added": list(unit.prose_added),
                    "prose_removed": list(unit.prose_removed),
                },
            )
        )
    return "\n".join(parts) + "\n"


def parse_literals(text: str) -> Literals:
    """Parse 06-literals.md back — round-trip contract for resume/tests."""

    def value_str(value: object) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        return str(value)

    sections = parse_sections(text)
    units: list[UnitLiterals] = []
    for unit_id, fields in sections.items():
        if not unit_id.startswith("u-"):
            continue
        changed_raw = fields.get("changed") or []
        changed = tuple(
            ChangedValue(
                value_str(row.get("key", "")),
                value_str(row.get("from", "")),
                value_str(row.get("to", "")),
            )
            for row in changed_raw
            if isinstance(row, dict)
        )
        units.append(
            UnitLiterals(
                unit_id=unit_id,
                section_id=str(fields.get("section_id", "")),
                removed=tuple(value_str(v) for v in fields.get("removed") or []),
                added=tuple(value_str(v) for v in fields.get("added") or []),
                changed=changed,
                textual=tuple(
                    (
                        value_str(row.get("before", "")),
                        value_str(row.get("after", "")),
                    )
                    for row in fields.get("textual") or []
                    if isinstance(row, dict)
                ),
                prose_added=tuple(
                    value_str(v) for v in fields.get("prose_added") or []
                ),
                prose_removed=tuple(
                    value_str(v) for v in fields.get("prose_removed") or []
                ),
            )
        )
    meta = parse_frontmatter(text)
    if meta.get("units") != len(units):
        raise ValueError("frontmatter unit count does not match blocks")
    return Literals(units=tuple(units))
