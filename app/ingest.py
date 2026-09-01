"""Shared MR-event ingestion logic — v4.1 design (docs/mr-review-pipeline.html
§①·§③), extracted from the GitLab webhook rail so it can be driven by either
an inbound webhook delivery or an outbound poller (P.2 폴링 수집 경로: GitLab ->
this host inbound webhooks are blocked by network policy in some deployments,
so the same event-handling units below must also be callable from a polling
loop that has no HTTP request/response of its own).

This module owns exactly three event-handling units, one per MR lifecycle
event the pipeline reacts to:

    handle_mr_open       -- MR opened/reopened: create-or-reuse the session,
                             fail closed on an empty reviewer mapping, else
                             build the AI review and post/persist the Slack
                             message.
    handle_human_push    -- a push landed on a tracked MR: ignore the
                             middleware's own revise-commit echo, otherwise
                             apply the human-push policy for the session's
                             current state (reviewing/revising/merging).
    handle_external_close -- an MR was merged or closed outside of Slack:
                             CAS the session to merged/manual and withdraw
                             the review buttons.

Every ``review_session.status`` change still goes exclusively through
``app.state_machine.cas_transition`` -- this module never writes the
``status`` column directly. Each function takes an already-open
``sqlite3.Connection`` (the caller owns its lifecycle) and a ``source``
string ("webhook" | "poller") that is recorded as the CAS transition's
``event_log.detail`` for observability only -- it never changes control flow.

Dependency layering (kept import-cycle free): this module depends only on
``app.config``, ``app.db``-shaped connections (no import of ``app.db``
itself is required), ``app.state_machine``, ``app.slack_client``,
``app.gitlab_client``, ``app.ai_reviewer``, and the standalone
``app.action_token`` helper. It must never import ``app.gitlab_webhook``
(that module imports this one).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.action_token import create_action_token
from app.ai_reviewer import MRReview, review_merge_request
from app.config import Settings, secret_value
from app.gitlab_client import GitLabClient
from app.report_html import render_review_report
from app.slack_client import SlackClient
from app.state_machine import (
    ALLOWED_TRANSITIONS,
    MANUAL,
    MERGED,
    MERGING,
    REVIEWING,
    REVISING,
    cas_transition,
)

_EMPTY_REVIEWER_MAPPING_WARNING = (
    "⚠️ !{iid} ({repo}) 담당자 매핑이 없어 자동 승인 게이트를 열 수 없습니다 — "
    "상태를 manual로 전환합니다. REVIEWER_MAP 설정을 확인해 주세요."
)

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Session DB helpers (shared by the three event-handling units below, and by
# app.gitlab_webhook's route for its own idempotency-key/session resolution).
# ---------------------------------------------------------------------------
def _find_session(conn: sqlite3.Connection, project_id: Any, mr_iid: Any) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_session WHERE project_id = ? AND mr_iid = ?",
        (str(project_id), mr_iid),
    ).fetchone()


def _create_session(
    conn: sqlite3.Connection, project_id: Any, mr_iid: Any, sha: str | None, repo_slug: str | None
) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO review_session (project_id, mr_iid, mr_sha, repo_slug) VALUES (?, ?, ?, ?)",
        (str(project_id), mr_iid, sha, repo_slug),
    )
    conn.commit()
    session = _find_session(conn, project_id, mr_iid)
    assert session is not None  # just inserted
    return session


def _touch_sha(conn: sqlite3.Connection, session_id: int, sha: str | None) -> None:
    if sha is None:
        return
    conn.execute(
        "UPDATE review_session SET mr_sha = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE id = ?",
        (sha, session_id),
    )
    conn.commit()


def _is_bot_actor(settings: Settings, username: str | None, email: str | None) -> bool:
    """True if the push actor is the middleware's own bot account (revise echo)."""

    if settings.bot_username and username == settings.bot_username:
        return True
    if settings.bot_email and email == settings.bot_email:
        return True
    return False


