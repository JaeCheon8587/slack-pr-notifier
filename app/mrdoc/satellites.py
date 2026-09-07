"""LLM satellites — headless Codex CLI missions for the three agent nodes.

Delegation contract borrowed from opus-orchestrator: the orchestrator
judges, satellites only read and write. Each mission is a self-contained
spec — a role system prompt with HARD LIMITS, absolute READ paths, a BUDGET
line, and exactly one RETURN artifact — executed as a single headless
'codex exec' call under the workspace-write sandbox. The executor never
trusts a satellite's word: success means the declared artifact exists on
disk afterwards; anything else is a False back to the wave loop's abort
contract.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.ai_runner import _CREDENTIAL_ENV_KEYS
from app.config import Settings

from . import classes, vocab
from .analysis import AnalysisUnit, FileAnalysis, parse_analysis, render_analysis
from .changeset import Changeset, parse_changeset
from .inventory import build_inventory
from .levelcheck import parse_levelcheck
from .literals import parse_literals
from .structure import parse_structure
from .verifier import (
    CountMismatch,
    CountsCheck,
    Fix,
    Verifier,
    VerifierUnit,
    parse_verifier,
    render_verifier,
)
from .workspace import artifact_paths

logger = logging.getLogger("uvicorn.error")

FENCE = chr(96) * 3

_SCOPE = re.compile(r"^files (\d+)\.\.(\d+)$")
#: The FIX re-call's scope — named units instead of a file batch.
_SCOPE_UNITS = re.compile(r"^units (.+)$")
_EFFORT = {"analyzer": "high", "verifier": "high"}


def _no_check() -> str:
    return ""


@dataclass(frozen=True)
class MissionPlan:
    """What a satellite is told, what it owes, and how to audit the result.

    The executor cannot trust a satellite's word, so success means the
    declared artifacts exist *and* `check` finds nothing wrong with them.
    """

    system: str
    prompt: str
    expected: tuple[Path, ...]
    check: Callable[[], str] = _no_check


def _round_number(work_dir: Path) -> int:
    """1 before the FIX re-call has been spent, 2 after."""

    return 2 if artifact_paths(work_dir)["fix_marker"].exists() else 1


@dataclass(frozen=True)
class Mission:
    """One satellite assignment parsed back out of the orchestrator's spec."""

    wave: int
    agent: str
    read: tuple[Path, ...]
    budget_usd: float
    return_path: Path
    scope: str = ""


def parse_spec(text: str) -> Mission:
    """WAVE/SPEC/READ/BUDGET/RETURN/SCOPE lines -> Mission (ValueError if bad)."""

    fields: dict[str, str] = {}
    for line in text.splitlines():
        head, _, rest = line.partition(" ")
        if head in ("WAVE", "SPEC", "READ", "BUDGET", "RETURN", "SCOPE"):
            fields[head] = rest
    if "SPEC" not in fields or "RETURN" not in fields:
        raise ValueError("spec block missing SPEC/RETURN lines")
    read = tuple(
        Path(part.strip())
        for part in fields.get("READ", "").split(",")
        if part.strip()
    )
    return Mission(
        wave=int(fields.get("WAVE") or 0),
        agent=fields["SPEC"],
        read=read,
        budget_usd=float(fields.get("BUDGET") or 0.0),
        return_path=Path(fields["RETURN"]),
        scope=fields.get("SCOPE", ""),
    )


_ANALYZER_SYSTEM = (
    "당신은 doc-analyzer 위성이다. 설명만 쓴다 — 판정하지 않는다.\n"
    "\n"
    "[HARD LIMITS]\n"
    "1. 원문을 읽지 않고 설명을 쓰지 않는다. 07-excerpts.md 의 발췌와 "
    "base/·snapshot/ 의 해당 절을 실제로 읽는다. 못 읽었으면 UNCOVERED 에 적는다.\n"
    "2. 사실을 직접 서술한다. 06-literals.md 의 값을 인용해 "
    "무엇이 어디서 어디로 바뀌었는지 쓴다. 원문과 값은 도구가 리포트에 병기한다 — "
    "설명은 그것을 풀어 쓰는 자리다.\n"
    "3. 왜(동기·배경) · 그래서(영향·의미) · 평가 어휘를 쓰지 않는다. "
    "금지 어휘: " + vocab.prompt_line() + ". "
    "이 목록은 mrdoc levelcheck 어휘 게이트와 같은 파일을 공유한다 — "
    "걸린 설명은 재작성 대상이다.\n"
    "4. 분류(class)는 표현 후보에만 낸다. 차집합이 공집합이고 본문만 다른 유닛에만 "
    "표현 또는 의미를 쓰고 나머지는 null 로 둔다 — "
    "의미(값)·구조는 도구 확정값이라 바꾸지 않는다.\n"
    "5. 지적 · 권고 · 심각도 · 머지 의견을 쓰지 않는다. 스키마에 그 필드가 없다.\n"
    "6. 절 라인 창 ±20줄만 읽는다. 파일 통독 금지, 같은 파일 재Read 금지.\n"
    "7. 템플릿에 없는 섹션을 만들지 않는다. UNIT · FILE_SUMMARY · RECEIPT 셋뿐이다."
)

