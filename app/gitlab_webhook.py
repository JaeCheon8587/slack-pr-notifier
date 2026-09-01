"""GitLab webhook rail — v4.1 design (docs/mr-review-pipeline.html §①·§③).

Route: POST /webhooks/gitlab/mr. Handles merge_request (open/reopen/update/
merge/close) and push events. This module is intentionally thin: secret
verification, the X-Gitlab-Webhook-UUID idempotency ledger, payload
parsing/normalization, and response assembly are its only jobs here — the
actual event-handling units (session create/reuse + guard + Slack notify,
the human-push policy, external merge/close detection) live in ``app.ingest``
so a future polling rail (GitLab -> this host inbound webhooks blocked by
network policy in some deployments) can drive the exact same logic without
going through this route. All ``review_session.status`` changes still go
through ``app.state_machine.cas_transition`` (inside ``app.ingest``) — this
module never writes the ``status`` column directly.

Scope note (P2b): approval-button handling, the opinion modal, and revise
execution are the next phase (P3+) and are intentionally out of scope here;
this rail only creates sessions, applies the human-push policy, and detects
external merge/close.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from json import JSONDecodeError
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings, secret_value
from app.db import get_connection, init_db
from app.ingest import (
    _create_session,
    _find_session,
    handle_external_close,
    handle_human_push,
    handle_mr_open,
)
from app.security import verify_gitlab_token
from app.state_machine import MERGING, REVIEWING, REVISING

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@router.post("/gitlab/mr")
async def receive_gitlab_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    background_tasks: BackgroundTasks,
    gitlab_event: Annotated[str | None, Header(alias="X-Gitlab-Event")] = None,
    gitlab_delivery: Annotated[str | None, Header(alias="X-Gitlab-Webhook-UUID")] = None,
    gitlab_token: Annotated[str | None, Header(alias="X-Gitlab-Token")] = None,
) -> dict[str, Any]:
    """Receive and verify GitLab project webhook events (merge_request, push)."""

    # I1: fail-closed if the secret is not configured.
    if not settings.gitlab_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITLAB_WEBHOOK_SECRET is not configured",
        )
    # I2/I3: verify (constant-time) before any parsing.
    if not verify_gitlab_token(gitlab_token, secret_value(settings.gitlab_webhook_secret)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitLab webhook token",
        )
    if gitlab_event is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Gitlab-Event header is missing",
        )

    body = await request.body()
    try:
        payload = json.loads(body)
    except (JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object",
        )

    conn = _get_conn(settings)
    try:
        if gitlab_event == "Merge Request Hook" or payload.get("object_kind") == "merge_request":
            return await _handle_merge_request(conn, payload, settings, background_tasks, gitlab_delivery)
        if gitlab_event == "Push Hook" or payload.get("object_kind") == "push":
            return await _handle_push_event(conn, payload, settings, gitlab_delivery)

        logger.info(
            "GitLab event ignored: delivery=%s event=%s",
            gitlab_delivery,
            gitlab_event,
        )
        return {"accepted": True, "event": gitlab_event, "ignored": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB helpers (route-only: idempotency ledger + push active-session lookup —
# neither is needed by a poller, which has no delivery UUID and already
# knows exactly which MR it is polling)
# ---------------------------------------------------------------------------
def _get_conn(settings: Settings) -> sqlite3.Connection:
    """Open a fresh connection per request and ensure the schema exists.

    ``init_db`` is idempotent (CREATE TABLE IF NOT EXISTS); opening per
    request (rather than a cached module-level connection) keeps this rail
    trivially testable against an isolated ``settings.db_path`` per test.
    """

    conn = get_connection(settings.db_path)
    init_db(conn)
    return conn


def _find_active_session_for_push(
    conn: sqlite3.Connection, project_id: Any, before_sha: str | None
) -> sqlite3.Row | None:
    """Resolve the active session touched by a push (project + branch).

    review_session has no branch column (schema is frozen for this phase), so
    the branch is inferred: within a project, an unambiguous single active
    session is used directly; if several are active for the same project,
    disambiguate via ``mr_sha == before`` (the branch's previous head, which
    is exactly the session's last known head). If still ambiguous, the push
    is ignored (safe no-op) rather than guessing — see RISKS in the receipt.
    """

    rows = conn.execute(
        "SELECT * FROM review_session WHERE project_id = ? AND status IN (?, ?, ?)",
        (str(project_id), REVIEWING, REVISING, MERGING),
    ).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    for row in rows:
        if before_sha is not None and row["mr_sha"] == before_sha:
            return row
    return None


def _try_idempotency_insert(
    conn: sqlite3.Connection,
    session_id: int,
    delivery_uuid: str | None,
    kind: str,
    detail: str | None = None,
) -> bool:
    """Record a webhook delivery against event_log.idempotency_key.

    Returns True if this is the first time this delivery UUID has been seen
    (caller should proceed), False if it is a duplicate (caller must no-op).
    A missing UUID header always proceeds (no idempotency key to check).
    """

    if delivery_uuid is None:
        return True
    try:
        conn.execute(
            "INSERT INTO event_log (session_id, kind, detail, idempotency_key) VALUES (?, ?, ?, ?)",
            (session_id, kind, detail, delivery_uuid),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError as error:
        conn.rollback()
        # Only a UNIQUE violation on idempotency_key means "already seen this
        # delivery" (the intended duplicate no-op). Any other IntegrityError
        # (e.g. a FOREIGN KEY or NOT NULL violation) is a real data-integrity
        # bug and must not be silently swallowed as a duplicate.
        if "UNIQUE" not in str(error):
            logger.error(
                "Idempotency insert failed with a non-UNIQUE IntegrityError: session=%s kind=%s",
                session_id,
                kind,
            )
            raise
        return False


# ---------------------------------------------------------------------------
# merge_request event routing
# ---------------------------------------------------------------------------
async def _handle_merge_request(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    settings: Settings,
    background_tasks: BackgroundTasks,
    delivery: str | None,
) -> dict[str, Any]:
    attributes = payload.get("object_attributes")
    if not isinstance(attributes, dict):
        raise HTTPException(status_code=400, detail="object_attributes object is missing")
    project = payload.get("project")
    if not isinstance(project, dict):
        raise HTTPException(status_code=400, detail="project object is missing")
    last_commit = attributes.get("last_commit")
    if not isinstance(last_commit, dict):
        last_commit = {}

    action = attributes.get("action")
    project_id = project.get("id")
    mr_iid = attributes.get("iid")
    sha = last_commit.get("id")
    repo_slug = project.get("path_with_namespace")

    logger.info(
        "GitLab MR received: delivery=%s action=%s project=%s mr=%s title=%s sha=%s",
        delivery,
        action,
        repo_slug,
        mr_iid,
        attributes.get("title"),
        sha,
    )

    base_response = {
        "accepted": True,
        "event": "merge_request",
        "action": action,
        "repository": repo_slug,
        "merge_request_iid": mr_iid,
        "title": attributes.get("title"),
        "head_sha": sha,
    }

    if project_id is None or mr_iid is None:
        return {**base_response, "ignored": True, "reason": "missing_project_or_iid"}

    session = _find_session(conn, project_id, mr_iid)

    if action in {"open", "reopen"}:
        created = session is None
        if created:
            session = _create_session(conn, project_id, mr_iid, sha, repo_slug)
        assert session is not None
        # Idempotency gate stays synchronous here (route responsibility) so a
        # retried delivery never schedules a second notify; session
        # creation/reuse and the sha touch-up on reopen are the shared
        # ingest function's job (app.ingest.handle_mr_open), run in the
        # background below exactly like the notify it wraps always was.
        proceed = _try_idempotency_insert(conn, session["id"], delivery, f"mr_{action}")
        if not proceed:
            return {**base_response, "duplicate": True}
        author = payload.get("user")
        actor = author.get("username") if isinstance(author, dict) else None
        background_tasks.add_task(
            _run_mr_open,
            settings,
            project_id=project_id,
            repo_slug=repo_slug,
            mr_iid=mr_iid,
            sha=sha,
            title=attributes.get("title"),
            url=attributes.get("url"),
            source_branch=attributes.get("source_branch"),
            target_branch=attributes.get("target_branch"),
            actor=actor,
        )
        return base_response

    if session is None:
        return {**base_response, "ignored": True, "reason": "no_session"}

    if action == "update":
        # v4.1 C1: push and MR `update` fire together — `update` is never used to
        # (re)trigger review-prep. Record it for observability/idempotency only.
        _try_idempotency_insert(conn, session["id"], delivery, "mr_update_ignored")
        return {**base_response, "ignored": True}

    if action in {"merge", "close"}:
        proceed = _try_idempotency_insert(conn, session["id"], delivery, f"mr_{action}")
        if not proceed:
            return {**base_response, "duplicate": True}
        result = await handle_external_close(
            settings,
            conn,
            project_id=project_id,
            mr_iid=mr_iid,
            title=attributes.get("title"),
            action=action,
            source="webhook",
        )
        return {**base_response, "transitioned": result["transitioned"]}

    return {**base_response, "ignored": True}


async def _run_mr_open(
    settings: Settings,
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
) -> None:
    """BackgroundTasks adapter for ``app.ingest.handle_mr_open``.

    The request's own connection is already closed by the time
    ``BackgroundTasks`` run (it is closed in ``receive_gitlab_webhook``'s
    ``finally`` as soon as the synchronous part of the handler returns), so
    this opens a connection scoped to this task's own lifetime — same
    pattern the pre-refactor background notify helpers used.
    """

    conn = _get_conn(settings)
    try:
        await handle_mr_open(
            settings,
            conn,
            project_id=project_id,
            repo_slug=repo_slug,
            mr_iid=mr_iid,
            sha=sha,
            title=title,
            url=url,
            source_branch=source_branch,
            target_branch=target_branch,
            actor=actor,
            source="webhook",
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# push event routing (human-push policy, §③/M5/M8)
# ---------------------------------------------------------------------------
async def _handle_push_event(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    settings: Settings,
    delivery: str | None,
) -> dict[str, Any]:
    project = payload.get("project")
    project_id = payload.get("project_id")
    if project_id is None and isinstance(project, dict):
        project_id = project.get("id")
    if project_id is None:
        return {"accepted": True, "event": "push", "ignored": True, "reason": "no_project_id"}

    before_sha = payload.get("before")
    after_sha = payload.get("after") or payload.get("checkout_sha")

    session = _find_active_session_for_push(conn, project_id, before_sha)
    if session is None:
        return {"accepted": True, "event": "push", "ignored": True, "reason": "no_active_session"}

    proceed = _try_idempotency_insert(conn, session["id"], delivery, "push")
    if not proceed:
        return {"accepted": True, "event": "push", "duplicate": True}

    result = await handle_human_push(
        settings,
        conn,
        project_id=project_id,
        mr_iid=session["mr_iid"],
        new_sha=after_sha,
        commit_author=payload.get("user_username"),
        commit_author_email=payload.get("user_email"),
        source="webhook",
    )
    return {"accepted": True, "event": "push", **result}
