import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.config import Settings

logger = logging.getLogger("uvicorn.error")


class MRReview(BaseModel):
    """Structured AI summary of a merge request (descriptive, not a verdict)."""

    summary: str = Field(description="이 MR이 무엇을 하는지 한 줄 요약")
    key_changes: list[str] = Field(description="주요 변경점 (파일/동작 단위, 3~6개)")
    points_to_watch: list[str] = Field(
        description="리뷰어가 살펴볼 지점 (버그 가능성·부작용·테스트 누락 등)"
    )


REVIEW_JSON_SCHEMA = json.dumps(
    MRReview.model_json_schema(),
    ensure_ascii=False,
    separators=(",", ":"),
)


SYSTEM_PROMPT = (
    "당신은 GitLab Merge Request를 분석해 리뷰어가 빠르게 파악하도록 돕는 조력자입니다. "
    "사용자 메시지에 담긴 변경 diff와 파일 내용만 근거로 무엇이 어떻게 바뀌었는지 요약하고, "
    "리뷰어가 살펴볼 지점을 정리하세요. 승인/거절 같은 최종 판단은 내리지 마세요. "
    "도구를 사용하지 말고(주어진 텍스트가 전부입니다), 추측을 사실처럼 쓰지 마세요. "
    "모든 텍스트는 한국어로 간결하게 작성합니다.\n"
    "출력은 오직 아래 형태의 JSON 객체 하나만, 코드펜스나 다른 설명 없이 출력하세요:\n"
    '{"summary": "<한 줄 요약>", '
    '"key_changes": ["<주요 변경점>", "..."], '
    '"points_to_watch": ["<살펴볼 지점>", "..."]}'
)


async def review_merge_request(
    mr: dict[str, Any], context: dict[str, Any], settings: Settings
) -> MRReview | None:
    """Analyze an MR with the Claude Code CLI and return a structured review, or None.

    Failures never propagate — the Slack notification must go out with or without AI.
    """

    if not settings.ai_enabled:
        return None
    try:
        prompt = _build_prompt(mr, context, settings.ai_max_input_chars)
        raw = await asyncio.to_thread(_run_claude, prompt, settings)
        if not raw:
            return None
        return _parse_review(raw)
    except Exception:
        logger.exception("AI review failed; posting notification without summary")
        return None


def _run_claude(prompt: str, settings: Settings) -> str | None:
    """Invoke the Claude Code CLI headlessly and return its raw JSON stdout, or None.

    Runs synchronously; call it via ``asyncio.to_thread`` from async code.
    """

    claude_bin = settings.claude_bin or shutil.which("claude")
    if not claude_bin:
        logger.warning("AI review skipped: `claude` CLI not found on PATH")
        return None

    # Run in a throwaway directory so the CLI does not load this repo's CLAUDE.md
    # or wander project files instead of the MR content we hand it over stdin.
    workdir = tempfile.mkdtemp(prefix="mr-review-")
    cmd = [
        claude_bin,
        "-p",
        "--safe-mode",
        "--tools",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--model",
        settings.ai_model,
        "--effort",
        settings.ai_effort,
        "--max-budget-usd",
        str(settings.ai_max_budget_usd),
        "--system-prompt",
        SYSTEM_PROMPT,
        "--json-schema",
        REVIEW_JSON_SCHEMA,
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=workdir,
            timeout=settings.ai_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        logger.warning("AI review timed out after %ss", settings.ai_timeout_seconds)
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "no error output")[:1000]
        logger.error("claude CLI exited %s: %s", proc.returncode, details)
        return None
    return proc.stdout