#: The output template is rendered, never typed. In the measured run a
#: hand-written template told the satellite to write list-objects as block
#: sequences while the parser only accepted inline flow: the artifact was
#: discarded whole and the pipeline still reported complete. One renderer,
#: one shape, and a test that parses this very string.
_ANALYZER_TEMPLATE = render_analysis(
    FileAnalysis(
        file_id="<file_id>",
        path="<파일 경로>",
        # Placeholders carry no spaces: '## UNIT <id>' takes one token, so a
        # spaced placeholder would demonstrate a header the parser truncates.
        units=(
            AnalysisUnit(
                unit_id="u-xxxxxxxx",
                section_id="s-xxxxxxxx",
                klass="",
                explanation="<2~4문장. 바뀐 줄을 전부 서술한다>",
            ),
            AnalysisUnit(
                unit_id="u-yyyyyyyy",
                section_id="s-yyyyyyyy",
                klass="표현",
                explanation="<2~4문장. 표현 변경의 종류를 쓴다>",
            ),
        ),
        summary_refs=("u-xxxxxxxx", "u-yyyyyyyy"),
        summary=(
            "<파일 전체 요약 한 문단 — 구조 변화 여부로 시작해 "
            "종류별 카운트·위치를 세어 쓴다>"
        ),
        status="OK",
        uncovered="none",
        uncertain="none",
        confidence="high — <직접 읽고 확인한 사실만>",
    )
)

_ANALYZER_WRITING = (
    "[설명 작성]\n"
    "- 발췌의 before 와 after 를 줄 단위로 대조해 바뀐 줄을 전부 서술한다. "
    "한 유닛에 값 변경과 표현 변경이 함께 있으면 둘 다 쓴다.\n"
    "- 값 변경은 06-literals.md 의 값을 인용하되 "
    "<항목> <이전값> → <이후값> 형식으로 쓴다(예: 검증 기준 3.12.4 → 3.12.7).\n"
    "- 조건 변경은 어떤 조건이 어디서 어디로 바뀌는지 쓴다.\n"
    "- yaml 키워드를 문장에 쓰지 않는다: key · from · to 라는 단어가 설명에 나오면 안 된다.\n"
    "- 표현 변경은 종류(동의어·어순·서식·오탈자·문장 분할/병합)만 쓴다.\n"
    "- ADDED 유닛은 추가된 내용이 무엇을 다루는지, 기존 어느 섹션 뒤/안에 "
    "들어갔는지, 기존 내용과 어떤 관계인지(보충·대체·신규 주제)를 쓴다.\n"
    "- REMOVED 유닛은 삭제된 내용이 무엇을 다루었는지를 쓰고, "
    "대체하는 추가 유닛이 있으면 연결해 쓰며 없으면 순삭제라고 적는다.\n"
    "- 발췌에는 보이지만 06-literals.md 에 대응 원자가 없는 변경도 서술에서 "
    "빠뜨리지 않고 리턴 UNCERTAIN 에 한 줄 밝힌다 — 인벤토리와 어긋나는 "
    "흔적을 남겨야 verifier 가 잡는다.\n"
    "- 바뀐 줄을 빠뜨리면 doc-verifier 가 omitted 로 잡는다.\n"
    "- 원문을 설명에 통째로 옮기지 않는다 — 원문은 07-excerpts.md 가 든다."
)

