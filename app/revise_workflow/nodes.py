"""The four deterministic nodes — anchor / impact / gate / summary-extract.

docs/revise-workflow.html moves the original steps 2, 3 and 6 off the LLM
entirely: existence checking, companion-change discovery and comparison all
reduce to string work this repo already has as pure functions
(markdown_tree / literals / ids). The LLM is left with the two things that
are not comparisons — which strings to look for (3a) and which of the found
candidates are real (3b).

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
from dataclasses import dataclass

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
    Required,
    Summary,
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
            a_in_head_text=before in head_text, b_in_head_text=after in head_text
        )
        passed = text.b_in_head_text and not text.a_in_head_text
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
