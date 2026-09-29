"""The five deterministic nodes — anchor / spec / impact / gate / summary-extract.

docs/revise-workflow.html moves the original steps 2, 3 and 6 off the LLM
entirely: existence checking, companion-change discovery and comparison all
reduce to string work this repo already has as pure functions
(markdown_tree / literals / ids). The LLM is left with the two things that
are not comparisons — which strings to look for (3a) and which of the found
candidates are real (3b). The spec node sits between them: 3a writes its
spec without seeing a document body, so before 3b is paid to act on it the
spec is checked against the opinion text and the anchor section, and the
one thing 3a can only guess — the current value A — is computed instead.

Nothing here spawns a process. `build_gate` in particular never calls git:
`changed_paths`, `changed_lines` and `untracked` arrive as arguments, so the
gate is testable without a repository and cannot be fooled by the working
tree changing under it mid-check.

Two `extract_literals` traps the design pins, both handled in `build_gate`:

- it dedups on (kind, key, value), so occurrence counts are gone. "3 of 3
  places replaced?" is unanswerable; the check says only "that value is no
  longer in the head unit" and marks the limit in the `occurrences` field.
- literals cover five shapes (kv / unit / version / bool / code / link) and a
  plain-prose term swap is none of them. So every check runs a
  `section_text` + `in` pass as well, reported separately as `text`; when the
  value was never a literal, `sets` is null and `text` alone decides.
"""

from __future__ import annotations

import difflib
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from app.mrdoc.ids import section_id as make_section_id
from app.mrdoc.ids import unit_id as make_unit_id
from app.mrdoc.literals import Literal, extract_literals
from app.mrdoc.markdown_tree import Section, parse_sections, section_text

from .schema import (
    OCCURRENCES_UNAVAILABLE,
    Anchor,
    AnchorCandidate,
    AnchorEntry,
    AnchorHit,
    AnchorRef,
    AnchorWindow,
    Gate,
    GatePaths,
    GateResidue,
    GateSize,
    Impact,
    ImpactCandidate,
    ImpactEntry,
    ImpactLiteral,
    Intent,
    LinkIn,
    LiteralCheck,
    LiteralSets,
    LiteralText,
    Opinion,
    Required,
    Spec,
    SpecEntry,
    Summary,
    Verdict,
    Verify,
    join_ids,
    render_key_change,
)

#: 3b reads the anchor unit ±20 lines and nothing else (design: 파일 통독 금지).
WINDOW_LINES = 20

#: How many near misses a MISSING anchor offers back to the human.
CANDIDATE_LIMIT = 3

MD_SUFFIXES: tuple[str, ...] = (".md", ".mdx")

_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_ARROW = re.compile(r"\s*(?:→|->|⇒|=>)\s*")
_QUOTES = " \t\"'`「」『』《》"
_SPACES = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^\w\s-]", re.UNICODE)


def _posix(path: str) -> str:
    """Every path this package emits is posix — Windows backslashes broke
    downstream JSON parsing in the measured run (design constraint 8)."""

    return str(path).replace("\\", "/")


def _norm(text: str) -> str:
    return _SPACES.sub(" ", text).strip().casefold()


# --------------------------------------------------------------------------
# section index — the coordinate system all four nodes share
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Unit:
    """One section of one file, with its stable ids and its own text."""

    file: str
    section: Section
    section_id: str
    unit_id: str
    text: str


def _index(tree: Mapping[str, str]) -> list[_Unit]:
    """Every section of every file, in (path, position) order."""

    units: list[_Unit] = []
    for path in sorted(tree):
        posix = _posix(path)
        body = tree[path]
        for section in parse_sections(body):
            sid = make_section_id(posix, section.ordinal, section.heading_path)
            units.append(
                _Unit(
                    file=posix,
                    section=section,
                    section_id=sid,
                    unit_id=make_unit_id(sid),
                    text=section_text(body, section),
                )
            )
    return units


def _line_of(unit: _Unit, needle: str) -> int:
    """1-based file line of `needle` inside the unit, else the heading line."""

    for offset, line in enumerate(unit.text.splitlines()):
        if needle and needle in line:
            return unit.section.start + offset
    return unit.section.start


# --------------------------------------------------------------------------
# 2 · anchor — does the thing the human pointed at exist?
# --------------------------------------------------------------------------


def _match_term(term: str, units: Sequence[_Unit]) -> list[_Unit]:
    """Units matching one search term, best tier only.

    Priority is exact > whitespace/case-normalized > token partial. A lower
    tier is consulted only when the one above it found nothing, so a literal
    hit is never diluted by a fuzzy one.
    """

    if not term.strip():
        return []
    exact = [unit for unit in units if term in unit.text]
    if exact:
        return exact
    needle = _norm(term)
    close = [unit for unit in units if needle in _norm(unit.text)]
    if close:
        return close
    tokens = [token for token in needle.split(" ") if len(token) >= 2]
    scored: list[tuple[int, _Unit]] = []
    for unit in units:
        body = _norm(unit.text)
        hits = sum(1 for token in tokens if token in body)
        if hits:
            scored.append((hits, unit))
    if not scored:
        return []
    best = max(hits for hits, _ in scored)
    return [unit for hits, unit in scored if hits == best]


def _near_candidates(terms: Sequence[str], units: Sequence[_Unit]) -> tuple[AnchorCandidate, ...]:
    """Top heading_path look-alikes — these become the clarify question's body."""

    scored: list[tuple[float, _Unit]] = []
    for unit in units:
        heading = _norm(unit.section.heading_path)
        ratio = max(
            (
                difflib.SequenceMatcher(None, _norm(term), heading).ratio()
                for term in terms
                if term.strip()
            ),
            default=0.0,
        )
        scored.append((ratio, unit))
    scored.sort(key=lambda row: (-row[0], row[1].file, row[1].section.start))
    return tuple(
        AnchorCandidate(
            file=unit.file, section_id=unit.section_id, heading=unit.section.heading
        )
        for _, unit in scored[:CANDIDATE_LIMIT]
    )