_ANALYZER_RETURN = (
    "[리턴] 12줄 이내로 다음 필드만 출력한다.\n"
    "STATUS: OK | EMPTY | PARTIAL — <잘린 범위> | BLOCKED — <이유> | FAILED — <이유>\n"
    "UNITS: <쓴 UNIT 블록 수>/<지목받은 유닛 수>\n"
    "CLASSES: 의미 <n> · 표현 <n> · 후보판정 <n>\n"
    "FILES: <쓴 파일 수>/<담당 파일 수>\n"
    "UNCOVERED: <처리하지 못한 id 목록> | none\n"
    "UNCERTAIN: <애매한 점 한 줄> | none\n"
    "CONFIDENCE: high|medium|low — <직접 확인한 사실만 근거로>\n"
    "REPORT: <산출 디렉터리 절대경로>\n"
    "리턴을 조립하기 전에 각 산출물 말미에 리턴 블록을 '## RECEIPT' 로 복사한다."
)

_VERIFIER_SYSTEM = (
    "당신은 doc-verifier 위성이다. 남의 산출물을 감사한다 — "
    "수정하지 않는다(Read 와 자기 리포트 Write 만 쓴다).\n"
    "\n"
    "[HARD LIMITS]\n"
    "1. 보는 것은 설명의 충실도 하나다. fidelity 는 "
    "ok | invented(원문에 없는 내용) | omitted(바뀐 것을 빠뜨림).\n"
    "2. 07-excerpts.md 의 before/after 와 base/·snapshot/ 원문을 직접 읽지 않고는 "
    "판정하지 않는다. 못 읽은 유닛은 판정하지 말고 uncovered 에 적는다.\n"
    "3. checked 는 지목받은 수가 아니라 실제로 읽은 유닛 수다.\n"
    "4. 애매하면 ok 로 넘기지 않는다. FIX 블록과 why 한 줄을 남긴다 — "
    "부당한 통과가 이 계층을 없앤다.\n"
    "5. class_opinion 은 agree | dispute 의견일 뿐이다. dispute 여도 분류는 바뀌지 않는다 "
    "(강등 금지) — 의견만 남는다.\n"
    "6. 어휘 위반은 잡지 않는다. mrdoc levelcheck 어휘 게이트가 먼저 본다.\n"
    "7. 문서 내용의 옳고 그름 · 머지 의견 · 심각도 · 권고를 쓰지 않는다. "
    "판정 자체가 없고 스키마에 그 필드가 없다.\n"
    "8. 남의 산출물을 수정하지 않는다. 20-analysis/*.md 는 건드리지 않고 "
    "FIX 에 대상 id 만 적는다.\n"
    "9. [카운트 대조] FILE_SUMMARY 의 카운트 서술이 프롬프트에 주어진 원자 "
    "집계와 모순되면 COUNTS 블록에 mismatch 로 남기고 "
    "FIX(field: FILE_SUMMARY, reason: counts_mismatch) 를 쓴다. "
    "존재 모순만 mismatch 다 — 'n곳' 과 'n항목' 은 단위가 다를 뿐이므로 "
    "숫자 차이만으로 불일치로 삼지 않는다. 설명이나 FILE_SUMMARY 가 명시적으로 "
    "서술한 변경에 대응하는 원자가 인벤토리에 없으면 그것도 존재 모순이다 — "
    "COUNTS 에 mismatch 로 남긴다."
)

#: Same rule as the analyzer: the template is rendered by the parser's own
#: renderer, so prompt and parser cannot describe different shapes.
_VERIFIER_TEMPLATE = render_verifier(
    Verifier(
        mr_iid=0,
        round=1,
        checked=2,
        uncovered="none",
        uncertain="none",
        confidence="high — <직접 읽은 절 목록>",
        units=(
            VerifierUnit(
                unit_id="u-xxxxxxxx",
                fidelity="ok",
                class_stated="",
                class_opinion="agree",
            ),
            VerifierUnit(
                unit_id="u-yyyyyyyy",
                fidelity="invented",
                class_stated="표현",
                class_opinion="agree",
                why="<원문에 없는 범위를 설명이 만들었다는 근거 한 줄>",
            ),
        ),
        counts=(
            CountsCheck(
                file_id="f-zzzzzzzz",
                mismatches=(
                    CountMismatch(
                        target="f-zzzzzzzz",
                        stated="<FILE_SUMMARY 가 말한 카운트>",
                        inventory="<원자 집계가 잰 카운트>",
                    ),
                ),
                why="<어느 문장이 어느 숫자와 모순인지 한 줄>",
            ),
        ),
        fixes=(
            Fix(
                fix_id="r-01",
                target="u-yyyyyyyy",
                field="설명",
                reason="fidelity_invented",
            ),
            Fix(
                fix_id="r-02",
                target="f-zzzzzzzz",
                field="FILE_SUMMARY",
                reason="counts_mismatch",
            ),
        ),
    )
)