def _normalize_id_for_token(value: Any, *, field: str) -> Any:
    """Coerce a numeric-string id to int for the signed action-token payload.

    ``app.slack_dispatch._decode_mr`` requires the token's ``project_id``/
    ``iid`` to be ``int`` (isinstance checks). The webhook rail already hands
    ``handle_mr_open`` an int here (the JSON payload's ``project.id``), but
    the poller rail (``app.gitlab_poller``, driven by
    ``settings.poll_project_ids_parsed`` -- a list of *strings*) hands it a
    numeric string instead, which used to reach the signed token unchanged
    and fail that isinstance check on click ("Action token is missing
    merge-request data").

    Only affects the ``mr`` dict fed into ``create_action_token`` below --
    never the DB's own ``project_id`` column, which stays ``str`` by the
    existing storage convention (see ``_find_session``/``_create_session``
    above, unaffected by this helper).

    Left unchanged (never raises) if ``value`` cannot be parsed as an int --
    the token will still fail to decode downstream exactly as before this
    normalization existed, just with a warning logged here instead of
    silently miscarrying.
    """

    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "ingest: could not normalize %s=%r to int for the action-token payload", field, value
        )
        return value


# ---------------------------------------------------------------------------
# [1] MR open/reopen
# ---------------------------------------------------------------------------
async def handle_mr_open(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    project_id: Any,
    repo_slug: str | None,
    mr_iid: Any,
    sha: str | None,
    title: str | None,
    url: str | None,
    source_branch: str | None,
    target_branch: str | None,
    actor: str | None,
    source: str,
) -> dict[str, Any]:
    """Create-or-reuse the review_session for an opened/reopened MR, then
    either fail closed (empty reviewer mapping -> manual) or build the AI
    review and post/persist the Slack message.

    Reuse touches ``mr_sha`` to the given ``sha`` (matching the previous
    webhook-only reopen behaviour) -- safe to call again for an
    already-existing session (e.g. once synchronously by the webhook route
    to resolve an idempotency key, and again here): a repeat call finds the
    same row and re-applies the same value.
    """

    session = _find_session(conn, project_id, mr_iid)
    created = session is None
    if created:
        session = _create_session(conn, project_id, mr_iid, sha, repo_slug)
    else:
        _touch_sha(conn, session["id"], sha)
    assert session is not None
    session_id = session["id"]

    mr = {
        "project_id": _normalize_id_for_token(project_id, field="project_id"),
        "repository": repo_slug,
        "iid": _normalize_id_for_token(mr_iid, field="iid"),
        "title": title,
        "url": url,
        "sha": sha,
        "head_ref": source_branch,
        "base_ref": target_branch,
        "author": actor,
    }

    result = {"created": created, "session_id": session_id, "notified": False}

    required = (settings.slack_bot_token, settings.slack_channel_id, settings.action_token_secret)
    if not all(required):
        logger.warning(
            "Slack notification skipped: SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, "
            "and ACTION_TOKEN_SECRET must be configured"
        )
        return result

    if not all(value is not None and value != "" for value in mr.values()):
        logger.warning("Slack notification skipped: merge request payload is missing fields")
        return result

    # v4.1: a session whose repo has no reviewer mapping (empty Slack user
    # set) can never pass the approve-button's step-0 authorization check
    # (app/slack_actions.py) — fail-closed at notification time instead of
    # posting review buttons nobody is allowed to click. This is the single
    # branch point for that guard (docs/mr-review-pipeline.html §① / §⑦).
    if not settings.reviewers_for(repo_slug):
        await _notify_empty_reviewer_mapping(settings, conn, session_id, mr, repo_slug, source)
        return result

    try:
        token = create_action_token(mr, secret_value(settings.action_token_secret))
        review, context = await _build_ai_review(mr, settings)
        client = SlackClient(secret_value(settings.slack_bot_token))
        posted = await client.post_mr_message(
            settings.slack_channel_id,  # type: ignore[arg-type]
            mr,
            token,
            review=review,
        )
        _save_slack_coordinates(conn, session_id, posted)
        result["notified"] = True
    except Exception:
        logger.exception("Slack notification failed")
    else:
        # Best-effort HTML report delivery -- failures are logged inside
        # _deliver_review_report and never propagated: the notification
        # itself is already final here.
        if review is not None:
            await _deliver_review_report(settings, mr, review, context, posted, session_id)
    return result