def build_anchor(intent: Intent, head_tree: dict[str, str]) -> Anchor:
    """Resolve every opinion's `search_terms` against the head section index.

    An opinion that names files keeps its search inside them, so a target
    that is not in the table of contents becomes MISSING instead of being
    quietly matched somewhere else in the repo.
    """

    units = _index(head_tree)
    by_path = {_posix(path): body for path, body in head_tree.items()}
    entries: list[AnchorEntry] = []
    for opinion in intent.opinions:
        targets = {_posix(path) for path in opinion.대상문서}
        scope = [unit for unit in units if unit.file in targets] if targets else units
        hits: list[AnchorHit] = []
        per_unit: dict[str, int] = {}
        for term in opinion.search_terms:
            matched = _match_term(term, scope)
            if not matched:
                continue
            unit = matched[0]
            hits.append(
                AnchorHit(
                    term=term,
                    file=unit.file,
                    section_id=unit.section_id,
                    line=_line_of(unit, term),
                )
            )
            per_unit[unit.unit_id] = per_unit.get(unit.unit_id, 0) + 1
        anchor_unit: _Unit | None = None
        best = 0
        for unit in scope:
            count = per_unit.get(unit.unit_id, 0)
            if count > best:
                best, anchor_unit = count, unit
        if anchor_unit is None:
            entries.append(
                AnchorEntry(
                    opinion_id=opinion.opinion_id,
                    status="MISSING",
                    hits=(),
                    candidates=_near_candidates(opinion.search_terms, scope or units),
                )
            )
            continue
        total = len(by_path.get(anchor_unit.file, "").splitlines())
        start = max(1, anchor_unit.section.start - WINDOW_LINES)
        end = max(start, min(total, anchor_unit.section.end + WINDOW_LINES))
        entries.append(
            AnchorEntry(
                opinion_id=opinion.opinion_id,
                status="FOUND",
                hits=tuple(hits),
                unit_id=anchor_unit.unit_id,
                window=AnchorWindow(file=anchor_unit.file, start=start, end=end),
            )
        )
    missing = [entry.opinion_id for entry in entries if entry.status == "MISSING"]
    if not entries:
        status = "EMPTY"
    elif missing:
        status = f"PARTIAL — 대상 미확인 {join_ids(missing)}"
    else:
        status = "OK"
    return Anchor(
        mr_iid=intent.mr_iid,
        round=intent.round,
        entries=tuple(entries),
        required=Required(
            STATUS=status,
            UNCOVERED=join_ids(missing),
            UNCERTAIN=(
                "none"
                if not missing
                else f"대상 미확인 {len(missing)}건 — 후보 {CANDIDATE_LIMIT}개를 되물음에 싣는다"
            ),
            CONFIDENCE="high — parse_sections 섹션 인덱스의 문자열 검색만 사용",
        ),
    )


# --------------------------------------------------------------------------
# 2b · spec — does 3a's spec agree with the opinion and the section?
# --------------------------------------------------------------------------

#: The note a non-substitution opinion carries — nothing the machine could
#: compare, so it is not a thing to watch either.
NOTE_NOT_A_SWAP = "치환 형태가 아니다 — 기계 대조 없이 편집으로 넘긴다"

_LEFT_DIGIT = r"(?<![0-9.])"
_RIGHT_DIGIT = r"(?!\.?[0-9])"


def _count(surface: str, text: str) -> int:
    """Occurrences of `surface` in `text`; a digit edge must not sit inside a
    longer number ('3.2' is not in '13.2' or '3.2.1')."""

    if not surface:
        return 0
    left = _LEFT_DIGIT if surface[0].isdigit() else ""
    right = _RIGHT_DIGIT if surface[-1].isdigit() else ""
    return len(re.findall(left + re.escape(surface) + right, text))


def _same_literal(a: Literal, b: Literal) -> bool:
    """One value regardless of the line around it — the key of a version or
    kv depends on its neighbours, a unit's key is the unit itself."""

    return a.kind == b.kind and a.value == b.value and (a.kind != "unit" or a.key == b.key)


def _compatible(literal: Literal, targets: Sequence[Literal]) -> bool:
    """Could `literal` be the value one of `targets` replaces?"""

    return any(
        literal.kind == target.kind
        and (literal.kind not in ("unit", "kv") or literal.key == target.key)
        for target in targets
    )


def _surface(literal: Literal, text: str) -> str:
    """How `literal` is spelled in `text` — a unit's value alone is not."""

    if literal.kind == "unit":
        found = re.search(re.escape(literal.value) + r"\s*" + re.escape(literal.key), text)
        return found.group(0) if found else f"{literal.value}{literal.key}"
    if literal.kind == "bool":
        found = re.search(r"\b" + re.escape(literal.value) + r"\b", text, re.IGNORECASE)
        return found.group(0) if found else literal.value
    return literal.value


def _locate(value: str, text: str) -> str | None:
    """`value` as `text` spells it, or None when `text` does not carry it."""

    if not value.strip():
        return None
    if _count(value, text):
        return value
    own = extract_literals(value)
    if len(own) != 1:
        return None
    for literal in extract_literals(text):
        if _same_literal(literal, own[0]):
            return _surface(literal, text)
    return None


def _mentions(value: str, text: str) -> bool:
    needle = _norm(value)
    return _locate(value, text) is not None or bool(needle and needle in _norm(text))


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