def _parse_review(raw: str) -> MRReview | None:
    """Parse the CLI's JSON envelope, extract the model result, and validate it."""

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("AI review: claude CLI did not return JSON")
        return None
    if not isinstance(envelope, dict):
        logger.warning("AI review: claude CLI did not return a JSON object")
        return None
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        logger.warning("AI review: claude CLI reported an error (%s)", envelope.get("subtype"))
        return None
    structured_output = envelope.get("structured_output")
    payload = (
        structured_output
        if isinstance(structured_output, dict)
        else _extract_json_object(str(envelope.get("result", "")))
    )
    if payload is None:
        logger.warning("AI review: could not extract JSON from the model result")
        return None
    try:
        return MRReview.model_validate(payload)
    except ValidationError:
        logger.warning("AI review: model output did not match the expected schema")
        return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of the model's text, tolerating code fences."""

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_prompt(mr: dict[str, Any], context: dict[str, Any], max_chars: int) -> str:
    files: list[dict[str, Any]] = context.get("files") or []
    contents: dict[str, str] = context.get("contents") or {}
    files_truncated = bool(context.get("files_truncated"))

    header = (
        "# Merge Request 리뷰 요청\n"
        f"- 저장소: {mr.get('repository')}\n"
        f"- 번호: !{mr.get('iid')}\n"
        f"- 제목: {mr.get('title')}\n"
        f"- 작성자: {mr.get('author')}\n"
        f"- 브랜치: {mr.get('head_ref')} → {mr.get('base_ref')}\n"
        f"- 변경 파일 수: {len(files)}\n\n"
        "아래 변경 diff와 파일 내용을 분석해 요약해 주세요.\n"
    )

    minimum = len(header) + len(_JSON_INSTRUCTION)
    if max_chars < minimum:
        logger.warning(
            "AI_MAX_INPUT_CHARS=%d is too small; using the %d-character minimum",
            max_chars,
            minimum,
        )
        max_chars = minimum

    parts = [header]
    budget = max_chars - minimum

    inventory = ["\n## 변경 파일 목록\n"]
    for entry in files:
        inventory.append(
            f"- {entry.get('filename')} ({entry.get('status')}, "
            f"+{entry.get('additions', 0)} -{entry.get('deletions', 0)})\n"
        )
    if files_truncated:
        inventory.append("- … (파일 조회 상한을 초과한 나머지 변경 파일은 생략)\n")
    inventory_text = "".join(inventory)
    if len(inventory_text) <= budget:
        parts.append(inventory_text)
        budget -= len(inventory_text)

    # 1) diff (patch) — 우선순위 높음
    diff_parts = ["\n## 변경 diff\n"]
    omitted_diffs: list[str] = []
    notice_budget = min(2000, max(200, budget // 10))
    diff_budget = max(0, budget - len(diff_parts[0]) - notice_budget)
    for entry in files:
        name = str(entry.get("filename"))
        status = entry.get("status")
        patch = entry.get("patch")
        added = entry.get("additions", 0)
        removed = entry.get("deletions", 0)
        block = f"\n### {name} ({status}, +{added} -{removed})\n"
        block += f"```diff\n{patch}\n```\n" if patch else "(diff 없음 — 대용량/바이너리)\n"
        if len(block) <= diff_budget:
            diff_parts.append(block)
            diff_budget -= len(block)
        else:
            omitted_diffs.append(name)

    if omitted_diffs:
        notice = _omission_notice("입력 예산으로 diff 생략", omitted_diffs, notice_budget)
        diff_parts.append(notice)
        logger.info("AI review: omitted diff for files: %s", ", ".join(omitted_diffs))

    diff_text = "".join(diff_parts)
    parts.append(diff_text)
    budget -= len(diff_text)

    # 2) 변경 파일 전체 내용 — 예산이 남는 만큼만, 초과분은 명시적으로 로그
    if budget > 500 and contents:
        parts.append("\n## 변경 파일 전체 내용\n")
        budget -= 40
        skipped: list[str] = []
        for name, text in contents.items():
            block = f"\n### {name}\n```\n{text}\n```\n"
            if len(block) > budget:
                skipped.append(name)
                continue
            parts.append(block)
            budget -= len(block)
        if skipped:
            notice = _omission_notice("전체 내용 생략", skipped, budget)
            if len(notice) <= budget:
                parts.append(notice)
                budget -= len(notice)
            logger.info(
                "AI review: skipped full content for files: %s",
                ", ".join(skipped),
            )

    parts.append(_JSON_INSTRUCTION)
    return "".join(parts)


def _omission_notice(label: str, names: list[str], max_chars: int) -> str:
    """Build a bounded, explicit notice for content excluded from the prompt."""

    prefix = f"\n({label}: "
    suffix = ")\n"
    if max_chars <= len(prefix) + len(suffix):
        return ""

    included: list[str] = []
    for index, name in enumerate(names):
        remaining = len(names) - index - 1
        tail = f", 외 {remaining}개" if remaining else ""
        candidate = prefix + ", ".join([*included, name]) + tail + suffix
        if len(candidate) > max_chars:
            break
        included.append(name)

    if included:
        remaining = len(names) - len(included)
        tail = f", 외 {remaining}개" if remaining else ""
        return prefix + ", ".join(included) + tail + suffix
    return (prefix + f"{len(names)}개 파일" + suffix)[:max_chars]


# Repeated at the very end of the user turn — the last thing the model reads — because
# the CLI's default system prompt otherwise pulls the answer toward conversational Markdown.
_JSON_INSTRUCTION = (
    "\n\n---\n"
    "위 diff와 파일 내용만 근거로 분석한 결과를, "
    "아래 스키마에 맞는 **JSON 객체 하나만** 출력하세요. "
    "코드펜스(```), 머리말, 마무리 문장을 절대 붙이지 마세요. "
    "출력의 첫 글자는 `{`, 마지막 글자는 `}` 여야 합니다.\n"
    '스키마: {"summary": "무엇을 하는 MR인지 한 줄 요약(string)", '
    '"key_changes": ["주요 변경점 string 3~6개"], '
    '"points_to_watch": ["리뷰어가 살펴볼 지점 string, 없으면 빈 배열"]}'
)
