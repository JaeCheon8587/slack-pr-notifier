"""Slack interaction dispatch — HTTP route + Socket Mode (next phase) shared
entry point. v4.1/P4a design (docs/mr-review-pipeline.html §②/§S3/§S4①),
extracted from ``app.slack_actions`` so the exact same approval/opinion-rail
logic can be driven by either the inbound HTTP webhook route or an outbound
Socket Mode connection (Slack -> this host inbound HTTP is blocked by network
policy in some deployments — see ``app.ingest``'s identical P.2 rationale for
the GitLab side). The Socket Mode client itself is a follow-up phase; this
module only provides the shared ``dispatch_interaction`` entry point plus the
moved rail implementations it dispatches to.

``dispatch_interaction`` takes an already-parsed Slack interaction payload
dict and returns the same response-body shape the HTTP route has always
returned (a result dict, or ``{}`` for ``view_submission`` per Slack's
contract). Handles both interaction shapes:
  - ``block_actions``: the [승인] button's full 0-5 step flow (authorization
    -> CAS lock -> GitLab merge (sha-bound) -> background poll ->
    merged/manual), and the [의견] button (authorization -> signed-token
    verification -> ``views.open`` modal).
  - ``view_submission``: the [의견] modal's submit — opinion INSERT (+ GitLab
    MR comment mirror) -> guard (a)(b) -> CAS lock (reviewing -> revising) ->
    revise executor kickoff (``app.revise_executor.enqueue_revise`` — P4b
    interface stub, body out of scope here) -> Slack update.

GitLab "approve" is not called anywhere in this module (v4.1 removed the
approve step) — the merge call itself is the only GitLab-side action — the
Slack click + review_session row + MR note together are the approval
record (§① / §② step 4).

All ``review_session.status`` changes go through
``app.state_machine.cas_transition`` — this module never writes the
``status`` column directly.

Dependency layering (kept import-cycle free, matching ``app.ingest``'s
note): this module depends only on ``app.config``, ``app.db``-shaped
connections, ``app.state_machine``, ``app.slack_client``,
``app.gitlab_client``, ``app.action_token``, and ``app.revise_executor``. It
must never import ``app.slack_actions`` (that module imports this one).

``background_tasks``: the [승인] rail's merge-status poll (step (e) below) is
deferred past the HTTP response via FastAPI's ``BackgroundTasks`` — Slack's
3s interactivity ack budget cannot cover the worst-case 3s x 10 = 30s poll.
Starlette runs ``BackgroundTasks`` sequentially, awaited inline right after
the response bytes are sent — still within the same coroutine, not a
separately scheduled ``asyncio.Task`` — which is what lets the existing
in-process ASGI tests observe the final DB state synchronously right after
the HTTP call returns. ``dispatch_interaction`` therefore accepts the
caller's ``BackgroundTasks`` instance as an optional keyword parameter
(``None`` when not applicable — e.g. a future Socket Mode caller with no HTTP
response to attach it to; that transport's own scheduling mechanism is
next-phase work) purely to relay it unchanged into the moved
``_handle_approve_click``; no other code path here uses it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from typing import Any

import httpx
from fastapi import BackgroundTasks, HTTPException

from app.action_token import InvalidActionToken, decode_action_token
from app.config import Settings, secret_value
from app.db import get_connection, init_db
from app.gitlab_client import GitLabClient, build_review_body
from app.revise_executor import enqueue_revise
from app.slack_client import SlackClient
from app.state_machine import MANUAL, MERGED, MERGING, REVIEWING, REVISING, cas_transition

ROUND_CAP = 3

logger = logging.getLogger("uvicorn.error")

POLL_INTERVAL_SECONDS = 3
POLL_MAX_ATTEMPTS = 10


# ---------------------------------------------------------------------------
# Shared dispatch entry point (HTTP route + Socket Mode, next phase)
# ---------------------------------------------------------------------------
async def dispatch_interaction(
    settings: Settings,
    payload: dict[str, Any],
    *,
    source: str = "http",
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    """Handle an already-parsed Slack interaction payload.

    ``payload`` is the decoded interaction dict (``block_actions`` or
    ``view_submission`` — any other ``type`` is accepted-and-ignored).
    ``source`` identifies the calling transport ("http" | "socket") for
    observability only — it never changes control flow or any transition
    reason/detail string. ``background_tasks`` is threaded straight through
    to the [승인] rail's merge-poll scheduling (see the module docstring).
    """

    payload_type = payload.get("type")
    if payload_type not in {"block_actions", "view_submission"}:
        return {"accepted": True, "ignored": True}

    try:
        if payload_type == "view_submission":
            # Slack's view_submission contract (F4): a success response must
            # be either an empty body or a valid `response_action` payload —
            # never arbitrary JSON. This handler has no inline field errors
            # to surface, so the modal is simply closed by returning `{}`
            # (an empty JSON object, not a literally empty HTTP body) and any
            # user-facing outcome goes out via chat.postEphemeral or a main
            # message update instead (see _handle_opinion_submission).
            await _handle_view_submission(payload, settings)
            return {}
        return await _handle_block_action(payload, settings, background_tasks)
    except InvalidActionToken as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPError as error:
        logger.exception("External API request failed")
        raise HTTPException(status_code=502, detail="GitLab or Slack API request failed") from error
    except RuntimeError as error:
        logger.exception("Slack API request failed")
        raise HTTPException(status_code=502, detail=str(error)) from error


# ---------------------------------------------------------------------------
# Block action dispatch
# ---------------------------------------------------------------------------
async def _handle_block_action(
    payload: dict[str, Any], settings: Settings, background_tasks: BackgroundTasks | None
) -> dict[str, Any]:
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
        raise HTTPException(status_code=400, detail="Slack action is missing")
    action = actions[0]
    token = action.get("value")
    if not isinstance(token, str):
        raise HTTPException(status_code=400, detail="Slack action token is missing")

    user_id = _slack_user_id(payload)
    response_url = payload.get("response_url")
    response_url = response_url if isinstance(response_url, str) else None
    action_id = action.get("action_id")

    # Diagnostic line for REVIEWER_MAP setup -- action_id + the clicking
    # Slack user id only; never the button's signed token value or any secret.
    logger.info("Slack action received: action_id=%s slack_user_id=%s", action_id, user_id)

    if action_id == "request_changes_mr":
        trigger_id = payload.get("trigger_id")
        trigger_id = trigger_id if isinstance(trigger_id, str) else None
        return await _handle_opinion_click(token, user_id, trigger_id, response_url, settings)

    if action_id != "approve_mr":
        return {"accepted": True, "ignored": True}

    mr = _decode_mr(token, settings)
    channel, message_ts = _message_location(payload)
    return await _handle_approve_click(
        mr, user_id, response_url, channel, message_ts, settings, background_tasks
    )


# ---------------------------------------------------------------------------
# [의견] button click — §S3 step 1: authorization -> views.open modal
# ---------------------------------------------------------------------------
async def _handle_opinion_click(
    token: str,
    user_id: str,
    trigger_id: str | None,
    response_url: str | None,
    settings: Settings,
) -> dict[str, Any]:
    # Signed token verified (signature + expiry) by decode_action_token inside
    # _decode_mr — this is the "버튼 value의 서명 토큰 검증(session_id·sha 바인딩)"
    # step; project_id+iid resolve the session, sha is carried through as the
    # modal's private_metadata. The submission handler does not re-verify this
    # sha directly — guard (b) instead re-checks for a human commit since it
    # via GitLab's commits API (see _handle_opinion_submission).
    mr = _decode_mr(token, settings)

    conn = _get_conn(settings)
    try:
        session = _find_session(conn, mr["project_id"], mr["iid"])
        if session is None:
            await _send_ephemeral(response_url, "세션을 찾을 수 없습니다 — 이미 종료되었을 수 있습니다.")
            return {"accepted": True, "ignored": True, "reason": "no_session"}

        # Same authorization set as the [승인] button (step-0, §② / §S3).
        reviewers = settings.reviewers_for(session["repo_slug"])
        if user_id not in reviewers:
            _log_event(conn, session["id"], "guard_reject", f"unauthorized opinion click by {user_id}")
            await _send_ephemeral(response_url, "이 MR에 의견을 남길 권한이 없습니다.")
            return {"accepted": True, "action": "unauthorized"}
    finally:
        conn.close()

    if not trigger_id:
        raise HTTPException(status_code=400, detail="Slack trigger_id is missing")
    if not settings.slack_bot_token:
        raise HTTPException(status_code=503, detail="SLACK_BOT_TOKEN is not configured")

    client = SlackClient(secret_value(settings.slack_bot_token))
    await client.open_opinion_modal(trigger_id, token)
    return {"accepted": True, "action": "opinion_modal_opened"}


# ---------------------------------------------------------------------------
# [의견] modal submit — §S3/§S4① step (a)-(e)
# ---------------------------------------------------------------------------
async def _handle_view_submission(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    view = payload.get("view")
    if not isinstance(view, dict) or view.get("callback_id") != "opinion_submission":
        return {"accepted": True, "ignored": True}

    token = view.get("private_metadata")
    if not isinstance(token, str):
        raise HTTPException(status_code=400, detail="Slack view metadata is missing")
    mr = _decode_mr(token, settings)
    user_id = _slack_user_id(payload)

    body = _view_state_value(view, "opinion_block", "opinion_body")
    if not isinstance(body, str) or not body.strip():
        raise HTTPException(status_code=400, detail="Opinion body is missing")

    # 확인질문 생성 미구현으로 입력 제거 — 컬럼은 향후 복원 대비 유지
    return await _handle_opinion_submission(mr, user_id, body, None, settings)


async def _handle_opinion_submission(
    mr: dict[str, Any],
    user_id: str,
    body: str,
    question_refs: str | None,
    settings: Settings,
) -> dict[str, Any]:
    conn = _get_conn(settings)
    try:
        session = _find_session(conn, mr["project_id"], mr["iid"])
        if session is None:
            return {"accepted": True, "ignored": True, "reason": "no_session"}

        channel = session["slack_channel"]
        message_ts = session["slack_ts"]
        session_id = session["id"]
        current_round = session["round"]
        last_notified_sha = session["mr_sha"]

        # Re-check authorization at submit time (F3) — the same reviewer set
        # as the [의견] button click and the [승인] button (fail-closed: an
        # unmapped/empty reviewer set rejects). The modal can stay open for a
        # while, so re-validating here (rather than trusting the click-time
        # check alone) prevents a stale/forwarded modal from submitting on
        # behalf of a non-reviewer.
        reviewers = settings.reviewers_for(session["repo_slug"])
        if user_id not in reviewers:
            _log_event(conn, session_id, "guard_reject", f"unauthorized opinion submit by {user_id}")
            await _post_ephemeral(settings, channel, user_id, "이 MR에 의견을 남길 권한이 없습니다.")
            return {"accepted": True, "action": "unauthorized"}

        # Step (a): opinion INSERT. UNIQUE(session_id, slack_user, body_hash)
        # absorbs a double-submit (e.g. a retried modal submission) —
        # normalized so incidental whitespace differences do not defeat dedup.
        normalized_body = _normalize_opinion_body(body)
        body_hash = hashlib.sha256(normalized_body.encode("utf-8")).hexdigest()
        try:
            conn.execute(
                "INSERT INTO opinion (session_id, slack_user, question_refs, body, body_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, question_refs, body, body_hash),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            await _post_ephemeral(settings, channel, user_id, "이미 접수된 의견입니다.")
            return {"accepted": True, "action": "duplicate_opinion"}
    finally:
        conn.close()

    # GitLab MR comment mirror (이중 기록) — reuses create_merge_request_note /
    # build_review_body exactly as the approve rail's audit-note pattern;
    # best-effort, a failure here must not block the guard/CAS flow below.
    if settings.gitlab_token:
        try:
            gitlab = GitLabClient(
                settings.gitlab_url, secret_value(settings.gitlab_token), verify_ssl=settings.gitlab_verify_ssl
            )
            note = build_review_body(
                approved=False, slack_user_id=user_id, commit_sha=last_notified_sha or "", reason=body
            )
            await gitlab.create_merge_request_note(mr["project_id"], mr["iid"], note)
        except httpx.HTTPError:
            logger.warning("Opinion submission recorded, but the GitLab MR note could not be added")

    # Step (b): guard (a) — round cap (§S4① / 안전망 ③).
    if current_round >= ROUND_CAP:
        return await _reject_to_manual(
            settings,
            session_id,
            channel,
            message_ts,
            mr,
            detail=f"round>={ROUND_CAP}",
            header="⚠️ 자동개선 한도 도달 — 수동 확인 필요",
        )

    # Step (b): guard (b) — human commit since the last notified sha
    # (GitLab commits API, 1회 조회, 봇 계정 author 제외). Fails closed: if the
    # token is missing this is a hard 503 (same as the approve rail), and if
    # the commits lookup itself fails, no state transition is attempted here
    # — the opinion row already exists with applied_round NULL, so it is
    # picked up automatically by the next round's unapplied-opinion query
    # regardless of what happens with this guard check.
    if not settings.gitlab_token:
        raise HTTPException(status_code=503, detail="GITLAB_TOKEN is not configured")
    gitlab = GitLabClient(
        settings.gitlab_url, secret_value(settings.gitlab_token), verify_ssl=settings.gitlab_verify_ssl
    )
    try:
        commits = await gitlab.list_mr_commits(mr["project_id"], mr["iid"])
    except httpx.HTTPError:
        logger.warning(
            "Guard(b) commits lookup failed for session=%s; failing closed with no state transition",
            session_id,
        )
        await _post_ephemeral(
            settings,
            channel,
            user_id,
            "사람 커밋 확인 실패 — 잠시 후 다시 시도해 주세요. "
            "(의견은 이미 접수되었고 다음 라운드에 자동 반영됩니다.)",
        )
        return {"accepted": True, "action": "guard_b_check_failed"}
    if _human_commit_since(commits, last_notified_sha, settings):
        return await _reject_to_manual(
            settings,
            session_id,
            channel,
            message_ts,
            mr,
            detail="human commit detected since last notified sha",
            header="👤 수동 편집 감지 — 수동 확인 필요",
        )

    # Step (c): CAS lock reviewing -> revising. rowcount=0 means the session
    # already left `reviewing` (concurrent approve, or another opinion
    # submission already won the race) — the opinion is already INSERTed
    # above, so it is picked up automatically by the next round's
    # unapplied-opinion query (app.db.unapplied_opinions); no resubmission
    # needed.
    conn = _get_conn(settings)
    try:
        accepted = cas_transition(conn, session_id, REVIEWING, REVISING, reason="opinion")
    finally:
        conn.close()

    if not accepted:
        await _post_ephemeral(
            settings,
            channel,
            user_id,
            "이미 개선 작업이 진행 중입니다 — 의견은 접수되었고 다음 라운드에 자동 반영됩니다.",
        )
        return {"accepted": True, "action": "already_revising"}

    # Step (d): revise executor kickoff (P4b interface stub — body out of
    # scope for this phase; app.revise_executor.enqueue_revise).
    await enqueue_revise(session_id)

    # Step (e): Slack main message update — buttons removed, revise started.
    await _notify_revising(settings, channel, message_ts, mr)
    return {"accepted": True, "action": "revise_started"}


async def _reject_to_manual(
    settings: Settings,
    session_id: int,
    channel: str | None,
    message_ts: str | None,
    mr: dict[str, Any],
    *,
    detail: str,
    header: str,
) -> dict[str, Any]:
    conn = _get_conn(settings)
    try:
        accepted = cas_transition(conn, session_id, REVIEWING, MANUAL, reason="guard_reject", detail=detail)
    finally:
        conn.close()

    if accepted:
        await _notify_manual(settings, channel, message_ts, mr, header)
        return {"accepted": True, "action": "guard_rejected", "detail": detail}
    # Lost the CAS race (session already left `reviewing`) — the opinion row
    # itself is unaffected; nothing further to do here.
    return {"accepted": True, "action": "guard_reject_raced"}


async def _notify_manual(
    settings: Settings, channel: str | None, message_ts: str | None, mr: dict[str, Any], reason: str
) -> None:
    if not settings.slack_bot_token or not channel or not message_ts:
        return
    try:
        summary = f"!{mr.get('iid')} {mr.get('title') or ''}".strip()
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.withdraw_buttons(channel, message_ts, summary, reason)
    except Exception:
        logger.exception("Slack guard-rejection message update failed")


async def _notify_revising(
    settings: Settings, channel: str | None, message_ts: str | None, mr: dict[str, Any]
) -> None:
    if not settings.slack_bot_token or not channel or not message_ts:
        return
    try:
        summary = f"!{mr.get('iid')} {mr.get('title') or ''}".strip()
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.update_revising(channel, message_ts, summary)
    except Exception:
        logger.exception("Slack revising message update failed")


async def _post_ephemeral(settings: Settings, channel: str | None, user_id: str, text: str) -> None:
    if not settings.slack_bot_token or not channel:
        return
    try:
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.post_ephemeral(channel, user_id, text)
    except Exception:
        logger.exception("Slack chat.postEphemeral failed")


def _normalize_opinion_body(body: str) -> str:
    """Collapse incidental whitespace differences before hashing for dedup."""

    return " ".join(body.split())


def _human_commit_since(commits: list[dict[str, Any]], since_sha: str | None, settings: Settings) -> bool:
    """True if any commit newer than ``since_sha`` was authored by a human.

    ``commits`` is assumed newest-first (GitLab's MR-commits endpoint default
    ordering). If ``since_sha`` cannot be located in the list (e.g. a history
    rewrite/squash), every commit is conservatively treated as "since" —
    guard (b) fails safe toward rejecting rather than silently allowing an
    automatic revise on top of an unaccounted-for commit.
    """

    for commit in commits:
        if since_sha is not None and commit.get("id") == since_sha:
            break
        if not _is_bot_commit(settings, commit):
            return True
    return False


def _is_bot_commit(settings: Settings, commit: dict[str, Any]) -> bool:
    author_name = commit.get("author_name")
    author_email = commit.get("author_email")
    if settings.bot_username and author_name == settings.bot_username:
        return True
    if settings.bot_email and author_email == settings.bot_email:
        return True
    return False


def _view_state_value(view: dict[str, Any], block_id: str, action_id: str) -> str | None:
    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    block = values.get(block_id) if isinstance(values, dict) else None
    field = block.get(action_id) if isinstance(block, dict) else None
    value = field.get("value") if isinstance(field, dict) else None
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Approve click — steps 0-5 of §② (a)-(d) synchronous, (e) poll backgrounded
# ---------------------------------------------------------------------------
async def _handle_approve_click(
    mr: dict[str, Any],
    user_id: str,
    response_url: str | None,
    channel: str,
    message_ts: str,
    settings: Settings,
    background_tasks: BackgroundTasks | None,
) -> dict[str, Any]:
    conn = _get_conn(settings)
    try:
        session = _find_session(conn, mr["project_id"], mr["iid"])
        if session is None:
            await _send_ephemeral(response_url, "세션을 찾을 수 없습니다 — 이미 종료되었을 수 있습니다.")
            return {"accepted": True, "ignored": True, "reason": "no_session"}

        # Step 0: authorization — click must come from the session's mapped
        # reviewer set. Empty/unmapped set = reject (fail-closed). No CAS is
        # attempted, so status is unchanged; the rejection is recorded
        # manually (CAS-based transitions log themselves).
        reviewers = settings.reviewers_for(session["repo_slug"])
        if user_id not in reviewers:
            _log_event(conn, session["id"], "guard_reject", f"unauthorized approve click by {user_id}")
            await _send_ephemeral(response_url, "이 MR을 승인/머지할 권한이 없습니다.")
            return {"accepted": True, "action": "unauthorized"}

        # Step (a): signed token was already verified (signature + expiry) by
        # decode_action_token inside _decode_mr; the token's sha is used below.

        # Step (b): re-fetch the current head sha from GitLab — the DB's
        # mr_sha is not trusted for this comparison (it can be stale).
        if not settings.gitlab_token:
            raise HTTPException(status_code=503, detail="GITLAB_TOKEN is not configured")
        gitlab = GitLabClient(
            settings.gitlab_url, secret_value(settings.gitlab_token), verify_ssl=settings.gitlab_verify_ssl
        )
        current_mr = await gitlab.get_merge_request(mr["project_id"], mr["iid"])
        current_sha = current_mr.get("sha")
        if current_sha != mr["sha"]:
            _log_event(
                conn, session["id"], "sha_stale", f"token_sha={mr['sha']} current_sha={current_sha}"
            )
            await _send_ephemeral(response_url, "새 커밋이 반영되었습니다 — 버튼을 다시 발급받아 주세요.")
            return {"accepted": True, "action": "sha_stale"}

        # Step (c): CAS lock reviewing -> merging. rowcount=0 absorbs
        # duplicate clicks (and any race against another accepted transition).
        accepted = cas_transition(conn, session["id"], REVIEWING, MERGING, reason="approve")
        if not accepted:
            _log_event(conn, session["id"], "duplicate_click", f"approve click by {user_id}")
            await _send_ephemeral(response_url, "이미 처리 중입니다 (중복 클릭).")
            return {"accepted": True, "action": "duplicate"}

        session_id = session["id"]
    finally:
        conn.close()

    # Step (d): merge API call, sha-bound — GitLab rejects the merge
    # server-side if a new commit landed after this sha was computed.
    try:
        await gitlab.merge_merge_request(mr["project_id"], mr["iid"], mr["sha"])
    except httpx.HTTPError as error:
        logger.warning("Merge call failed immediately: %s", error)
        _transition_and_log(settings, session_id, "merge_poll_failed", f"merge call failed: {error}")
        background_tasks.add_task(
            _notify_merge_manual, settings, channel, message_ts, mr, "⚠️ 머지 실패: GitLab 확인 필요"
        )
        return {"accepted": True, "action": "merge_failed"}

    # Audit trail mirror (best-effort — a failure here must not block the
    # merge/poll flow, per the existing note-mirroring pattern).
    try:
        note = build_review_body(approved=True, slack_user_id=user_id, commit_sha=mr["sha"])
        await gitlab.create_merge_request_note(mr["project_id"], mr["iid"], note)
    except httpx.HTTPError:
        logger.warning("MR was merged, but the Slack audit note could not be added")

    # Step (e): poll merge state (Slack's 3s ack budget cannot cover
    # 3s x 10 = 30s worst case) -> merged/manual, backgrounded.
    background_tasks.add_task(
        _poll_merge_and_finalize, settings, session_id, mr, user_id, channel, message_ts
    )
    return {"accepted": True, "action": "approve_in_progress"}


async def _poll_merge_and_finalize(
    settings: Settings,
    session_id: int,
    mr: dict[str, Any],
    user_id: str,
    channel: str,
    message_ts: str,
) -> None:
    gitlab = GitLabClient(
        settings.gitlab_url, secret_value(settings.gitlab_token or ""), verify_ssl=settings.gitlab_verify_ssl
    )
    merged = False
    failure_detail: str | None = None

    for _attempt in range(POLL_MAX_ATTEMPTS):
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            current_mr = await gitlab.get_merge_request(mr["project_id"], mr["iid"])
        except httpx.HTTPError:
            logger.exception("Merge-status poll request failed (session=%s)", session_id)
            continue
        state = current_mr.get("state")
        if state == "merged":
            merged = True
            break
        merge_error = current_mr.get("merge_error")
        if merge_error or state == "closed":
            failure_detail = merge_error or f"state={state}"
            break

    if merged:
        accepted = _transition_and_log(settings, session_id, "merge_poll_success", None, from_status=MERGING, to_status=MERGED)
        if accepted:
            await _notify_merge_success(settings, channel, message_ts, mr, user_id)
        return

    detail = failure_detail or "머지 차단: 조건 미충족 (poll 10회 실패)"
    accepted = _transition_and_log(settings, session_id, "merge_poll_failed", detail)
    if accepted:
        header = "⚠️ 머지 차단: 조건 미충족" if failure_detail is None else "⚠️ 머지 실패"
        await _notify_merge_manual(settings, channel, message_ts, mr, f"{header} — {detail}")


def _transition_and_log(
    settings: Settings,
    session_id: int,
    reason: str,
    detail: str | None,
    *,
    from_status: str = MERGING,
    to_status: str = MANUAL,
) -> bool:
    conn = _get_conn(settings)
    try:
        return cas_transition(conn, session_id, from_status, to_status, reason=reason, detail=detail)
    finally:
        conn.close()


async def _notify_merge_success(
    settings: Settings, channel: str, message_ts: str, mr: dict[str, Any], user_id: str
) -> None:
    if not settings.slack_bot_token:
        return
    try:
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.update_decision(channel, message_ts, mr, approved=True, slack_user_id=user_id)
    except Exception:
        logger.exception("Slack merge-success message update failed")


async def _notify_merge_manual(
    settings: Settings, channel: str, message_ts: str, mr: dict[str, Any], reason: str
) -> None:
    if not settings.slack_bot_token:
        return
    try:
        summary = f"!{mr.get('iid')} {mr.get('title') or ''}".strip()
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.withdraw_buttons(channel, message_ts, summary, reason)
    except Exception:
        logger.exception("Slack merge-failure message update failed")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _get_conn(settings: Settings) -> sqlite3.Connection:
    conn = get_connection(settings.db_path)
    init_db(conn)
    return conn


def _find_session(conn: sqlite3.Connection, project_id: Any, mr_iid: Any) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_session WHERE project_id = ? AND mr_iid = ?",
        (str(project_id), mr_iid),
    ).fetchone()


def _log_event(conn: sqlite3.Connection, session_id: int, kind: str, detail: str | None = None) -> None:
    """Manually record an event for a rejection/duplicate-click that never
    reached a CAS transition (accepted CAS transitions log themselves)."""

    conn.execute(
        "INSERT INTO event_log (session_id, kind, detail) VALUES (?, ?, ?)",
        (session_id, kind, detail),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Slack ephemeral responses (response_url — no chat.postEphemeral method
# needed; not a new external dependency, httpx is already a project dep)
# ---------------------------------------------------------------------------
async def _send_ephemeral(response_url: str | None, text: str) -> None:
    if not response_url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                response_url,
                json={
                    "response_type": "ephemeral",
                    "text": text,
                    # Slack replaces the original message when these are
                    # omitted (its response_url default) — that would silently
                    # delete the [승인]/[의견] buttons still bound to their
                    # valid signed action token, e.g. an unauthorized click's
                    # rejection would otherwise wipe the message before an
                    # authorized reviewer ever gets to click it. This POST is
                    # a plain ephemeral notice, never a deliberate original
                    # -message update, so both must always stay False here.
                    "replace_original": False,
                    "delete_original": False,
                },
            )
    except httpx.HTTPError:
        logger.warning("Failed to send Slack ephemeral response")


# ---------------------------------------------------------------------------
# Payload parsing helpers
# ---------------------------------------------------------------------------
def _decode_mr(token: str, settings: Settings) -> dict[str, Any]:
    if not settings.action_token_secret:
        raise HTTPException(status_code=503, detail="ACTION_TOKEN_SECRET is not configured")
    mr = decode_action_token(token, secret_value(settings.action_token_secret))
    required = (
        "project_id",
        "repository",
        "iid",
        "title",
        "url",
        "sha",
        "head_ref",
        "base_ref",
        "author",
    )
    if (
        not all(mr.get(key) for key in required)
        or not isinstance(mr["project_id"], int)
        or not isinstance(mr["iid"], int)
    ):
        raise InvalidActionToken("Action token is missing merge-request data")
    return mr


def _slack_user_id(payload: dict[str, Any]) -> str:
    user = payload.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise HTTPException(status_code=400, detail="Slack user is missing")
    return user["id"]


def _message_location(payload: dict[str, Any]) -> tuple[str, str]:
    channel = payload.get("channel")
    container = payload.get("container")
    if not isinstance(channel, dict) or not isinstance(container, dict):
        raise HTTPException(status_code=400, detail="Slack message location is missing")
    channel_id = channel.get("id")
    message_ts = container.get("message_ts")
    if not isinstance(channel_id, str) or not isinstance(message_ts, str):
        raise HTTPException(status_code=400, detail="Slack message location is invalid")
    return channel_id, message_ts