#: A unit glued to its particle ('30초를', '10초는') is invisible to
#: extract_literals — its `(?!\w)` edge counts Hangul as a word character —
#: so the opinion, which is human prose, has the particle split off first.
_GLUED_UNIT = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:초|분|시간|일|ms|s|m|h|KB|MB|GB|TB|%|회|개|건|명|배))"
    r"(?=[을를이가은는에로와과도만까부의])"
)

#: Written right after a value the human wants left alone — '4.0 은 유지',
#: '10초는 그대로', '3.2 버전은 건드리지 말고'. Such a value is never A.
_KEEP = re.compile(
    r"\s*(?:[가-힣]{1,3}\s*)??(?:은|는|을|를|이|가|도|만)?\s*"
    r"(?:(?:계속|현재|지금|그냥|기존)\s*)?"
    r"(?:유지|그대로|두고|둬|두세요|둔다|놔두|놓아두|바꾸지|변경하지|건드리지|고정)"
)

#: A sentence end — '주세요.' closes one, the dot inside '4.0' does not.
_SENTENCE_END = re.compile(r"(?<![0-9])[.!?。]+(?=\s|$)|\n")


def _opinion_literals(text: str) -> list[Literal]:
    return extract_literals(_GLUED_UNIT.sub(r"\1 ", text))


def _spans(literal: Literal, text: str) -> list[tuple[int, int]]:
    """Where `literal` sits in `text` — a unit may be glued to its particle."""

    if literal.kind == "unit":
        pattern = _LEFT_DIGIT + re.escape(literal.value) + r"\s*" + re.escape(literal.key)
        if literal.key[-1:].isascii() and literal.key[-1:].isalpha():
            pattern += r"(?![A-Za-z])"
    else:
        value = literal.value
        left = _LEFT_DIGIT if value[:1].isdigit() else ""
        right = _RIGHT_DIGIT if value[-1:].isdigit() else ""
        pattern = left + re.escape(value) + right
    flags = re.IGNORECASE if literal.kind == "bool" else 0
    return [found.span() for found in re.finditer(pattern, text, flags)]


def _sentences(text: str) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    start = 0
    for end in _SENTENCE_END.finditer(text):
        bounds.append((start, end.start()))
        start = end.end()
    bounds.append((start, len(text)))
    return bounds


def _current_values(
    named: Sequence[Literal], b_literals: Sequence[Literal], text: str
) -> tuple[list[Literal], list[Literal]]:
    """(candidates, held) — which of the values the opinion names may be A.

    A value followed by a keep word is held back. Of the rest, the ones
    written before B in B's own sentence ('3.2 에서 3.3 으로') are the swap's
    from side; once there is such a side, a value mentioned anywhere else
    ('4.0 은 이미 지원') is context, not A. Without one ('3.2 로 되어 있네요.
    4.0 으로 바꿔 주세요'), every value not kept stays a candidate.
    """

    kept = [
        literal
        for literal in named
        if any(_KEEP.match(text, end) for _, end in _spans(literal, text))
    ]
    free = [literal for literal in named if literal not in kept]
    b_starts = [start for target in b_literals for start, _ in _spans(target, text)]
    before: list[Literal] = []
    for first, last in _sentences(text):
        cut = min((start for start in b_starts if first <= start < last), default=None)
        if cut is None:
            continue
        before.extend(
            literal
            for literal in free
            if literal not in before
            and any(first <= start and end <= cut for start, end in _spans(literal, text))
        )
    candidates = before or free
    return candidates, [literal for literal in named if literal not in candidates]


def _resolve_a(
    spec_a: str, b: str, opinion_text: str, section: str
) -> tuple[str, str, tuple[str, ...]]:
    """(A, source, candidates) — A as the section spells it, '' when undecided.

    The human outranks 3a, and 3a outranks inference: a current value the
    opinion itself names wins (`_current_values` — never one the human said
    to keep), then 3a's A if the section holds it and the human did not hold
    it back (source 'held' when that is the only reason A is undecided),
    then the one value in the section that B could replace (same kind — and
    same unit or key). More than one such value is ambiguous and comes back
    as candidates; none leaves A undecided. Inference never overrides the
    human: once the opinion names a current value, a section that does not
    hold it is not given a look-alike ('3.2' is not '13.2'). Nor does it
    guess when the section already reads B — the lone other value may be
    one the edit must not touch, so it comes back as a candidate to ask.
    """

    b_literals = extract_literals(b)
    replaceable: list[Literal] = []
    named: list[Literal] = []
    held: list[Literal] = []
    stated: tuple[str, ...] = ()
    if b_literals:
        replaceable = [
            literal
            for literal in extract_literals(section)
            if _compatible(literal, b_literals)
            and not any(_same_literal(literal, target) for target in b_literals)
        ]
        named = [
            literal
            for literal in _opinion_literals(opinion_text)
            if _compatible(literal, b_literals)
            and not any(_same_literal(literal, target) for target in b_literals)
        ]
        candidates, held = _current_values(named, b_literals, opinion_text)
        stated = _unique(
            [
                _surface(literal, section)
                for literal in replaceable
                if any(_same_literal(literal, own) for own in candidates)
            ]
        )
        if len(stated) == 1:
            return stated[0], "opinion", ()
    located = _locate(spec_a, section)
    vetoed = located is not None and any(
        _same_literal(own, kept) for own in extract_literals(located) for kept in held
    )
    if located and not vetoed and (not stated or located in stated):
        return located, "spec", ()
    if len(stated) > 1:
        return "", "", stated
    if named:
        return "", "held" if vetoed else "", ()
    surfaces = _unique([_surface(literal, section) for literal in replaceable])
    if len(surfaces) == 1 and _locate(b, section) is None:
        return surfaces[0], "code", ()
    return "", "", surfaces


def _tier(terms: Sequence[str], unit: _Unit) -> str:
    """The best tier any search term reached inside the anchor unit."""

    best = "partial"
    for term in terms:
        if not term.strip():
            continue
        if term in unit.text:
            return "exact"
        if _norm(term) in _norm(unit.text):
            best = "normalized"
    return best