async def _notify_empty_reviewer_mapping(
    settings: Settings,
    conn: sqlite3.Connection,
    session_id: int,
    mr: dict[str, Any],
    repo_slug: Any,
    source: str,
) -> None:
    """Fail-closed branch for a session whose repo has no reviewer mapping.

    CAS-transitions the session straight to ``manual`` (reason=guard_reject)
    instead of posting review buttons, and posts a channel warning so an
    operator notices the missing REVIEWER_MAP entry.
    """

    cas_transition(conn, session_id, REVIEWING, MANUAL, reason="guard_reject", detail=source)

    if not (settings.slack_bot_token and settings.slack_channel_id):
        logger.warning(
            "Empty reviewer-mapping warning skipped: SLACK_BOT_TOKEN / SLACK_CHANNEL_ID "
            "must be configured (session=%s)",
            session_id,
        )
        return
    try:
        client = SlackClient(secret_value(settings.slack_bot_token))
        text = _EMPTY_REVIEWER_MAPPING_WARNING.format(iid=mr.get("iid"), repo=repo_slug or mr.get("project_id"))
        await client.call("chat.postMessage", {"channel": settings.slack_channel_id, "text": text})
    except Exception:
        logger.exception("Empty reviewer-mapping channel warning failed (session=%s)", session_id)


def _save_slack_coordinates(conn: sqlite3.Connection, session_id: int, result: dict[str, Any]) -> None:
    channel = result.get("channel") if isinstance(result, dict) else None
    message_ts = result.get("ts") if isinstance(result, dict) else None
    if not channel or not message_ts:
        return
    conn.execute(
        "UPDATE review_session SET slack_channel = ?, slack_ts = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (channel, message_ts, session_id),
    )
    conn.commit()


async def _build_ai_review(
    mr: dict[str, Any], settings: Settings
) -> tuple[MRReview | None, dict[str, Any] | None]:
    """Fetch MR context and produce an AI review; returns (review, context).

    The GitLab context (changed files / diffs / contents) is returned
    alongside the review so the caller can render the HTML report from the
    same fetch instead of querying GitLab twice. Either element is None on
    any failure -- the Slack notification goes out without AI content.
    """

    if not (settings.ai_enabled and settings.gitlab_token):
        return None, None
    try:
        context = await GitLabClient(
            settings.gitlab_url,
            secret_value(settings.gitlab_token),
            verify_ssl=settings.gitlab_verify_ssl,
        ).fetch_mr_context(mr["project_id"], mr["iid"], mr["sha"])
    except Exception:
        logger.exception("Fetching MR context for AI review failed")
        return None, None
    review = await review_merge_request(mr, context, settings)
    return review, context


def _report_filename(mr: dict[str, Any], round_number: int | None = None) -> str:
    """Deterministic report.html name: report-<repo>-<iid>[-rN]-<sha8>.html."""

    repo = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        str(mr.get("repository") or mr.get("project_id") or "repo"),
    )
    sha = str(mr.get("sha") or "")[:8] or "nosha"
    round_part = f"-r{round_number}" if round_number is not None else ""
    return f"report-{repo}-{mr.get('iid')}{round_part}-{sha}.html"


async def _deliver_review_report(
    settings: Settings,
    mr: dict[str, Any],
    review: Any,
    context: dict[str, Any] | None,
    posted: dict[str, Any],
    session_id: int,
    *,
    round_number: int | None = None,
    client: SlackClient | None = None,
) -> None:
    """Render the HTML review report, archive it, and upload it to the thread.

    Best-effort by design: every failure is logged and swallowed -- the
    Slack notification must never depend on the report (the file is
    additive detail, not a gate).
    """

    if not settings.report_html_enabled:
        return

    try:
        html_text = render_review_report(mr, review, context)
        name = _report_filename(mr, round_number)
        path = Path(settings.report_html_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_text, encoding="utf-8")
    except Exception:
        logger.exception("Review report HTML generation failed (session=%s)", session_id)
        return

    channel = posted.get("channel") if isinstance(posted, dict) else None
    thread_ts = posted.get("ts") if isinstance(posted, dict) else None
    if not channel or not thread_ts:
        logger.warning(
            "Review report upload skipped: no Slack coordinates (session=%s)", session_id
        )
        return

    try:
        uploader = client or SlackClient(secret_value(settings.slack_bot_token))
        await uploader.upload_report_file(
            str(channel),
            str(thread_ts),
            name,
            html_text,
            initial_comment="📄 리뷰 리포트 (report.html)",
        )
    except Exception:
        logger.exception("Review report HTML upload failed (session=%s)", session_id)


