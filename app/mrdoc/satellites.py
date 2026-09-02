"""LLM satellites — headless claude CLI missions for the three agent nodes.

Delegation contract borrowed from opus-orchestrator: the orchestrator
judges, satellites only read and write. Each mission is a self-contained
spec — a role system prompt with HARD LIMITS, absolute READ paths, a BUDGET
line, and exactly one RETURN artifact — executed as a single headless
'claude -p' call with only the Read/Write tools. The executor never trusts
a satellite's word: success means the declared artifact exists on disk
afterwards; anything else is a False back to the wave loop's abort contract.
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

from .changeset import parse_changeset

logger = logging.getLogger("uvicorn.error")

FENCE = chr(96) * 3

_SCOPE = re.compile(r"^files (\d+)\.\.(\d+)$")
_EFFORT = {"analyzer": "high", "verifier": "high", "reporter": "medium"}


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
    "당신은 doc-analyzer 위성이다. "
    "오케스트레이터의 판단을 돕는 읽기·집필 전문가 — 스스로 판단하지 않는다.\n"
    "\n"
    "[HARD LIMITS]\n"
    "1. before/after 두 줄은 base/와 snapshot/의 원문에서 해당 절을 직접 읽은 뒤에만 쓴다. "
    "원문을 읽지 않고 서술한 파일은 전체가 폐기된다.\n"
    "2. 사실(수치·키:값·코드·링크)을 직접 나열하지 않는다 — "
    "기계 추출(06-literals)이 그 역할을 한다. before/after는 맥락 서술만 담는다.\n"
    "3. 레벨(L1/L2/L3)은 06-literals의 해당 유닛 "
    "차집합(removed/added/changed)을 근거로만 주장한다. "
    "근거가 애매하면 L2.\n"
    "4. severity를 부여하지 않는다. "
    "category(contradiction|stale_reference|dangling_link|terminology|completeness)만 쓴다.\n"
    "5. 변경 절의 라인 창 ±20줄만 읽는다. 파일 전체 통독은 금지.\n"
    "6. finding의 evidence quote는 원문 한 줄에서 문자 그대로 복사하고 "
    "rev(base|head)·file·line을 정확히 표기한다. "
    "quote 대조에서 탈락한 finding은 조용히 사라진다."
)

_VERIFIER_SYSTEM = (
    "당신은 doc-verifier 위성이다. 남의 산출물을 감사한다 — "
    "수정하지 않는다(Read만 쓴다).\n"
    "\n"
    "[규칙]\n"
    "1. base/·snapshot/ 원문을 직접 읽지 않고는 판정하지 않는다. "
    "못 읽었으면 uncovered에 기록한다.\n"
    "2. fidelity는 ok | distorted(왜곡) | omitted(누락). "
    "before/after가 원문보다 범위를 넓히거나 좁히면 distorted다.\n"
    "3. level_opinion은 agree | dispute. "
    "dispute여도 레벨은 바뀌지 않는다(강등 금지 원칙) — 의견만 남긴다.\n"
    "4. 애매하면 verdict를 REVISE로 하고 FIX 블록을 남긴다. 확신 없는 APPROVE는 거짓 영수증이다.\n"
    "5. checked는 지목받은 수가 아니라 실제로 읽은 유닛 수다."
)

_REPORTER_SYSTEM = (
    "당신은 doc-reporter 위성이다. 문장만 쓴다 — 새 주장을 만들지 않는다.\n"
    "\n"
    "[규칙]\n"
    "1. verdict는 50-collect.md에서 그대로 복사한다. "
    "변경하면 render 노드가 전체 리포트를 폐기한다.\n"
    "2. 모든 숫자는 50-collect.md에서 복사한다 — 재계산 금지.\n"
    "3. refs는 50-collect.md에 실재하는 u-/f- id만 지목한다. "
    "없는 id를 지목한 블록은 버려진다.\n"
    "4. 블록 종류: HEADLINE(전체 요약, 2문장 이내), "
    "VERDICT_REASON(verdict 사유 1-2문장), "
    "MUST_READ u-x(그 절이 왜 중요한지), "
    "FILE_DIGEST slug(파일별 변경 요약 1문장).\n"
    "5. UNSOURCED와 CONFLICTS는 필수 필드다 — 생략하면 clean으로 읽히는 거짓 영수증이 된다."
)


def _scope_indices(scope: str) -> tuple[int, int] | None:
    match = _SCOPE.match(scope)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _analyzer_mission(
    mission: Mission, work_dir: Path
) -> tuple[str, str, tuple[Path, ...]]:
    """Resolve the SCOPE batch to concrete files + expected artifacts."""

    changeset = parse_changeset(_read(work_dir / "00-changeset.md"))
    span = _scope_indices(mission.scope) or (0, len(changeset.files) - 1)
    lo, hi = span
    entries = changeset.files[lo : hi + 1]
    if not entries:
        raise ValueError(f"analyzer scope {mission.scope} selects no files")
    lines = ["[담당 파일] 각 파일마다 RETURN 디렉터리에 <file_id>.md 하나를 쓴다."]
    expected: list[Path] = []
    for entry in entries:
        head_path = (work_dir / "snapshot" / entry.path).resolve()
        base_rel = entry.old_path or entry.path
        base_path = (work_dir / "base" / base_rel).resolve()
        expected.append(mission.return_path / f"{entry.fid}.md")
        lines.append(
            f"- file_id {entry.fid} · head {head_path} · base {base_path}"
        )
    template = "\n".join(
        [
            "---",
            "file_id: <file_id>",
            'path: "<원본 경로>"',
            "units: <UNIT 블록 수>",
            "levels: {L1: 0, L2: 0, L3: 0}",
            "findings: <FINDING 블록 수>",
            "STATUS: OK",
            "UNCOVERED: none",
            "UNCERTAIN: none",
            "CONFIDENCE: high",
            "---",
            "",
            "## UNIT <05-structure CHANGED 표의 unit_id>",
            FENCE + "yaml",
            "section_id: <같은 표의 section_id>",
            "level: L2",
            FENCE,
            "**before** <base 원문 근거 서술>",
            "**after** <snapshot 원문 근거 서술>",
            "",
            "## FINDING f-01",
            FENCE + "yaml",
            "unit_id: <유닛 id>",
            "category: stale_reference",
            "evidence:",
            '  - {role: changed, rev: head, file: <경로>, line: <줄>, quote: "<원문 그대로>"}',
            FENCE,
            "**claim** <한 줄>",
            "**recommendation** <한 줄>",
        ]
    )
    prompt = "\n".join(
        [
            "[산출 형식] 파일당 정확히 이 템플릿을 따른다 (yaml fence 필수):",
            template,
            "",
            *lines,
            "",
            "[입력 산출물] READ 목록의 파일을 먼저 읽는다. "
            "unit_id/section_id는 05-structure.md CHANGED 표에서 그대로 가져온다.",
            f"[산출 위치] {mission.return_path.resolve()}",
        ]
    )
    return _ANALYZER_SYSTEM, prompt, tuple(expected)


def _verifier_mission(
    mission: Mission, work_dir: Path
) -> tuple[str, str, tuple[Path, ...]]:
    template = "\n".join(
        [
            "---",
            "mr_iid: <00-changeset.md 의 mr_iid>",
            "round: 1",
            "verdict: APPROVE",
            "checked: <실제로 읽은 unit 수>",
            "fidelity: {ok: 0, distorted: 0, omitted: 0}",
            "levels: {agree: 0, dispute: 0}",
            "required_fixes: 0",
            "uncovered: none",
            "uncertain: none",
            'confidence: "high — <근거 한 줄>"',
            "---",
            "",
            "## UNIT <unit_id>",
            FENCE + "yaml",
            "fidelity: ok",
            "level_claimed: L2",
            "level_opinion: agree",
            FENCE,
            "**why** <원문 근거 한 줄>",
            "",
            "## FIX r-01",
            FENCE + "yaml",
            "target: <unit_id>",
            "field: after",
            "reason: fidelity_distorted",
            FENCE,
            "",
            "## RECEIPT",
            "<이 스펙 블록 원문 복사>",
        ]
    )
    prompt = "\n".join(
        [
            "[산출 형식] 정확히 이 템플릿을 따른다 (yaml fence 필수):",
            template,
            "",
            "[입력] 20-analysis/ 의 모든 UNIT과 30-levelcheck.md, 그리고 base/·snapshot/ 원문.",
            "[입력 산출물] READ 목록의 파일을 먼저 읽는다.",
            f"[산출 위치] {mission.return_path.resolve()} — 이 파일 하나만 쓴다.",
        ]
    )
    return _VERIFIER_SYSTEM, prompt, (mission.return_path,)


def _reporter_mission(
    mission: Mission, work_dir: Path
) -> tuple[str, str, tuple[Path, ...]]:
    template = "\n".join(
        [
            "---",
            "verdict: <50-collect.md 의 verdict 그대로>",
            "sentences: <본문 문장 수>",
            "STATUS: OK",
            'SOURCES: "<n>/<n> 대응"',
            "UNSOURCED: none",
            "CONFLICTS: none",
            "UNCOVERED: none",
            "CONFIDENCE: high",
            "---",
            "",
            "## HEADLINE",
            FENCE + "yaml",
            "refs: []",
            FENCE,
            "<전체 요약 2문장 이내>",
            "",
            "## VERDICT_REASON",
            FENCE + "yaml",
            "refs: [<u-/f- id들>]",
            FENCE,
            "<verdict 사유 1-2문장>",
            "",
            "## MUST_READ <u-id>",
            FENCE + "yaml",
            "refs: [<같은 u-id>]",
            FENCE,
            "**why_matters** <그 절이 왜 중요한지>",
            "",
            "## FILE_DIGEST <파일 slug>",
            FENCE + "yaml",
            "refs: [<그 파일의 u-id들>]",
            FENCE,
            "<파일별 변경 요약 1문장>",
        ]
    )
    prompt = "\n".join(
        [
            "[산출 형식] 정확히 이 템플릿을 따른다 (yaml fence 필수):",
            template,
            "",
            "[입력] 50-collect.md 가 유일한 사실 원천이다. "
            "must_read 목록의 각 id마다 MUST_READ 블록을, 변경 파일마다 FILE_DIGEST 블록을 쓴다.",
            "[입력 산출물] READ 목록의 파일을 먼저 읽는다.",
            f"[산출 위치] {mission.return_path.resolve()} — 이 파일 하나만 쓴다.",
        ]
    )
    return _REPORTER_SYSTEM, prompt, (mission.return_path,)


_MISSIONS: dict[
    str, Callable[[Mission, Path], tuple[str, str, tuple[Path, ...]]]
] = {
    "analyzer": _analyzer_mission,
    "verifier": _verifier_mission,
    "reporter": _reporter_mission,
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
        claude_bin = settings.claude_bin or shutil.which("claude")
        if not claude_bin:
            logger.warning("mrdoc satellite: claude CLI not found on PATH")
            return False
        try:
            system_prompt, prompt, expected = builder(mission, work_dir)
        except (OSError, ValueError) as error:
            logger.warning(
                "mrdoc satellite(%s): mission build failed: %s",
                mission.agent,
                error,
            )
            return False
        cmd = [
            claude_bin,
            "-p",
            "--model",
            settings.mrdoc_satellite_model,
            "--effort",
            _EFFORT[mission.agent],
            "--max-budget-usd",
            str(mission.budget_usd),
            "--tools",
            "Read,Write",
            "--permission-mode",
            "acceptEdits",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--system-prompt",
            system_prompt,
        ]
        env = {
            key: value
            for key, value in _process_env().items()
            if key.upper() not in _CREDENTIAL_ENV_KEYS
        }
        timeout = settings.mrdoc_satellite_timeout_seconds
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
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
                "mrdoc satellite(%s): claude exited %s: %s",
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
        logger.info(
            "mrdoc satellite(%s): artifact ok (%s)", mission.agent, mission.return_path
        )
        return True

    return run