def _rivals(terms: Sequence[str], scope: Sequence[_Unit], chosen: _Unit) -> tuple[str, ...]:
    """Sections the search terms fit at least as well as the chosen one.

    build_anchor counts only the first unit each term matched and breaks a
    tie by file order, so a second section just as plausible disappears
    without a trace. Counting every match surfaces it.
    """

    counts: dict[str, int] = {}
    for term in terms:
        for unit in _match_term(term, scope):
            counts[unit.unit_id] = counts.get(unit.unit_id, 0) + 1
    mine = counts.get(chosen.unit_id, 0)
    return tuple(
        f"{unit.file} § {unit.section.heading}"
        for unit in scope
        if unit.unit_id != chosen.unit_id and mine and counts.get(unit.unit_id, 0) >= mine
    )


def _judge(
    opinion: Opinion,
    entry: AnchorEntry | None,
    opinion_text: str,
    units: Sequence[_Unit],
    by_unit: Mapping[str, _Unit],
    *,
    duplicated: bool,
) -> SpecEntry:
    """One opinion's decision — every check reads the opinion or the section."""

    base = {"opinion_id": opinion.opinion_id, "op": opinion.op}
    if duplicated:
        return SpecEntry(
            **base, decision="spec_error", notes=("같은 opinion_id 의 OPINION 블록이 둘 이상이다",)
        )
    unit = by_unit.get(entry.unit_id) if entry is not None and entry.status == "FOUND" else None
    if unit is None:
        return SpecEntry(**base, decision="missing", notes=("대상 미확인 — 앵커 0건",))

    targets = {_posix(path) for path in opinion.대상문서}
    scope = [candidate for candidate in units if candidate.file in targets] if targets else units
    tier = _tier(opinion.search_terms, unit)
    rivals = _rivals(opinion.search_terms, scope, unit)
    common = {**base, "anchor_tier": tier, "alternatives": rivals}

    swap = parse_replacement(opinion.수정방향)
    if swap is None:
        return SpecEntry(**common, decision="ok", notes=(NOTE_NOT_A_SWAP,))
    spec_a, b = swap
    pair = {"from_value": spec_a, "to_value": b}
    if opinion.op == "create":
        return SpecEntry(
            **common, **pair, decision="spec_error",
            notes=("op 가 create 인데 수정방향이 치환(A → B)이다",),
        )
    if spec_a == b:
        return SpecEntry(
            **common, **pair, decision="spec_error", notes=("현재 값과 목표 값이 같다",)
        )
    if not _mentions(b, opinion_text):
        return SpecEntry(
            **common, **pair, decision="spec_error",
            notes=(f"목표 값 {b!r} 이 의견 원문에 없다 — 3a 가 만든 값",),
        )

    section = unit.text
    a, source, candidates = _resolve_a(spec_a, b, opinion_text, section)
    if candidates:
        note = (
            f"대상 절에 바꿀 수 있는 현재 값이 여럿이다: {', '.join(candidates)}"
            if len(candidates) > 1
            else f"목표 값이 이미 절에 있어 {candidates[0]!r} 을 현재 값으로 단정하지 않는다"
        )
        return SpecEntry(
            **common, **pair, decision="clarify", a_candidates=candidates, notes=(note,)
        )
    if source == "held":
        return SpecEntry(
            **common, **pair, decision="spec_error",
            notes=(f"스펙의 현재 값 {spec_a!r} 은 의견이 바꿀 값으로 쓰지 않은 값이다",),
        )
    if not a:
        if _locate(b, section) is None:
            return SpecEntry(
                **common, **pair, decision="spec_error",
                notes=(f"현재 값 {spec_a!r} 이 대상 절에 없다",),
            )
        if tier == "partial" or rivals:
            return SpecEntry(
                **common, **pair, decision="spec_error",
                notes=("목표 값은 절에 있지만 앵커가 약해 이미 반영으로 단정하지 않는다",),
            )
        return SpecEntry(
            **common, **pair, decision="already_applied",
            notes=("대상 절에 목표 값이 이미 있고 현재 값은 없다",),
        )

    notes: list[str] = []
    if a != spec_a:
        origin = "의견 원문" if source == "opinion" else "절의 같은 종류 값"
        notes.append(f"스펙의 현재 값 {spec_a!r} 을 {origin} {a!r} 로 교정했다")
    occurrences = max(_count(a, section), 1)
    if occurrences > 1:
        notes.append(f"현재 값이 절 안에 {occurrences}곳 — 전부 바꾼다")
    if _locate(b, section) is not None:
        notes.append("목표 값이 이미 절에 있다 — 남은 현재 값만 바꾼다")
    return SpecEntry(
        **common,
        decision="ok",
        from_value=a,
        to_value=b,
        a_source=source,
        occurrences=occurrences,
        notes=tuple(notes),
    )