# ---------------------------------------------------------------------------
# [2] human-push policy (§③/M5/M8)
# ---------------------------------------------------------------------------
async def handle_human_push(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    project_id: Any,
    mr_iid: Any,
    new_sha: str | None,
    commit_author: str | None,
    commit_author_email: str | None = None,
    source: str,
) -> dict[str, Any]:
    """Apply the human-push policy to the session identified by
    ``(project_id, mr_iid)``: ignore the middleware's own bot-commit echo,
    otherwise branch on the session's current status --
    reviewing -> reissue the [승인]-only button (SHA refreshed), revising ->
    manual, merging -> manual. Any other status is a no-op (already
    resolved/terminal).
    """

    session = _find_session(conn, project_id, mr_iid)
    if session is None:
        return {"ignored": True, "reason": "no_session"}

    if _is_bot_actor(settings, commit_author, commit_author_email):
        logger.info("Push ignored (bot actor echo): session=%s source=%s", session["id"], source)
        return {"ignored": True, "reason": "bot_actor"}

    current_status = session["status"]

    if current_status == REVIEWING:
        _touch_sha(conn, session["id"], new_sha)
        refreshed = _find_session(conn, project_id, mr_iid) or session
        await _reissue_approve_button(settings, refreshed, new_sha)
        return {"action": "reviewing_sha_updated"}

    if current_status == REVISING:
        accepted = cas_transition(conn, session["id"], REVISING, MANUAL, reason="human_push", detail=source)
        if accepted:
            await _withdraw_slack_buttons_for_session(
                settings, session, "사람 push 감지 — 자동 revise 중단(수동 확인 필요)"
            )
        return {"transitioned": accepted}

    if current_status == MERGING:
        accepted = cas_transition(
            conn, session["id"], MERGING, MANUAL, reason="push_during_merge", detail=source
        )
        if accepted:
            await _withdraw_slack_buttons_for_session(
                settings, session, "머지 중 사람 push 감지 — 담당자 재확인 필요"
            )
        return {"transitioned": accepted}

    return {"ignored": True, "reason": f"status={current_status}"}


async def _reissue_approve_button(settings: Settings, session: sqlite3.Row, sha: str | None) -> None:
    """Reissue the message with the [승인] button only, keyed to the new SHA.

    NOTE (RISKS): a push webhook carries no MR title/url/branches/author, and
    review_session does not persist them either (schema frozen this phase).
    The reissued action token is therefore built with placeholders for those
    fields; ``app.slack_actions._decode_mr`` (P3) requires all of them to be
    truthy, so this token will not decode successfully until a follow-up
    phase adds a metadata source (GitLab API fetch or a schema extension).
    Session/state changes in this rail are correct and independent of that
    gap.
    """

    if not all((settings.slack_bot_token, settings.action_token_secret)):
        logger.warning(
            "Slack button reissue skipped: SLACK_BOT_TOKEN / ACTION_TOKEN_SECRET must be configured"
        )
        return
    channel = session["slack_channel"]
    message_ts = session["slack_ts"]
    if not channel or not message_ts:
        logger.warning("Slack button reissue skipped: session %s has no Slack message", session["id"])
        return
    try:
        mr = _mr_from_session(session, sha)
        token = create_action_token(mr, secret_value(settings.action_token_secret))
        header = (
            f"MR !{session['mr_iid']} ({session['repo_slug'] or session['project_id']}) "
            "새 커밋 반영 — 재확인 후 승인해주세요"
        )
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.reissue_approve_only(channel, message_ts, header, token)
    except Exception:
        logger.exception("Slack button reissue failed")


