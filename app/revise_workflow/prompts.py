"""The three LLM stages' prompts — system role, HARD LIMITS, and user body.

Ground truth: docs/revise-workflow.html Part 1 (에이전트) and Part 3 (템플릿).

Two rules shape every prompt here, both taken from mrdoc's measured failures
(docs/mrdoc-pipeline.html):

**템플릿은 렌더러가.** No output template is typed by hand. Each one comes from
``app.revise_workflow.schema``'s own renderer (``intent_template`` /
``edit_template`` / ``verify_template``), so the shape a stage is told to
produce and the shape its parser accepts are the same string. In the measured
mrdoc run a hand-written template told a satellite to write list-objects as
block sequences while the parser only accepted inline flow: the artifact was
discarded whole and the pipeline still reported complete.

**검색은 기계가.** No stage is given a search tool. 3a sees a table of
contents, never the documents; 3b sees the exact ±20-line windows the anchor
node resolved; 3c sees the diff. A stage that could grep would start inventing
search terms, and an invented search term has nothing to compare against.

The stages return the artifact text on stdout. ``runner`` parses it with the
matching ``parse_*`` and writes the canonical re-render to disk — the parse
*is* the validation, so a stage that answers with prose instead of the
template fails the node rather than poisoning the next one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .schema import (
    Anchor,
    Impact,
    Intent,
    edit_template,
    intent_template,
    verify_template,
)

_RULE = "\n\n---\n\n"


# ---------------------------------------------------------------------------
# 3a INTENT — 사람의 의견을 구조화한다. 문서 본문은 보지 않는다.
# ---------------------------------------------------------------------------
INTENT_SYSTEM = (
    "당신은 revise-intent 위성이다. 사람이 슬랙에 쓴 리뷰 의견을 기계가 쓸 수 있는 "
    "스펙으로 옮긴다 — 문서를 고치지 않고, 무엇을 고칠지 판단하지도 않는다.\n"
    "\n"
    "[HARD LIMITS]\n"
    "1. 목차에 없는 파일을 지목하지 않는다. 목차가 이 작업에서 당신이 아는 "
    "문서의 전부다. 어느 파일인지 정할 수 없으면 대상문서를 비우고 "
    "search_terms 로만 남긴다 — 추측한 경로는 존재하지 않는 앵커를 만든다.\n"
    "2. search_terms 는 원문에 그대로 있을 문자열만 쓴다. 당신이 지어낸 요약문이나 "
    "일반 명사를 넣지 않는다. 이 문자열이 기계가 대상을 찾는 유일한 단서이고, "
    "찾지 못하면 그 의견은 사람에게 되돌아간다.\n"
    "3. 의견에 없는 개선을 만들지 않는다. 수정방향은 사람이 요청한 것만 옮긴다 — "
    "「겸사겸사」가 붙는 순간 검증할 근거가 사라진다.\n"
    "4. 치환 요청은 「A → B」 형태로 정확히 적는다. 기계가 이 형태만 리터럴 "
    "검증으로 내린다. A 와 B 는 원문에 나타날 문자열 그대로 쓴다.\n"
    "5. 범위는 local(문서 하나 안) 또는 cross-doc(문서 경계를 넘음) 둘 중 하나다. "
    "애매하면 local 로 둔다 — 동반 변경은 기계가 따로 찾는다.\n"
    "6. 같은 것을 말하는 의견이 여럿이면 통합에 그 opinion_id 들을 적는다. "
    "서로 모순되면 충돌해소에 어느 쪽을 택했는지와 이유를 한 줄로 쓴다.\n"
    "7. 템플릿에 없는 필드나 섹션을 만들지 않는다."
)

_INTENT_TASK = (
    "[할 일] 아래 의견 각각을 스펙 한 블록으로 옮긴다. 의견 수와 OPINION 블록 수가 "
    "같아야 한다.\n"
    "[입력] 변경 파일 목록과 문서 목차뿐이다. 문서 본문은 주어지지 않았고 "
    "요청해서도 안 된다.\n"
    "[출력] 아래 템플릿 그대로의 마크다운 본문만 출력한다. 코드펜스로 감싸지 말고, "
    "설명 문장을 앞뒤에 붙이지 않는다."
)


def intent_prompt(
    *,
    mr_iid: int,
    project_id: str,
    round_number: int,
    base_sha: str,
    repo_slug: str,
    opinions: Sequence[Mapping[str, object]],
    changed_files: Sequence[str],
    toc: Mapping[str, Sequence[str]],
) -> str:
    """3a's user prompt: opinions + changed files + headings. No document bodies.

    ``toc`` maps a posix document path to its heading paths, which is the
    whole of what this stage learns about the repository.
    """

    lines = [
        _INTENT_TASK,
        "",
        f"[맥락] 저장소 {repo_slug} · MR !{mr_iid} · project {project_id} · "
        f"라운드 {round_number} · base {base_sha}",
        "",
        "[의견 목록]",
    ]
    for opinion in opinions:
        lines.append(f"\n### opinion_id={opinion.get('id')}")
        lines.append(f"본문: {opinion.get('body')}")
        refs = opinion.get("question_refs")
        if refs:
            lines.append(f"question_refs: {refs}")

    lines.append("\n[이번 MR 이 바꾼 파일]")
    if changed_files:
        lines.extend(f"- {path}" for path in changed_files)
    else:
        lines.append("- (없음)")

    lines.append("\n[문서 목차] — 당신이 아는 문서의 전부다")
    for path in sorted(toc):
        lines.append(f"\n{path}")
        headings = toc[path]
        if not headings:
            lines.append("  (헤딩 없음)")
        lines.extend(f"  · {heading}" for heading in headings)

    lines.append("\n[산출 형식] 정확히 이 템플릿을 따른다:")
    lines.append(
        intent_template(
            mr_iid=mr_iid, project_id=project_id, round=round_number, base_sha=base_sha
        )
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3b EDIT — 후보를 고르고 파일을 고친다. 지목된 파일 밖은 건드리지 않는다.
# ---------------------------------------------------------------------------
EDIT_SYSTEM = (
    "당신은 revise-edit 위성이다. 확정된 앵커와 동반 후보를 받아 문서를 실제로 "
    "고친다 — 무엇이 대상인지 다시 찾지 않는다.\n"
    "\n"
    "[HARD LIMITS]\n"
    "1. 지목된 파일 밖을 수정하지 않는다. 허용 목록은 앵커 파일과 동반 후보 파일뿐이고, "
    "그 밖의 수정은 게이트가 기계로 되돌린다 — 되돌려진 편집은 없던 일이 되고 "
    "라운드 예산만 쓴다.\n"
    "2. 파일을 통독하지 않는다. 주어진 라인 창(±20줄)이 당신이 읽을 범위다. "
    "창 밖이 필요하면 고치지 말고 그 의견을 unapplied 로 남긴다.\n"
    "3. 동반 후보는 근거가 붙어 있다(같은 리터럴 값 · 들어오는 링크). "
    "근거를 확인하고 채택 여부를 선택한다. 기각도 정당한 선택이고 "
    "그때는 decision: skip 과 이유를 남긴다.\n"
    "4. 스펙에 없는 개선을 하지 않는다. 오탈자가 눈에 띄어도 의견이 요청하지 않았으면 "
    "고치지 않는다 — 그 편집은 어느 의견으로도 추적되지 않아 검증에서 탈락한다.\n"
    "5. 못 한 의견은 반드시 사유와 함께 남긴다. 침묵은 누락으로 처리된다 — "
    "processed 에도 unprocessed 에도 없는 opinion_id 는 기계가 미반영으로 강등한다.\n"
    "6. 치환은 요청받은 문자열 그대로 한다. 「3.2 → 4.0」이면 4.0 이지 4.0.0 이 아니다. "
    "기계가 이 치환을 원문 대조로 검증한다.\n"
    "7. edits 에는 실제로 고친 위치만 적는다. 고치지 않고 적으면 거짓 영수증이고, "
    "검증 단계가 diff 로 잡아낸다."
)

_EDIT_TASK = (
    "[할 일] 각 의견의 앵커를 스펙의 수정방향대로 고치고, 동반 후보를 채택할지 "
    "정해 함께 고친다. 그 다음 무엇을 했는지 영수증으로 적는다.\n"
    "[도구] 파일 편집이 허용돼 있다. 검색 도구는 없다 — 위치는 이미 확정돼 있다.\n"
    "[출력] 편집을 마친 뒤, 아래 템플릿 그대로의 마크다운 본문만 출력한다. "
    "코드펜스로 감싸지 말고, 설명 문장을 앞뒤에 붙이지 않는다."
)


def edit_prompt(
    *,
    intent: Intent,
    anchor: Anchor,
    impact: Impact,
    windows: Mapping[str, str],
    allowed_files: Sequence[str],
    workspace_posix: str,
    retry_note: str = "",
) -> str:
    """3b's user prompt: the spec, the resolved anchors, the ±20-line windows.

    ``windows`` maps ``"<path>:<start>-<end>"`` to that slice's text — the
    only document content this stage sees. ``retry_note`` carries the gate's
    or 3c's evidence when the orchestrator re-injects this stage (구현 누락).
    """

    lines = [_EDIT_TASK]
    if retry_note:
        lines += ["", "[재투입 — 아래 지적을 해소하는 것이 이번 호출의 목적이다]", retry_note]

    lines += [
        "",
        f"[작업 폴더] {workspace_posix}",
        "",
        "[허용 파일] 이 목록 밖을 고치면 게이트가 되돌린다",
    ]
    if allowed_files:
        lines.extend(f"- {path}" for path in allowed_files)
    else:
        lines.append("- (없음)")

    lines += ["", "[스펙] 00-intent.md", _fence(_render_intent_digest(intent))]
    lines += ["", "[앵커] 10-anchor.md — 확정된 위치", _fence(_render_anchor_digest(anchor))]
    lines += [
        "",
        "[동반 후보] 20-impact.md — 근거가 붙어 있다",
        _fence(_render_impact_digest(impact)),
    ]

    lines += ["", "[라인 창] 당신이 읽을 범위의 전부다"]
    for key in sorted(windows):
        lines += [f"\n--- {key}", windows[key]]

    lines += [
        "",
        "[산출 형식] 정확히 이 템플릿을 따른다:",
        edit_template(mr_iid=intent.mr_iid, round=intent.round),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3c VERIFY — 의견 원문과 diff 를 대조한다. 편집자의 영수증은 보지 않는다.
# ---------------------------------------------------------------------------
VERIFY_SYSTEM = (
    "당신은 revise-verify 위성이다. 사람의 의견이 실제로 반영됐는지 원문과 "
    "변경분을 대조해 판정한다 — 고치지 않는다.\n"
    "\n"
    "[HARD LIMITS]\n"
    "1. 판정 축은 의견 원문이다. 스펙(3a 산출)은 참고용이고, 스펙이 원문을 잘못 "
    "옮겼으면 그것도 미반영이다 — 스펙에 맞았다고 통과시키지 않는다.\n"
    "2. 편집자의 영수증은 주어지지 않았다. 「했다」는 주장이지 증거가 아니고, "
    "당신에게 주어진 증거는 diff 뿐이다.\n"
    "3. 애매하면 partial 이나 unapplied 로 둔다. 확신 없는 applied 는 거짓 영수증이고, "
    "부당한 통과 하나가 이 계층을 없앤다.\n"
    "4. 판정마다 근거를 파일·라인·before/after 로 지목한다. 지목할 diff 가 없으면 "
    "그 의견은 applied 가 아니다.\n"
    "5. 문서 내용의 옳고 그름을 보지 않는다. 의견과 변경분의 대응만 본다 — "
    "더 나은 문장이 떠올라도 쓰지 않는다.\n"
    "6. 지적 · 권고 · 심각도를 쓰지 않는다. 스키마에 그 필드가 없다."
)

_VERIFY_TASK = (
    "[할 일] 의견 하나마다 판정 한 블록을 쓴다. 의견 수와 VERDICT 블록 수가 같아야 한다.\n"
    "[도구] 없다. 아래 diff 가 증거의 전부이고, 그 밖을 볼 방법은 주어지지 않았다.\n"
    "[출력] 아래 템플릿 그대로의 마크다운 본문만 출력한다. 코드펜스로 감싸지 말고, "
    "설명 문장을 앞뒤에 붙이지 않는다."
)


def verify_prompt(
    *,
    intent: Intent,
    opinions: Sequence[Mapping[str, object]],
    diff_text: str,
) -> str:
    """3c's user prompt: opinion originals + spec + diff. Never the 3b receipt.

    No tool is granted and no workspace path is named. The stage's second hard
    limit already says the diff is the whole of its evidence, so a `Read`
    grant bought nothing -- and it was expensive: measured against the same
    input, 3c cost $1.2174 over 2 turns with `Read` and $0.0287 over 1 turn
    without it, reaching the identical verdict. A tool a stage must not rely
    on is a tool it should not be handed.
    """

    lines = [_VERIFY_TASK, "", "[의견 원문] — 판정의 기준"]
    for opinion in opinions:
        lines.append(f"\n### opinion_id={opinion.get('id')}")
        lines.append(f"본문: {opinion.get('body')}")

    lines += ["", "[스펙] 00-intent.md — 참고용. 원문과 다르면 원문이 이긴다",
              _fence(_render_intent_digest(intent))]
    lines += [
        "",
        "[변경분] git diff — 당신에게 주어진 증거의 전부다",
        _fence(diff_text or "(변경 없음)"),
    ]
    lines += [
        "",
        "[산출 형식] 정확히 이 템플릿을 따른다:",
        verify_template(mr_iid=intent.mr_iid, round=intent.round),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# digests — 산출물을 프롬프트에 실을 때의 축약 (본문 전체를 싣지 않는다)
# ---------------------------------------------------------------------------
def _fence(body: str) -> str:
    return "```\n" + body.rstrip("\n") + "\n```"


def _render_intent_digest(intent: Intent) -> str:
    lines: list[str] = []
    for opinion in intent.opinions:
        lines.append(f"opinion_id={opinion.opinion_id} 범위={opinion.범위}")
        lines.append(f"  대상문서: {', '.join(opinion.대상문서) or '(미정)'}")
        lines.append(f"  대상주장: {opinion.대상주장}")
        lines.append(f"  수정방향: {opinion.수정방향}")
        if opinion.근거:
            lines.append(f"  근거: {opinion.근거}")
        if opinion.충돌해소:
            lines.append(f"  충돌해소: {opinion.충돌해소}")
    return "\n".join(lines)


def _render_anchor_digest(anchor: Anchor) -> str:
    lines: list[str] = []
    for entry in anchor.entries:
        if entry.status != "FOUND":
            lines.append(f"opinion_id={entry.opinion_id} MISSING — 이번 호출의 대상이 아니다")
            continue
        window = entry.window
        where = f"{window.file}:{window.start}-{window.end}" if window else "(창 없음)"
        lines.append(f"opinion_id={entry.opinion_id} unit={entry.unit_id} 창={where}")
        for hit in entry.hits:
            lines.append(f"  hit {hit.file}:{hit.line} — {hit.term!r}")
    return "\n".join(lines)


def _render_impact_digest(impact: Impact) -> str:
    lines: list[str] = []
    for entry in impact.entries:
        if not entry.candidates:
            lines.append(f"opinion_id={entry.opinion_id} 동반 후보 없음")
            continue
        lines.append(f"opinion_id={entry.opinion_id}")
        for candidate in entry.candidates:
            lines.append(
                f"  {candidate.file}:{candidate.line} unit={candidate.unit_id} "
                f"via={candidate.via} 근거={candidate.match!r}"
            )
    return "\n".join(lines)


def toc_of(
    tree: Mapping[str, str], heading_paths: Mapping[str, Iterable[str]]
) -> dict[str, list[str]]:
    """Narrow a heading index to the documents present in ``tree``."""

    return {path: list(heading_paths.get(path, ())) for path in sorted(tree)}