def build_spec(
    intent: Intent,
    anchor: Anchor,
    opinions: Sequence[Mapping[str, object]],
    head_tree: dict[str, str],
) -> Spec:
    """Check 3a's spec against the opinion text and the anchor section.

    3a writes the spec from the table of contents alone, so every claim it
    makes about the document body is a guess. This node checks the guesses
    that can be checked and fixes the one that can be computed — the
    current value A — before 3b is paid to act on them:

    - coverage: one spec block per input opinion (통합 counts as covered);
      a missing, duplicated or invented id is a spec error;
    - B, the target, has to come from the human's text;
    - A is resolved inside the anchor section (`_resolve_a`), never taken on
      faith; a section that already reads B and no A is already applied;
    - how well the anchor matched is measured and handed on as a note — a
      weak or tied anchor still goes to 3b, flagged (a human decision).

    Opinions are the executor's rows (`id`, `body`); the output has exactly
    one entry per input opinion, in input order, plus any invented id.
    """

    units = _index(head_tree)
    by_unit = {unit.unit_id: unit for unit in units}
    bodies = {int(str(row.get("id"))): str(row.get("body") or "") for row in opinions}

    blocks: dict[int, Opinion] = {}
    duplicated: set[int] = set()
    for opinion in intent.opinions:
        if opinion.opinion_id in blocks:
            duplicated.add(opinion.opinion_id)
        else:
            blocks[opinion.opinion_id] = opinion
    hosts: dict[int, int] = {}
    for opinion in blocks.values():
        for raw in opinion.통합:
            for digits in re.findall(r"\d+", raw):
                merged = int(digits)
                if merged != opinion.opinion_id and merged not in blocks:
                    hosts.setdefault(merged, opinion.opinion_id)
    anchors: dict[int, AnchorEntry] = {}
    for entry in anchor.entries:
        anchors.setdefault(entry.opinion_id, entry)

    judged: dict[int, SpecEntry] = {}
    for opinion_id, opinion in blocks.items():
        group = [opinion_id, *(merged for merged, host in hosts.items() if host == opinion_id)]
        judged[opinion_id] = _judge(
            opinion,
            anchors.get(opinion_id),
            "\n".join(bodies.get(member, "") for member in group),
            units,
            by_unit,
            duplicated=opinion_id in duplicated,
        )

    entries: list[SpecEntry] = []
    for opinion_id in bodies:
        if opinion_id in judged:
            entries.append(judged[opinion_id])
        elif opinion_id in hosts:
            host = judged[hosts[opinion_id]]
            entries.append(
                SpecEntry(
                    opinion_id=opinion_id,
                    decision=host.decision,
                    op=host.op,
                    host=host.opinion_id,
                    notes=(f"의견 {host.opinion_id} 에 통합",),
                )
            )
        else:
            entries.append(
                SpecEntry(
                    opinion_id=opinion_id,
                    decision="spec_error",
                    notes=("3a 스펙에 이 의견의 블록이 없다",),
                )
            )
    for opinion_id, entry in judged.items():
        if opinion_id not in bodies:
            entries.append(
                replace(
                    entry,
                    decision="spec_error",
                    notes=("입력에 없는 opinion_id — 3a 가 만든 블록", *entry.notes),
                )
            )

    spec = Spec(
        mr_iid=intent.mr_iid,
        round=intent.round,
        entries=tuple(entries),
        required=Required(STATUS="OK", UNCOVERED="none", UNCERTAIN="none", CONFIDENCE="-"),
    )
    blocked = [
        entry.opinion_id
        for entry in entries
        if entry.decision in ("spec_error", "clarify", "missing")
    ]
    weak = [
        entry.opinion_id
        for entry in entries
        if entry.decision == "ok" and (entry.anchor_tier == "partial" or entry.alternatives)
    ]
    counts = spec.counts()
    if not entries:
        status = "EMPTY"
    elif blocked:
        status = "PARTIAL — " + " · ".join(
            f"{decision} {counts[decision]}"
            for decision in ("spec_error", "clarify", "missing")
            if counts[decision]
        )
    else:
        status = "OK"
    return replace(
        spec,
        required=Required(
            STATUS=status,
            UNCOVERED=join_ids(blocked),
            UNCERTAIN=(
                "none"
                if not weak
                else f"약하거나 동점인 앵커 {len(weak)}건 — 편집은 진행하고 주의 항목에 싣는다"
            ),
            CONFIDENCE="high — 의견 원문과 앵커 절의 문자열·리터럴 대조만 사용",
        ),
    )


def effective_view(intent: Intent, anchor: Anchor, spec: Spec) -> tuple[Intent, Anchor]:
    """The intent and anchor 3b, the gate and 3c act on: `ok` opinions only,
    each substitution rewritten to the code-resolved A → B."""

    ok = {entry.opinion_id: entry for entry in spec.entries if entry.decision == "ok"}
    opinions = []
    for opinion in intent.opinions:
        entry = ok.get(opinion.opinion_id)
        if entry is None or entry.host:
            continue
        if entry.from_value and entry.to_value:
            opinion = replace(opinion, 수정방향=f"{entry.from_value} → {entry.to_value}")
        opinions.append(opinion)
    kept = {opinion.opinion_id for opinion in opinions}
    seen: set[int] = set()
    entries = []
    for entry in anchor.entries:
        if entry.opinion_id in kept and entry.opinion_id not in seen:
            seen.add(entry.opinion_id)
            entries.append(entry)
    return replace(intent, opinions=tuple(opinions)), replace(anchor, entries=tuple(entries))


def spec_verdict(entry: SpecEntry, spec: Spec) -> Verdict:
    """The verdict an opinion the spec gate kept away from 3c receives."""

    host = spec.entry(entry.host) if entry.host else None
    source = host or entry
    prefix = f"의견 {entry.host} 에 통합 — " if entry.host else ""
    reason = source.notes[0] if source.notes else "사유 없음"
    if source.decision == "already_applied":
        return Verdict(
            opinion_id=entry.opinion_id,
            verdict="applied",
            evidence=(),
            판정문=(
                f"{prefix}이미 반영 — 대상 절에 목표 값 {source.to_value!r} 이 있고 "
                "현재 값이 없어 편집하지 않았다"
            ),
        )
    if source.decision == "clarify" and len(source.a_candidates) == 1:
        text = (
            f"{prefix}목표 값이 이미 절에 있어 되묻는다 — 후보 {source.a_candidates[0]}"
        )
    elif source.decision == "clarify":
        text = f"{prefix}현재 값 후보가 여럿이라 되묻는다 — {', '.join(source.a_candidates)}"
    elif source.decision == "missing":
        text = f"{prefix}대상 미확인 — 앵커 0건"
    elif source.decision == "spec_error":
        text = f"{prefix}스펙 검증 실패 — {reason}"
    else:
        text = f"{prefix}변경분 없음 — 대조할 diff 가 없다"
    return Verdict(opinion_id=entry.opinion_id, verdict="unapplied", evidence=(), 판정문=text)