def _mr_from_session(session: sqlite3.Row, sha: str | None) -> dict[str, Any]:
    project_id: Any = session["project_id"]
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        pass
    return {
        "project_id": project_id,
        "repository": session["repo_slug"] or "",
        "iid": session["mr_iid"],
        "title": "",
        "url": "",
        "sha": sha or session["mr_sha"],
        "head_ref": "",
        "base_ref": "",
        "author": "",
    }


async def _withdraw_slack_buttons_for_session(
    settings: Settings, session: sqlite3.Row, header_text: str
) -> None:
    """Withdraw the review buttons using only what is stored on the session
    itself (no merge_request payload -- e.g. a push-originated transition,
    where no title/url/etc. is available)."""

    if not settings.slack_bot_token:
        return
    channel = session["slack_channel"]
    message_ts = session["slack_ts"]
    if not channel or not message_ts:
        logger.warning("Slack button withdrawal skipped: session %s has no Slack message", session["id"])
        return
    try:
        summary = f"!{session['mr_iid']} ({session['repo_slug'] or session['project_id']})"
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.withdraw_buttons(channel, message_ts, summary, header_text)
    except Exception:
        logger.exception("Slack button withdrawal failed")


# ---------------------------------------------------------------------------
# [3] external merge/close detection
# ---------------------------------------------------------------------------
async def handle_external_close(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    project_id: Any,
    mr_iid: Any,
    title: str | None,
    action: str,
    source: str,
) -> dict[str, Any]:
    """Apply an externally observed merge/close to the session identified by
    ``(project_id, mr_iid)``: CAS (reviewing|revising|merging) -> merged (on
    ``action="merge"``) or -> manual (on ``action="close"``), and withdraw
    the review buttons on success. ``action`` must be "merge" or "close".

    A no-op (``transitioned: False``) if the edge is not a currently valid
    transition (already resolved -- e.g. raced with an approve click, or a
    terminal state) per §③ 651: whichever transition landed first wins.
    """

    session = _find_session(conn, project_id, mr_iid)
    if session is None:
        logger.warning(
            "External %s ignored: no session for project=%s mr=%s source=%s",
            action,
            project_id,
            mr_iid,
            source,
        )
        return {"transitioned": False}

    from_status = session["status"]
    if action == "merge":
        to_status = MERGED
        reason = "human_merge" if from_status == MANUAL else "external_merge"
        header_text = "외부 머지 감지"
    else:
        to_status = MANUAL
        reason = "external_close"
        header_text = "MR 닫힘"

    edge = (from_status, to_status)
    if edge not in ALLOWED_TRANSITIONS or reason not in ALLOWED_TRANSITIONS[edge]:
        # Already resolved (e.g. raced with an approve click, or terminal state) —
        # per §③ 651, whichever transition landed first wins; this is a no-op.
        logger.info(
            "External transition skipped (invalid/already resolved): session=%s %s->%s reason=%s source=%s",
            session["id"],
            from_status,
            to_status,
            reason,
            source,
        )
        return {"transitioned": False}

    accepted = cas_transition(conn, session["id"], from_status, to_status, reason=reason, detail=source)
    if accepted:
        await _withdraw_slack_buttons(settings, session, mr_iid, title, header_text)
    return {"transitioned": accepted}


async def _withdraw_slack_buttons(
    settings: Settings, session: sqlite3.Row, mr_iid: Any, title: str | None, header_text: str
) -> None:
    if not settings.slack_bot_token:
        return
    channel = session["slack_channel"]
    message_ts = session["slack_ts"]
    if not channel or not message_ts:
        logger.warning("Slack button withdrawal skipped: session %s has no Slack message", session["id"])
        return
    try:
        summary = f"!{mr_iid} {title or ''}".strip()
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.withdraw_buttons(channel, message_ts, summary, header_text)
    except Exception:
        logger.exception("Slack button withdrawal failed")
