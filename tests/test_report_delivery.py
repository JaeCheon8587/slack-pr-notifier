"""Tests for the report.html Slack delivery (files API v2) and ingest wiring.

Covers:
* SlackClient.upload_report_file -- the three-step files API v2 dance,
  including the pre-signed-URL rule (no Authorization header on step 2);
* ingest._report_filename -- deterministic, filesystem-safe naming;
* ingest.handle_mr_open -- when an AI review exists, the HTML report is
  rendered, archived, and uploaded to the notification's thread; when the
  upload fails, the notification result is unaffected (best-effort).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.ingest as ingest
from app.ingest import _report_filename
from app.slack_client import SlackClient


CHANNEL = "C_CHANNEL"
THREAD_TS = "111.222"


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RecordingPost:
    """Stand-in for httpx.AsyncClient.post inside upload_report_file."""

    calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, *, content: bytes) -> _FakeResponse:
        self.calls.append({"url": url, "content": content})
        return _FakeResponse()


@pytest.fixture()
def spy_upload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record SlackClient.call invocations; satisfy the files API contract."""

    calls: list[tuple[str, dict[str, Any]]] = []
    uploads: list[dict[str, Any]] = []

    async def fake_call(
        self, method: str, payload: dict[str, Any], *, form: bool = False
    ) -> dict[str, Any]:
        calls.append({"method": method, "payload": payload, "form": form})
        if method == "files.getUploadURLExternal":
            return {"upload_url": "https://files.slack.com/upload/v1/ABC", "file_id": "F123"}
        if method == "files.completeUploadExternal":
            return {"ok": True}
        raise AssertionError(f"unexpected method {method}")

    async def fake_post(self, url: str, *, content: bytes) -> _FakeResponse:
        uploads.append({"url": url, "content": content})
        return _FakeResponse()

    monkeypatch.setattr(SlackClient, "call", fake_call)
    monkeypatch.setattr("app.slack_client.httpx.AsyncClient.post", fake_post)
    return {"calls": calls, "uploads": uploads}


def test_upload_report_file_three_step_dance(spy_upload: dict[str, Any]) -> None:
    client = SlackClient("xoxb-test")
    result = asyncio.run(
        client.upload_report_file(
            CHANNEL,
            THREAD_TS,
            "report-group-project-7-deadbeef.html",
            "<html>report</html>",
            initial_comment="📄 리뷰 리포트",
        )
    )
    assert result == {"ok": True}

    methods = [call["method"] for call in spy_upload["calls"]]
    assert methods == ["files.getUploadURLExternal", "files.completeUploadExternal"]
    # The files API v2 family rejects JSON bodies with invalid_arguments;
    # both calls must go out form-encoded.
    assert all(call["form"] for call in spy_upload["calls"])

    reserve = spy_upload["calls"][0]["payload"]
    assert reserve["filename"] == "report-group-project-7-deadbeef.html"
    assert reserve["length"] == len("<html>report</html>".encode())

    # Step 2: raw bytes POSTed to the pre-signed URL (no auth header -- the
    # fake client records the call, and the real one omits Authorization).
    (upload,) = spy_upload["uploads"]
    assert upload["url"] == "https://files.slack.com/upload/v1/ABC"
    assert upload["content"] == b"<html>report</html>"

    # Step 3: published to the thread under the notification message.
    complete = spy_upload["calls"][1]["payload"]
    assert complete["files"] == [{"id": "F123"}]
    assert complete["channel_id"] == CHANNEL
    assert complete["thread_ts"] == THREAD_TS
    assert complete["initial_comment"] == "📄 리뷰 리포트"


def test_report_filename_is_deterministic_and_safe() -> None:
    mr = {"repository": "group/sub/project", "iid": 12, "sha": "cafe12345678"}
    assert _report_filename(mr) == "report-group-sub-project-12-cafe1234.html"
    assert _report_filename(mr, 3) == "report-group-sub-project-12-r3-cafe1234.html"
    # Path-hostile repo slugs are reduced to safe characters.
    hostile = {"repository": "a/../..\\evil", "iid": 1, "sha": "fff"}
    name = _report_filename(hostile)
    assert "/" not in name and "\\" not in name  # traversal needs a separator