def spec_watch(spec: Spec) -> list[str]:
    """points_to_watch lines the spec gate owes the reviewer.

    Rejected opinions already surface through their verdict's 판정문; what is
    left is what went through with a caveat — a weak or tied anchor, an A the
    code corrected, a value replaced in several places — and the opinions
    closed as already applied without an edit.
    """

    lines: list[str] = []
    for entry in spec.entries:
        if entry.host:
            continue
        if entry.decision == "already_applied":
            lines.append(f"의견 {entry.opinion_id} 이미 반영 — 편집 없이 반영 완료로 처리했다")
            continue
        if entry.decision != "ok":
            continue
        if entry.anchor_tier == "partial":
            lines.append(
                f"약한 앵커 — 의견 {entry.opinion_id}: 검색어가 토큰 일부로만 잡힌 절을 편집했다"
            )
        if entry.alternatives:
            lines.append(
                f"모호한 앵커 — 의견 {entry.opinion_id}: 같은 수준으로 잡힌 절 "
                + ", ".join(entry.alternatives)
            )
        lines.extend(
            f"의견 {entry.opinion_id} — {note}" for note in entry.notes if note != NOTE_NOT_A_SWAP
        )
    return lines


# --------------------------------------------------------------------------
# 3 · impact — what else has to change with it?
# --------------------------------------------------------------------------


def _slug(heading: str) -> str:
    """GitHub-style heading anchor: '2. 설치 절차' -> '2-설치-절차'."""

    text = _NON_SLUG.sub("", heading.strip().casefold())
    return _SPACES.sub("-", text.strip())


def _resolve_link(source: str, target_path: str) -> str:
    """Resolve a link's path part against the linking file's directory."""

    if not target_path:
        return source
    if target_path.startswith("/"):
        return posixpath.normpath(target_path.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target_path))


def _via_rank(via: str) -> int:
    return 0 if via == "literal" else 1


def build_impact(intent: Intent, anchor: Anchor, head_tree: dict[str, str]) -> Impact:
    """Reverse-search the anchor unit's literals and inbound links.

    Two evidence kinds, both recorded on the candidate: `literal` (the same
    value lives in another unit) and `link` (another unit links to the anchor
    section). A candidate with no evidence is never produced — the machine
    finds, 3b decides.

    3a's `연관문서` guesses are checked against the same evidence. The scan
    itself is already repo-wide, so a related file with real evidence was a
    candidate anyway; what this adds is the negative report — a related file
    that produced no candidate lands in `related_unverified`, suspicion the
    machine could not confirm, carried to the report instead of dropped.
    """

    units = _index(head_tree)
    by_unit = {unit.unit_id: unit for unit in units}
    literals_by_unit = {unit.unit_id: extract_literals(unit.text) for unit in units}
    opinions = {opinion.opinion_id: opinion for opinion in intent.opinions}
    entries: list[ImpactEntry] = []
    for entry in anchor.entries:
        if entry.status != "FOUND" or not entry.unit_id:
            continue
        opinion = opinions.get(entry.opinion_id)
        related = {_posix(path) for path in (opinion.연관문서 if opinion else ())}
        unit = by_unit.get(entry.unit_id)
        if unit is None:
            entries.append(
                ImpactEntry(
                    opinion_id=entry.opinion_id,
                    anchor=None,
                    literals=(),
                    links_in=(),
                    candidates=(),
                    related_unverified=tuple(sorted(related)),
                )
            )
            continue
        own = literals_by_unit[unit.unit_id]
        values = {lit.value for lit in own if lit.value}
        target_slug = _slug(unit.section.heading)
        links_in: list[LinkIn] = []
        found: dict[tuple[str, str, int, str, str], ImpactCandidate] = {}
        for other in units:
            if other.unit_id == unit.unit_id:
                continue
            for value in sorted(values & {lit.value for lit in literals_by_unit[other.unit_id]}):
                line = _line_of(other, value)
                key = (other.file, other.unit_id, line, "literal", value)
                found[key] = ImpactCandidate(
                    file=other.file,
                    section_id=other.section_id,
                    unit_id=other.unit_id,
                    line=line,
                    via="literal",
                    match=value,
                )
            for offset, raw in enumerate(other.text.splitlines()):
                for _label, target in _LINK.findall(raw):
                    path_part, _, fragment = target.partition("#")
                    if not fragment.strip():
                        continue  # points at the file, not at the anchor section
                    if _resolve_link(other.file, path_part) != unit.file:
                        continue
                    if _slug(fragment) != target_slug:
                        continue
                    line = other.section.start + offset
                    links_in.append(LinkIn(file=other.file, line=line, target=target))
                    key = (other.file, other.unit_id, line, "link", target)
                    found[key] = ImpactCandidate(
                        file=other.file,
                        section_id=other.section_id,
                        unit_id=other.unit_id,
                        line=line,
                        via="link",
                        match=target,
                    )
        candidates = sorted(
            found.values(),
            key=lambda row: (row.file, row.line, row.unit_id, _via_rank(row.via), row.match),
        )
        candidate_files = {candidate.file for candidate in candidates}
        entries.append(
            ImpactEntry(
                opinion_id=entry.opinion_id,
                anchor=AnchorRef(
                    file=unit.file, section_id=unit.section_id, unit_id=unit.unit_id
                ),
                literals=tuple(
                    ImpactLiteral(kind=lit.kind, key=lit.key, value=lit.value) for lit in own
                ),
                links_in=tuple(
                    sorted(links_in, key=lambda row: (row.file, row.line, row.target))
                ),
                candidates=tuple(candidates),
                related_unverified=tuple(
                    sorted(path for path in related if path not in candidate_files)
                ),
            )
        )
    unanchored = [entry.opinion_id for entry in entries if entry.anchor is None]
    if not entries:
        status = "EMPTY"
    elif unanchored:
        status = f"PARTIAL — 앵커 유닛 소실 {join_ids(unanchored)}"
    else:
        status = "OK"
    return Impact(
        mr_iid=anchor.mr_iid,
        round=anchor.round,
        entries=tuple(entries),
        required=Required(
            STATUS=status,
            UNCOVERED=join_ids(unanchored),
            UNCERTAIN="none",
            CONFIDENCE="high — extract_literals 역검색과 링크 타깃 대조만 사용",
        ),
    )


