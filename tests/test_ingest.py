"""Regression tests for the project-id-fix ledger item
(.orchestration/reports/project-id-fix.md): app.gitlab_poller passes
project_id as a *string* (settings.poll_project_ids_parsed yields strings),
while app.slack_dispatch._decode_mr requires the signed action token's
project_id/iid to be ``int`` (isinstance checks) -- so a button posted via
the poller rail used to fail on click with
``InvalidActionToken: Action token is missing merge-request data``. The
webhook rail never hit this because its JSON payload's ``project.id`` is
already an int.

Covers app/ingest.py's ``handle_mr_open`` mr-dict/token-payload shape (the
fix itself), the full round trip into app/slack_dispatch.py's approve click
(the regression this fix resolves), and a pin on the untouched DB-storage
convention (review_session.project_id stays ``str(project_id)``, per
CONSTRAINTS).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import BackgroundTasks

import app.ingest as ingest
import app.slack_dispatch as slack_dispatch
from app.config import get_settings
from app.db import get_connection, init_db
from app.gitlab_client import GitLabClient
from app.slack_client import SlackClient
from app.state_machine import REVIEWING

ACTION_SECRET = "test-action-secret"
PROJECT_ID_STR = "918"  # poller-style: a numeric string, not an int
MR_IID = 7
REPO_SLUG = "group/project"
SHA = "deadbeef"
AUTHORIZED_USER = "U_AUTH"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _configure(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_channel_id", "C_CHANNEL")
    monkeypatch.setattr(settings, "action_token_secret", ACTION_SECRET)
    monkeypatch.setattr(settings, "gitlab_token", "gitlab-test")
    monkeypatch.setattr(settings, "reviewer_map", '{"' + REPO_SLUG + '": "' + AUTHORIZED_USER + '"}')
    return settings


def _conn(settings):  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    init_db(conn)
    return conn


async def _open_mr(settings, conn):  # type: ignore[no-untyped-def]
    """Call handle_mr_open exactly the way app.gitlab_poller does: project_id
    as a numeric string (poller-style), mr_iid as int (GitLab API's JSON
    ``iid``, already int on that rail)."""

    return await ingest.handle_mr_open(
        settings,
        conn,
        project_id=PROJECT_ID_STR,
        repo_slug=REPO_SLUG,
        mr_iid=MR_IID,
        sha=SHA,
        title="Fix bug",
        url="https://gitlab.example.com/group/project/-/merge_requests/7",
        source_branch="feature/x",
        target_branch="main",
        actor="alice",
        source="poller",
    )


# ---------------------------------------------------------------------------
# [1] mr-dict/token-payload shape: project_id/iid must normalize to int
# ---------------------------------------------------------------------------
def test_handle_mr_open_normalizes_project_id_and_iid_to_int(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Even called with a string project_id (exactly as
    app.gitlab_poller._dispatch_mr_open does), the mr dict handle_mr_open
    feeds into create_action_token must carry int project_id/iid -- the
    shape app.slack_dispatch._decode_mr's isinstance checks require."""

    settings = _configure(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    async def fake_post_mr_message(self, channel, mr, token, review=None):  # noqa: ANN001
        captured["mr"] = mr
        captured["token"] = token
        return {"channel": "C123", "ts": "111.222"}

    monkeypatch.setattr(SlackClient, "post_mr_message", fake_post_mr_message)

    conn = _conn(settings)
    try:
        result = asyncio.run(_open_mr(settings, conn))
    finally:
        conn.close()

    assert result["notified"] is True
    assert isinstance(captured["mr"]["project_id"], int)
    assert captured["mr"]["project_id"] == int(PROJECT_ID_STR)
    assert isinstance(captured["mr"]["iid"], int)
    assert captured["mr"]["iid"] == MR_IID


# ---------------------------------------------------------------------------
# [2] Core round-trip regression: poller-style string project_id -> the
# resulting button token must pass app.slack_dispatch's approve click.
# ---------------------------------------------------------------------------
def test_poller_style_string_project_id_round_trips_through_approve_click(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The signed button token handle_mr_open builds from a poller-style
    string project_id must decode and pass app.slack_dispatch's approve
    click all the way to the merge call -- previously this raised
    InvalidActionToken("Action token is missing merge-request data") because
    the token's project_id round-tripped back as the string "918", failing
    _decode_mr's ``isinstance(mr["project_id"], int)`` check."""

    settings = _configure(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    async def fake_post_mr_message(self, channel, mr, token, review=None):  # noqa: ANN001
        captured["token"] = token
        return {"channel": "C123", "ts": "111.222"}

    monkeypatch.setattr(SlackClient, "post_mr_message", fake_post_mr_message)

    conn = _conn(settings)
    try:
        result = asyncio.run(_open_mr(settings, conn))
    finally:
        conn.close()
    assert result["notified"] is True
    token = captured["token"]

    async def fake_get_merge_request(self, project_id, iid):  # noqa: ANN001
        return {"sha": SHA, "state": "opened"}

    merge_calls: list[tuple[Any, Any, Any]] = []

    async def fake_merge_merge_request(self, project_id, iid, sha):  # noqa: ANN001
        merge_calls.append((project_id, iid, sha))
        return {"state": "opened"}

    async def fake_create_merge_request_note(self, project_id, iid, body):  # noqa: ANN001
        return {"id": 1}

    monkeypatch.setattr(GitLabClient, "get_merge_request", fake_get_merge_request)
    monkeypatch.setattr(GitLabClient, "merge_merge_request", fake_merge_merge_request)
    monkeypatch.setattr(GitLabClient, "create_merge_request_note", fake_create_merge_request_note)

    payload = {
        "type": "block_actions",
        "user": {"id": AUTHORIZED_USER},
        "channel": {"id": "C123"},
        "container": {"message_ts": "111.222"},
        "response_url": "https://hooks.slack.example.com/actions/response",
        "actions": [{"action_id": "approve_mr", "value": token}],
    }

    # If the bug were still present, _decode_mr would raise InvalidActionToken
    # here, which dispatch_interaction converts to an HTTPException -- i.e.
    # this call itself is the regression guard, independent of the asserts
    # below.
    result = asyncio.run(
        slack_dispatch.dispatch_interaction(
            settings, payload, source="http", background_tasks=BackgroundTasks()
        )
    )

    assert result == {"accepted": True, "action": "approve_in_progress"}
    assert merge_calls == [(int(PROJECT_ID_STR), MR_IID, SHA)]


# ---------------------------------------------------------------------------
# [3] DB-storage convention pin (CONSTRAINTS): review_session.project_id
# stays str(project_id), unaffected by the token-payload normalization above.
# ---------------------------------------------------------------------------
def test_session_row_still_keyed_by_string_project_id(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _configure(monkeypatch, tmp_path)

    async def fake_post_mr_message(self, channel, mr, token, review=None):  # noqa: ANN001
        return {"channel": "C123", "ts": "111.222"}

    monkeypatch.setattr(SlackClient, "post_mr_message", fake_post_mr_message)

    conn = _conn(settings)
    try:
        asyncio.run(_open_mr(settings, conn))
        row = conn.execute(
            "SELECT project_id, status FROM review_session WHERE mr_iid = ?", (MR_IID,)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["project_id"] == PROJECT_ID_STR  # stored as string, unchanged convention
    assert row["status"] == REVIEWING