def test_revise_round_delivers_html_report_to_new_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A finished revise round uploads report-...-rN-....html to the new message thread."""
    import app.revise_executor as revise_executor
    from app.config import get_settings
    from app.db import get_connection, init_db
    from app.ingest import _create_session

    settings = get_settings()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "action_token_secret", "secret")
    monkeypatch.setattr(settings, "gitlab_url", "https://gitlab.example.com")
    monkeypatch.setattr(settings, "gitlab_token", "glpat-test")
    monkeypatch.setattr(settings, "report_html_dir", str(tmp_path / "reports"))

    uploads: list[dict[str, Any]] = []

    class FakeSlack:
        def __init__(self, token: str) -> None:
            pass

        async def post_revise_result(
            self, channel, mr, token, *, round_number, unapplied,
            summary=None, diff_stat=None, compare_url=None
        ):  # noqa: ANN001
            return {"ts": "999.888", "channel": channel}

        async def withdraw_buttons(self, channel, message_ts, header_text, reason):  # noqa: ANN001
            return None

        async def upload_report_file(
            self, channel, thread_ts, filename, content, *, initial_comment=None
        ):  # noqa: ANN001
            uploads.append(
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "filename": filename,
                    "content": content,
                }
            )
            return {"ok": True}

    class FakeGitLab:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def fetch_mr_context(self, project_id, merge_request_iid, sha):  # noqa: ANN001
            return {"files": [], "contents": {}, "files_truncated": False}

    monkeypatch.setattr(revise_executor, "SlackClient", FakeSlack)
    monkeypatch.setattr(revise_executor, "GitLabClient", FakeGitLab)

    conn = get_connection(settings.db_path)
    init_db(conn)
    _create_session(conn, "918", 7, "deadbeef1234", "group/project")
    conn.execute(
        "UPDATE review_session SET slack_channel = ?, slack_ts = ? WHERE mr_iid = 7",
        (CHANNEL, "111.222"),
    )
    conn.commit()
    session = conn.execute("SELECT * FROM review_session WHERE mr_iid = 7").fetchone()

    mr_full = {
        "title": "Fix",
        "web_url": "https://gitlab.example.com/mr/7",
        "source_branch": "feat",
        "target_branch": "main",
        "author": {"username": "alice"},
    }
    asyncio.run(
        revise_executor._notify_revise_success(
            conn,
            settings,
            session,
            mr_full,
            "deadbeef1234",
            2,
            [{"reason": "모듈 경계상 반영 불가"}],
            summary="버퍼 크기 상수화",
            diff_stat="buffer.py | 3 +++",
            compare_url="https://gitlab.example.com/compare",
        )
    )
    conn.close()

    (upload,) = uploads
    assert upload["thread_ts"] == "999.888"  # the new round message, not the old one
    assert upload["filename"] == "report-group-project-7-r2-deadbeef.html"
    archived = tmp_path / "reports" / upload["filename"]
    assert archived.exists()
    html_text = archived.read_text(encoding="utf-8")
    assert "라운드 2" in html_text
    assert "buffer.py | 3 +++" in html_text
    assert "모듈 경계상 반영 불가" in html_text

def test_handle_mr_open_renders_archives_and_uploads_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spy_upload: dict[str, Any]
) -> None:
    from app.ai_reviewer import MRReview
    from app.config import get_settings
    from app.db import get_connection, init_db

    settings = get_settings()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_channel_id", CHANNEL)
    monkeypatch.setattr(settings, "action_token_secret", "secret")
    monkeypatch.setattr(settings, "reviewer_map", '{"group/project": "U1"}')
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "gitlab_token", "glpat-test")
    monkeypatch.setattr(settings, "report_html_dir", str(tmp_path / "reports"))

    review = MRReview(summary="요약", key_changes=["변경"], points_to_watch=["주의"])

    async def fake_build(mr, settings):  # noqa: ANN001
        return review, {"files": [], "contents": {}, "files_truncated": False}

    monkeypatch.setattr(ingest, "_build_ai_review", fake_build)

    async def fake_post(self, channel, mr, token, review=None):  # noqa: ANN001
        return {"channel": CHANNEL, "ts": THREAD_TS}

    monkeypatch.setattr(SlackClient, "post_mr_message", fake_post)

    conn = get_connection(settings.db_path)
    init_db(conn)
    try:
        result = asyncio.run(
            ingest.handle_mr_open(
                settings,
                conn,
                project_id="918",
                repo_slug="group/project",
                mr_iid=7,
                sha="deadbeef1234",
                title="Fix",
                url="https://gitlab.example.com/mr/7",
                source_branch="feat",
                target_branch="main",
                actor="alice",
                source="poller",
            )
        )
    finally:
        conn.close()

    assert result["notified"] is True
    # The HTML archive exists on disk under the configured directory.
    reports = list((tmp_path / "reports").glob("report-group-project-7-*.html"))
    assert len(reports) == 1
    html_text = reports[0].read_text(encoding="utf-8")
    assert "요약" in html_text
    # And the upload reached the notification's thread.
    complete = spy_upload["calls"][-1]["payload"]
    assert complete["thread_ts"] == THREAD_TS


def test_report_upload_failure_never_fails_the_notification(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from app.ai_reviewer import MRReview
    from app.config import get_settings
    from app.db import get_connection, init_db

    settings = get_settings()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_channel_id", CHANNEL)
    monkeypatch.setattr(settings, "action_token_secret", "secret")
    monkeypatch.setattr(settings, "reviewer_map", '{"group/project": "U1"}')
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "gitlab_token", "glpat-test")
    monkeypatch.setattr(settings, "report_html_dir", str(tmp_path / "reports"))

    review = MRReview(summary="요약", key_changes=[], points_to_watch=[])

    async def fake_build(mr, settings):  # noqa: ANN001
        return review, None

    monkeypatch.setattr(ingest, "_build_ai_review", fake_build)

    async def fake_post(self, channel, mr, token, review=None):  # noqa: ANN001
        return {"channel": CHANNEL, "ts": THREAD_TS}

    monkeypatch.setattr(SlackClient, "post_mr_message", fake_post)

    async def exploding_upload(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError("files API down")

    monkeypatch.setattr(SlackClient, "upload_report_file", exploding_upload)

    conn = get_connection(settings.db_path)
    init_db(conn)
    try:
        result = asyncio.run(
            ingest.handle_mr_open(
                settings,
                conn,
                project_id="918",
                repo_slug="group/project",
                mr_iid=7,
                sha="deadbeef1234",
                title="Fix",
                url="https://gitlab.example.com/mr/7",
                source_branch="feat",
                target_branch="main",
                actor="alice",
                source="poller",
            )
        )
    finally:
        conn.close()

    # The notification went out; the report failure was absorbed.
    assert result["notified"] is True