# --------------------------------------------------------------------------
# 5 · gate — the design's core. It does not believe the receipt.
# --------------------------------------------------------------------------


def parse_replacement(direction: str) -> tuple[str, str] | None:
    """'A → B' out of an opinion's 수정방향, or None when it is not a swap.

    Accepts →, ->, ⇒ and => and strips the quoting a model tends to add.
    Anything else means the opinion did not ask for a substitution, so no
    LITERAL check is produced for it (an invented check would fail forever).
    """

    parts = _ARROW.split(direction)
    if len(parts) != 2:
        return None
    left, right = (part.strip().strip(_QUOTES).strip() for part in parts)
    return (left, right) if left and right else None


def build_gate(
    intent: Intent,
    anchor: Anchor,
    impact: Impact,
    changed_paths: list[str],
    base_tree: dict[str, str],
    head_tree: dict[str, str],
    *,
    max_changed_files: int,
    max_changed_lines: int,
    changed_lines: int,
    untracked: list[str],
) -> Gate:
    """Check paths, literal substitution, size and residue — reverting nobody.

    The four checks are the design's; the acting is the caller's. `reverted`
    is the complete revert set (every changed path when a size overflow set
    `revert_all`), and `failed_opinion_ids` are the LITERAL failures, which
    the design classifies as 「구현 누락」 — the cause that re-injects 3b.
    """

    changed = tuple(sorted({_posix(path) for path in changed_paths}))
    allowed = tuple(sorted(set(anchor.files) | set(impact.files)))
    non_md = tuple(path for path in changed if not path.lower().endswith(MD_SUFFIXES))
    outside = tuple(path for path in changed if path not in allowed and path not in non_md)
    path_reverted = tuple(sorted(set(non_md) | set(outside)))
    paths = GatePaths(
        changed=changed,
        allowed=allowed,
        non_md=non_md,
        outside=outside,
        reverted=path_reverted,
        result="fail" if path_reverted else "pass",
    )

    head_by_unit = {unit.unit_id: unit for unit in _index(head_tree)}
    base_by_unit = {unit.unit_id: unit for unit in _index(base_tree)}
    anchored = {entry.opinion_id: entry for entry in anchor.entries}
    checks: list[LiteralCheck] = []
    for opinion in intent.opinions:
        swap = parse_replacement(opinion.수정방향)
        entry = anchored.get(opinion.opinion_id)
        if swap is None or entry is None or entry.status != "FOUND" or not entry.unit_id:
            continue
        before, after = swap
        head_unit = head_by_unit.get(entry.unit_id)
        base_unit = base_by_unit.get(entry.unit_id)
        head_text = head_unit.text if head_unit is not None else ""
        base_text = base_unit.text if base_unit is not None else ""
        base_values = {lit.value for lit in extract_literals(base_text)}
        head_values = {lit.value for lit in extract_literals(head_text)}
        # The three-term test only means anything once A is a literal of the
        # base unit; a plain-prose term is not, and then `sets` stays null.
        sets = (
            LiteralSets(
                a_in_base=True,
                a_not_in_head=before not in head_values,
                b_in_head=after in head_values,
            )
            if before in base_values
            else None
        )
        text = LiteralText(
            a_in_head_text=before in head_text,
            b_in_head_text=after in head_text,
            a_in_base_text=before in base_text,
        )
        # An A the base section never held proves nothing by being absent
        # from head: the spec gate resolves A inside the section, so this is
        # the backstop for an artifact that bypassed it.
        passed = text.a_in_base_text and text.b_in_head_text and not text.a_in_head_text
        if sets is not None:
            passed = passed and sets.a_not_in_head and sets.b_in_head
        checks.append(
            LiteralCheck(
                unit_id=entry.unit_id,
                opinion_id=opinion.opinion_id,
                from_value=before,
                to_value=after,
                sets=sets,
                text=text,
                occurrences=OCCURRENCES_UNAVAILABLE,
                result="pass" if passed else "fail",
            )
        )

    over_size = len(changed) > max_changed_files or changed_lines > max_changed_lines
    size = GateSize(
        files=len(changed),
        max_files=max_changed_files,
        lines=changed_lines,
        max_lines=max_changed_lines,
        result="fail" if over_size else "pass",
    )
    residue_paths = tuple(sorted({_posix(path) for path in untracked}))
    residue = GateResidue(
        untracked=residue_paths, result="fail" if residue_paths else "pass"
    )

    failed = tuple(sorted({check.opinion_id for check in checks if check.result == "fail"}))
    text_only = [check.opinion_id for check in checks if check.sets is None]
    absent = [check.opinion_id for check in checks if not check.text.a_in_base_text]
    notes: list[str] = []
    if over_size:
        notes.append(
            f"규모 상한 초과 — {size.files}/{size.max_files} 파일 · "
            f"{size.lines}/{size.max_lines} 라인"
        )
    if path_reverted:
        notes.append(f"허용 밖 파일 {len(path_reverted)}건 되돌림")
    if failed:
        notes.append(f"리터럴 미치환 {len(failed)}건 — 구현 누락")
    if absent:
        notes.append(f"치환 전 값이 base 절에 없음 {len(absent)}건")
    if residue_paths:
        notes.append(f"untracked 잔여물 {len(residue_paths)}건")
    if not notes:
        status = "OK"
    else:
        status = ("FAILED — " if over_size else "PARTIAL — ") + " · ".join(notes)
    return Gate(
        mr_iid=intent.mr_iid,
        round=intent.round,
        kind="failed" if over_size else "ok",
        reverted=changed if over_size else path_reverted,
        revert_all=over_size,
        failed_opinion_ids=failed,
        paths=paths,
        literals=tuple(checks),
        size=size,
        residue=residue,
        required=Required(
            STATUS=status,
            UNCOVERED=join_ids(failed),
            UNCERTAIN=(
                "none"
                if not text_only
                else f"평문 치환 {len(text_only)}건은 text 검사만으로 판정 — 출현 횟수는 판정 불가"
            ),
            CONFIDENCE="high — changed_paths 와 base/head 유닛 대조만 사용",
        ),
    )