_VERIFIER_RETURN = (
    "[리턴] 12줄 이내로 다음 필드만 출력한다.\n"
    "STATUS: OK | EMPTY | PARTIAL — <잘린 범위> | BLOCKED — <이유> | FAILED — <이유>\n"
    "CHECKED: <실제로 읽은 unit 수>/<전체 unit 수> unit\n"
    "FIDELITY: ok <n> / invented <n> / omitted <n>\n"
    "CLASS_OPINION: agree <n> / dispute <n>\n"
    "REQUIRED_FIXES: <FIX 블록 수>\n"
    "UNCOVERED: <읽지 못한 unit id 목록> | none\n"
    "UNCERTAIN: <애매한 점 한 줄> | none\n"
    "CONFIDENCE: high|medium|low — <직접 확인한 사실만 근거로>\n"
    "REPORT: <40-verifier.md 절대경로>\n"
    "리턴을 조립하기 전에 산출물 말미에 리턴 블록을 '## RECEIPT' 로 복사한다."
)

def _scope_indices(scope: str) -> tuple[int, int] | None:
    match = _SCOPE.match(scope)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _scope_units(scope: str) -> tuple[str, ...] | None:
    """'units u-a,u-b' -> the ids, or None when this is a normal file batch."""

    match = _SCOPE_UNITS.match(scope)
    if not match:
        return None
    return tuple(
        part.strip() for part in match.group(1).split(",") if part.strip()
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _unit_audit(paths: tuple[Path, ...], *, preserve: bool = False) -> Callable[[], str]:
    """Audit the analyzer's own output before the wave accepts it.

    Duplicate ids are checked on every call, first one included: the yaml
    lookup is keyed by unit_id, so two blocks under one id make the second
    silently win — a 설명 that was written and then lost inside its own
    artifact. A file that does not parse at all is left alone on a first
    call; load_analyses_split isolates it as '설명 생성 실패' and the report
    still renders, which is the design's partial-failure rule rather than a
    wave abort.

    `preserve` adds the re-call's other half. A satellite told to redo two
    units can overwrite the file with only those two, silently dropping the
    rest — the report would then show '설명 생성 실패' for units that had a
    perfectly good 설명 a minute earlier. There the prior artifact is the
    contract, so a rewrite that no longer parses is a rejection too.
    """

    before = {
        path: tuple(
            unit.unit_id
            for unit in parse_analysis(path.read_text(encoding="utf-8")).units
        )
        for path in paths
        if preserve and path.is_file()
    }

    def check() -> str:
        for path in paths:
            if not path.is_file():
                return f"{path.name} 이 사라졌다"
            try:
                after = [
                    unit.unit_id
                    for unit in parse_analysis(
                        path.read_text(encoding="utf-8")
                    ).units
                ]
            except ValueError as error:
                if not preserve:
                    continue  # isolated downstream, not a wave abort
                return f"{path.name} 파싱 실패: {error}"
            duplicated = sorted({unit for unit in after if after.count(unit) > 1})
            if duplicated:
                return f"{path.name} 에 중복 유닛: {', '.join(duplicated)}"
            missing = [unit for unit in before.get(path, ()) if unit not in after]
            if missing:
                return f"{path.name} 에서 유닛 유실: {', '.join(missing)}"
        return ""

    return check


def _analyzer_mission(mission: Mission, work_dir: Path) -> MissionPlan:
    """Resolve the SCOPE batch to concrete files + expected artifacts."""

    changeset = parse_changeset(_read(work_dir / "00-changeset.md"))
    structure = parse_structure(_read(work_dir / "05-structure.md"))
    units_by_file: dict[str, list[str]] = {}
    for unit in structure.changed:
        units_by_file.setdefault(unit.file_id, []).append(unit.unit_id)

    scoped = _scope_units(mission.scope)
    if scoped is not None:
        return _fix_mission(mission, work_dir, changeset, units_by_file, scoped)

    span = _scope_indices(mission.scope) or (0, len(changeset.files) - 1)
    lo, hi = span
    entries = changeset.files[lo : hi + 1]
    if not entries:
        raise ValueError(f"analyzer scope {mission.scope} selects no files")

    excerpts = (work_dir / "07-excerpts.md").resolve()
    lines = ["[담당 파일] 각 파일마다 RETURN 디렉터리에 <file_id>.md 하나를 쓴다."]
    expected: list[Path] = []
    for entry in entries:
        head_path = (work_dir / "snapshot" / entry.path).resolve()
        base_rel = entry.old_path or entry.path
        base_path = (work_dir / "base" / base_rel).resolve()
        expected.append(mission.return_path / f"{entry.fid}.md")
        assigned = units_by_file.get(entry.fid, [])
        lines.append(f"- file_id {entry.fid} · path {entry.path}")
        lines.append(f"  units: {', '.join(assigned) if assigned else '(없음)'}")
        lines.append(
            f"  발췌: {excerpts} 의 '## EXCERPT <unit_id>' 블록 (before/after 원문)"
        )
        lines.append(f"  원문: head {head_path} · base {base_path}")
    prompt = "\n".join(
        [
            "[산출 형식] 파일당 정확히 이 템플릿을 따른다 (yaml fence 필수). "
            "리스트-객체는 한 줄 flow 로만 쓴다:",
            _ANALYZER_TEMPLATE,
            "",
            *lines,
            "",
            "[읽는 순서] 07-excerpts.md 의 담당 유닛 → 06-literals.md 의 같은 유닛 → "
            "05-structure.md CHANGED 표의 자기 파일 행 → base/·snapshot/ 의 해당 절 ±20줄.",
            "unit_id 와 section_id 는 05-structure.md CHANGED 표에서 그대로 가져온다 — "
            "표에 없는 id 를 지목한 블록은 버려진다.",
            "담당 file_id 의 유닛이 하나도 없으면 BLOCKED — no changed unit 으로 끝낸다.",
            "",
            _ANALYZER_WRITING,
            "",
            _ANALYZER_RETURN,
            "",
            f"[산출 위치] {mission.return_path.resolve()}",
        ]
    )
    return MissionPlan(
        _ANALYZER_SYSTEM, prompt, tuple(expected), _unit_audit(tuple(expected))
    )


def _fix_mission(
    mission: Mission,
    work_dir: Path,
    changeset: Changeset,
    units_by_file: dict[str, list[str]],
    targets: tuple[str, ...],
) -> MissionPlan:
    """The one re-call: rewrite these units' 설명, leave every other block alone.

    Reasons come from the two gates that produced them — doc-verifier's FIX
    blocks and levelcheck's vocab hits — read here from the files rather than
    carried through the orchestrator, so the spec stays deterministic.
    """

    owner = {
        unit: fid for fid, units in units_by_file.items() for unit in units
    }
    paths = {entry.fid: entry.path for entry in changeset.files}
    reasons = _fix_reasons(work_dir)
    # counts_mismatch FIX targets are file_ids — the FILE_SUMMARY is the
    # field being rewritten, so they ride along their file's mission.
    file_targets = tuple(dict.fromkeys(t for t in targets if t in paths))
    grouped: dict[str, list[str]] = {}
    for unit in targets:
        fid = owner.get(unit)
        if fid is None:
            continue  # points at nothing — the design drops inventions
        grouped.setdefault(fid, []).append(unit)
    if not grouped and not file_targets:
        raise ValueError(f"fix scope {mission.scope} selects no known unit")

    excerpts = (work_dir / "07-excerpts.md").resolve()
    lines = ["[재작성 대상] 아래 대상만 다시 쓴다."]
    counts_rows = (
        {row.split(" ", 2)[1]: row for row in _counts_table(work_dir, changeset)}
        if file_targets
        else {}
    )
    expected: list[Path] = []
    for fid in sorted(set(grouped) | set(file_targets)):
        expected.append(mission.return_path / f"{fid}.md")
        lines.append(f"- file_id {fid} · path {paths.get(fid, fid)}")
        for unit in grouped.get(fid, []):
            lines.append(f"  · {unit} — 설명 — {reasons.get(unit, '재작성 요청')}")
        if fid in file_targets:
            lines.append(
                f"  · FILE_SUMMARY — {reasons.get(fid, '집계 불일치')}"
            )
            if fid in counts_rows:
                lines.append(f"    원자 집계: {counts_rows[fid].split(': ', 1)[-1]}")
    prompt = "\n".join(
        [
            "[산출 형식] 기존 파일과 같은 템플릿을 유지한다 (yaml fence 필수):",
            _ANALYZER_TEMPLATE,
            "",
            *lines,
            "",
            "[보존] 대상이 아닌 UNIT 블록과 FILE_SUMMARY 는 기존 파일의 내용을 "
            "그대로 유지한다. 파일을 새로 쓰더라도 나머지 유닛이 전부 남아 있어야 하고, "
            "같은 unit_id 를 두 번 쓰지 않는다 — 유실이나 중복이 있으면 결과가 폐기된다.",
            "[FILE_SUMMARY 재작성] FILE_SUMMARY 가 대상인 파일은 요약 문단만 "
            "다시 쓴다 — 카운트 서술은 위에 준 원자 집계와 일치해야 하고 "
            "그 파일의 UNIT 블록은 그대로 둔다.",
            f"[근거] {excerpts} 의 before/after 와 base/·snapshot/ 원문을 다시 읽고 쓴다.",
            "",
            _ANALYZER_WRITING,
            "",
            _ANALYZER_RETURN,
            "",
            f"[산출 위치] {mission.return_path.resolve()}",
        ]
    )
    return MissionPlan(
        _ANALYZER_SYSTEM,
        prompt,
        tuple(expected),
        _unit_audit(tuple(expected), preserve=True),
    )


def _fix_reasons(work_dir: Path) -> dict[str, str]:
    """unit_id -> why it is being redone, from 40-verifier and 30-levelcheck."""

    paths = artifact_paths(work_dir)
    reasons: dict[str, str] = {}
    if paths["verifier"].is_file():
        try:
            report = parse_verifier(paths["verifier"].read_text(encoding="utf-8"))
        except ValueError:
            report = None
        for fix in report.fixes if report else ():
            reasons[fix.target] = f"{fix.reason} ({fix.field})"
    if paths["levelcheck"].is_file():
        try:
            check = parse_levelcheck(paths["levelcheck"].read_text(encoding="utf-8"))
        except ValueError:
            check = None
        for row in check.units if check else ():
            if not row.vocab_violation:
                continue
            hit = "금지 어휘: " + " · ".join(row.vocab_violation)
            reasons[row.unit_id] = (
                f"{reasons[row.unit_id]} / {hit}" if row.unit_id in reasons else hit
            )
    return reasons


def _counts_table(work_dir: Path, changeset: Changeset) -> list[str]:
    """Per-file atom counts — the measured numbers prose is audited against.

    The verifier's counts gate needs the inventory's numbers, not a re-derivation:
    the satellite reads them off the prompt and only judges whether the
    FILE_SUMMARY contradicts them. Measuring here keeps one source — the same
    build_inventory 50-collect counts from.
    """

    structure = parse_structure(_read(work_dir / "05-structure.md"))
    literals = parse_literals(_read(work_dir / "06-literals.md"))
    inventory = build_inventory(structure, literals, changeset)
    lines: list[str] = []
    for entry in changeset.files:
        axes = inventory.matrix_by_file.get(entry.fid, {})
        described: list[str] = []
        for axis in (classes.STRUCTURE, classes.MEANING, classes.EXPRESSION):
            ops = axes.get(axis, {})
            hits = [
                f"{op} {ops.get(op, 0)}"
                for op in ("추가", "삭제", "변경")
                if ops.get(op, 0)
            ]
            if hits:
                described.append(f"{axis}({' · '.join(hits)})")
        lines.append(
            f"- {entry.fid} ({entry.path}): "
            + (" · ".join(described) if described else "원자 없음")
        )
    return lines


def _verifier_mission(mission: Mission, work_dir: Path) -> MissionPlan:
    changeset = parse_changeset(_read(work_dir / "00-changeset.md"))
    excerpts = (work_dir / "07-excerpts.md").resolve()
    literals = (work_dir / "06-literals.md").resolve()
    analysis_dir = (work_dir / "20-analysis").resolve()
    prompt = "\n".join(
        [
            "[산출 형식] 정확히 이 템플릿을 따른다 (yaml fence 필수). "
            "카운트 필드는 블록에서 다시 세므로 블록과 어긋나면 블록이 이긴다:",
            _VERIFIER_TEMPLATE,
            "",
            f"[mr_iid] {changeset.mr_iid}",
            f"[round] {_round_number(work_dir)}",
            "",
            "[읽는 순서] "
            f"{analysis_dir} 의 UNIT 별 설명 → {excerpts} 의 같은 유닛 before/after → "
            f"{literals} 의 같은 유닛 값 → "
            f"{(work_dir / 'base').resolve()} · {(work_dir / 'snapshot').resolve()} 의 변경 절.",
            "20-analysis 의 UNIT 마다 UNIT 블록 하나를 쓴다. "
            "class_stated 는 20-analysis 가 쓴 class 를 그대로 옮기고, 없으면 null 이다.",
            "fidelity 가 ok 가 아닌 유닛마다 FIX 블록을 하나씩 쓴다 "
            "(reason: fidelity_invented | fidelity_omitted). "
            "ok 인 유닛에는 FIX 를 쓰지 않는다.",
            "",
            "[카운트 대조] FILE_SUMMARY 마다 아래 원자 집계와 대조한다. "
            "'없다'는 서술 뒤에 실재 원자가 있으면 COUNTS 블록에 mismatch 로 "
            "남기고 FIX(field: FILE_SUMMARY, reason: counts_mismatch) 를 쓴다. "
            "모순이 없는 파일의 COUNTS 블록은 쓰지 않는다.",
            *_counts_table(work_dir, changeset),
            "",
            _VERIFIER_RETURN,
            "",
            f"[산출 위치] {mission.return_path.resolve()} — 이 파일 하나만 쓴다.",
        ]
    )
    return MissionPlan(_VERIFIER_SYSTEM, prompt, (mission.return_path,))


_MISSIONS: dict[str, Callable[[Mission, Path], MissionPlan]] = {
    "analyzer": _analyzer_mission,
    "verifier": _verifier_mission,
}


def _process_env() -> dict[str, str]:
    """Process env copy — split out so tests can patch it."""

    return dict(os.environ)


def satellite_executor(
    settings: Settings, work_dir: Path
) -> Callable[[str], bool]:
    """Build the agent executor the rail injects into run_to_completion."""

    def run(spec_text: str) -> bool:
        try:
            mission = parse_spec(spec_text)
        except ValueError as error:
            logger.warning("mrdoc satellite: unparseable spec (%s)", error)
            return False
        builder = _MISSIONS.get(mission.agent)
        if builder is None:
            logger.warning("mrdoc satellite: unknown agent %r", mission.agent)
            return False
        codex_bin = settings.codex_bin or shutil.which("codex")
        if not codex_bin:
            logger.warning("mrdoc satellite: codex CLI not found on PATH")
            return False
        try:
            plan = builder(mission, work_dir)
        except (OSError, ValueError) as error:
            logger.warning(
                "mrdoc satellite(%s): mission build failed: %s",
                mission.agent,
                error,
            )
            return False
        cmd = [
            codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(work_dir),
            "-c",
            'model_reasoning_effort="' + _EFFORT[mission.agent] + '"',
        ]
        system_prompt, prompt, expected = plan.system, plan.prompt, plan.expected
        if settings.mrdoc_satellite_model:
            cmd += ["--model", settings.mrdoc_satellite_model]
        cmd += ["-"]
        prompt_text = system_prompt + "\n\n---\n\n" + prompt
        env = {
            key: value
            for key, value in _process_env().items()
            if key.upper() not in _CREDENTIAL_ENV_KEYS
        }
        timeout = settings.mrdoc_satellite_timeout_seconds
        try:
            proc = subprocess.run(
                cmd,
                input=prompt_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=work_dir,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "mrdoc satellite(%s): timed out after %ss", mission.agent, timeout
            )
            return False
        except OSError as error:
            logger.warning(
                "mrdoc satellite(%s): failed to start: %s",
                mission.agent,
                type(error).__name__,
            )
            return False
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "no error output")[:500]
            logger.warning(
                "mrdoc satellite(%s): codex exited %s: %s",
                mission.agent,
                proc.returncode,
                detail,
            )
            return False
        missing = [path for path in expected if not path.is_file()]
        if missing:
            names = ", ".join(path.name for path in missing)
            logger.warning(
                "mrdoc satellite(%s): artifact missing after run: %s",
                mission.agent,
                names,
            )
            return False
        problem = plan.check()
        if problem:
            logger.warning(
                "mrdoc satellite(%s): result rejected: %s", mission.agent, problem
            )
            return False
        logger.info(
            "mrdoc satellite(%s): artifact ok (%s)", mission.agent, mission.return_path
        )
        return True

    return run