# --------------------------------------------------------------------------
# 7 · summary-extract — the report's three slots
# --------------------------------------------------------------------------


def _grouped(literals: Sequence[Literal]) -> dict[tuple[str, str], list[str]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for lit in literals:
        groups.setdefault((lit.kind, lit.key), []).append(lit.value)
    return groups


def _slot_lines(label: str, before: Sequence[Literal], after: Sequence[Literal]) -> list[str]:
    """One label's key_changes bullets from the literal difference.

    Grouping mirrors `literals._diff`: same (kind, key) with values that
    moved is one fact ('3.2 → 4.0'), and leftovers pair in document order —
    sorting first would pair the wrong two values.
    """

    base, head = _grouped(before), _grouped(after)
    lines: list[str] = []
    for group in sorted(set(base) | set(head)):
        shared = set(base.get(group, [])) & set(head.get(group, []))
        rest_base = [value for value in base.get(group, []) if value not in shared]
        rest_head = [value for value in head.get(group, []) if value not in shared]
        for old, new in zip(rest_base, rest_head, strict=False):
            lines.append(render_key_change(label, group[1], old, new))
        for value in rest_base[len(rest_head) :]:
            lines.append(f"{label} — {group[1]}: {value} 삭제")
        for value in rest_head[len(rest_base) :]:
            lines.append(f"{label} — {group[1]}: {value} 추가")
    return lines


def build_summary(
    verify: Verify,
    gate: Gate,
    literals_before: Mapping[str, Sequence[Literal]],
    literals_after: Mapping[str, Sequence[Literal]],
) -> Summary:
    """Fill the report's three slots — `key_changes` is machine-made, not written.

    `literals_before` / `literals_after` are keyed by the display label the
    bullets carry (e.g. 'docs/install.md § 2. 설치 절차'), so the caller
    decides the granularity and this node only differences values.

    `points_to_watch` is 3c's partial/unapplied 판정문 plus what the gate
    reverted: a file the gate silently took back is precisely a thing to
    look at, and the design forbids silent omissions everywhere else.
    """

    counts = verify.counts()
    applied = tuple(row.opinion_id for row in verify.by_verdict("applied"))
    partial = tuple(row.opinion_id for row in verify.by_verdict("partial"))
    unapplied = tuple(row.opinion_id for row in verify.by_verdict("unapplied"))

    key_changes: list[str] = []
    for label in sorted(set(literals_before) | set(literals_after)):
        key_changes.extend(
            _slot_lines(label, literals_before.get(label, ()), literals_after.get(label, ()))
        )

    watch: list[str] = []
    for kind in ("partial", "unapplied"):
        for row in verify.by_verdict(kind):
            note = row.판정문 or "판정문 없음"
            watch.append(f"[{kind}] 의견 {row.opinion_id} — {note}")
    if gate.revert_all:
        watch.append(
            f"게이트 규모 상한 초과 — {gate.size.files}/{gate.size.max_files} 파일 · "
            f"{gate.size.lines}/{gate.size.max_lines} 라인, 전량 되돌림"
        )
    elif gate.reverted:
        watch.append(f"게이트가 허용 밖 파일을 되돌렸다: {', '.join(gate.reverted)}")
    if gate.residue.untracked:
        watch.append(f"워크스페이스 잔여물 untracked {len(gate.residue.untracked)}건")

    reverted = set(gate.reverted)
    commit_paths = (
        ()
        if gate.revert_all
        else tuple(path for path in gate.paths.changed if path not in reverted)
    )
    summary_text = verify.summary or (
        f"applied {counts['applied']} · partial {counts['partial']} · "
        f"unapplied {counts['unapplied']}"
    )
    open_ids = [*partial, *unapplied]
    return Summary(
        mr_iid=verify.mr_iid,
        round=verify.round,
        applied=applied,
        partial=partial,
        unapplied=unapplied,
        commit_paths=commit_paths,
        summary=summary_text,
        key_changes=tuple(key_changes),
        points_to_watch=tuple(watch),
        required=Required(
            STATUS=(
                "OK"
                if not open_ids and gate.kind == "ok"
                else f"PARTIAL — 미반영 {join_ids(open_ids)} · gate {gate.kind}"
            ),
            UNCOVERED=join_ids(open_ids),
            UNCERTAIN=(
                "none"
                if not gate.reverted
                else f"게이트 되돌림 {len(gate.reverted)}건이 커밋 경로에서 빠졌다"
            ),
            CONFIDENCE="high — 3c 판정과 리터럴 차집합만 사용, 생성 문장 없음",
        ),
    )
