"""Tests for the P.2 GitLab MR poller (app/gitlab_poller.py).

Drives ``poll_once(settings, conn, client)`` directly against a hand-written
``FakeClient`` (fixed/canned responses, no network) — the pure, fully
awaitable core shared by the background thread wrapper
(``start_poller``/``stop_poller``, not exercised here beyond its on/off gate,
mirroring tests/test_sweeper.py's choice to leave the daemon-thread mechanics
themselves untested at the unit level).

``app.ingest``'s three shared handlers (handle_mr_open / handle_human_push /
handle_external_close) are wrapped with call-recording "spies" that still
delegate to the real implementation, so both the call arguments (what the
poller detected) and the real DB side effects (session created / sha
touched / status transitioned via CAS) can be asserted directly — no
FakeSlackClient is needed since tests/conftest.py's autouse ``isolate_settings``
fixture already leaves slack_bot_token/action_token_secret/etc. unset, which
makes every ingest handler's own Slack-notification branch a safe, network-free
no-op while still performing its core session/CAS side effects.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.gitlab_poller as poller
import app.ingest as ingest
from app.config import get_settings
from app.db import get_connection, init_db
from app.state_machine import MANUAL, MERGED, MERGING, REVIEWING, REVISING, cas_transition

PROJECT_ID = "77"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeClient:
    """Duck-typed GitLabClient fake with fixed responses (no network).

    - ``opened_by_project``: project_id -> list of raw MR dicts, as returned
      by GitLab's ``state=opened`` list endpoint.
    - ``commits_by_iid``: mr_iid -> newest-first commit dicts, consumed by
      ``list_mr_commits`` (the poller's push-author check).
    - ``detail_by_iid``: mr_iid -> the dict ``get_merge_request`` returns
      (only consulted for sessions no longer present in the opened list).
    - ``raise_on``: a set of (method_name, key) pairs; a matching call raises
      instead of returning a canned value (exception-isolation tests).
    """

    def __init__(
        self,
        opened_by_project: dict[str, list[dict[str, Any]]] | None = None,
        commits_by_iid: dict[int, list[dict[str, Any]]] | None = None,
        detail_by_iid: dict[int, dict[str, Any]] | None = None,
        raise_on: set[tuple[str, Any]] | None = None,
    ) -> None:
        self.opened_by_project = opened_by_project or {}
        self.commits_by_iid = commits_by_iid or {}
        self.detail_by_iid = detail_by_iid or {}
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, Any, Any]] = []

    async def list_merge_requests(self, project_id, *, state="opened", per_page=100):  # noqa: ANN001
        self.calls.append(("list_merge_requests", project_id, state))
        if ("list_merge_requests", project_id) in self.raise_on:
            raise RuntimeError("boom: list_merge_requests")
        return list(self.opened_by_project.get(project_id, []))

    async def list_mr_commits(self, project_id, mr_iid):  # noqa: ANN001
        self.calls.append(("list_mr_commits", project_id, mr_iid))
        if ("list_mr_commits", mr_iid) in self.raise_on:
            raise RuntimeError("boom: list_mr_commits")
        return list(self.commits_by_iid.get(mr_iid, []))

    async def get_merge_request(self, project_id, mr_iid):  # noqa: ANN001
        self.calls.append(("get_merge_request", project_id, mr_iid))
        if ("get_merge_request", mr_iid) in self.raise_on:
            raise RuntimeError("boom: get_merge_request")
        return dict(self.detail_by_iid[mr_iid])


def _raw_mr(
    *,
    iid: int,
    sha: str,
    title: str = "Add x",
    source_branch: str = "feature",
    target_branch: str = "main",
    author: str = "alice",
) -> dict[str, Any]:
    return {
        "iid": iid,
        "sha": sha,
        "title": title,
        "web_url": f"https://gitlab.example.com/group/project/-/merge_requests/{iid}",
        "source_branch": source_branch,
        "target_branch": target_branch,
        "author": {"username": author},
    }


# ---------------------------------------------------------------------------
# Fixtures / DB helpers
# ---------------------------------------------------------------------------
def configure(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "poll_enabled", True)
    monkeypatch.setattr(settings, "poll_project_ids", PROJECT_ID)
    monkeypatch.setattr(settings, "poll_project_id", None)
    monkeypatch.setattr(settings, "poll_interval", 60)
    monkeypatch.setattr(settings, "bot_username", "revise-bot")
    monkeypatch.setattr(settings, "bot_email", "bot@example.com")
    return settings


def _conn(settings):  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    init_db(conn)
    return conn


def _insert_session(settings, *, mr_iid: int, sha: str, repo_slug: str = "group/project", status: str = REVIEWING) -> int:  # type: ignore[no-untyped-def]
    conn = _conn(settings)
    conn.execute(
        "INSERT INTO review_session (project_id, mr_iid, mr_sha, repo_slug) VALUES (?, ?, ?, ?)",
        (PROJECT_ID, mr_iid, sha, repo_slug),
    )
    conn.commit()
    session_id = conn.execute(
        "SELECT id FROM review_session WHERE project_id = ? AND mr_iid = ?", (PROJECT_ID, mr_iid)
    ).fetchone()["id"]

    if status == MERGING:
        assert cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    elif status != REVIEWING:
        raise ValueError(status)

    conn.close()
    return session_id


# Manual reasons only ever reachable via an intermediate reviewing -> revising
# ("opinion") hop first, matching how a real session actually reaches
# `revising` before any of these — see app/state_machine.py's ALLOWED_TRANSITIONS
# ((REVIEWING, MANUAL) only allows "external_close"/"guard_reject" directly).
_REVISING_ONLY_MANUAL_REASONS = {"human_push", "revise_timeout", "revise_attempts_exceeded"}


def _insert_manual_session(
    settings, *, mr_iid: int, sha: str, manual_reason: str, repo_slug: str = "group/project"
) -> int:  # type: ignore[no-untyped-def]
    """Create a session already sitting in ``manual``, reached via the exact
    CAS edge(s) production code uses for ``manual_reason`` — so the resulting
    ``event_log`` row matches what a real session's history looks like (see
    ``app.gitlab_poller._is_external_close_manual``, which reads that row back
    to decide whether a reopen may auto-resume it)."""

    session_id = _insert_session(settings, mr_iid=mr_iid, sha=sha, repo_slug=repo_slug)
    conn = _conn(settings)
    if manual_reason in _REVISING_ONLY_MANUAL_REASONS:
        assert cas_transition(conn, session_id, REVIEWING, REVISING, reason="opinion")
        assert cas_transition(conn, session_id, REVISING, MANUAL, reason=manual_reason, detail="test-setup")
    else:
        assert cas_transition(conn, session_id, REVIEWING, MANUAL, reason=manual_reason, detail="test-setup")
    conn.close()
    return session_id


def _session_row(settings, mr_iid: int):  # type: ignore[no-untyped-def]
    conn = _conn(settings)
    row = conn.execute(
        "SELECT * FROM review_session WHERE project_id = ? AND mr_iid = ?", (PROJECT_ID, mr_iid)
    ).fetchone()
    conn.close()
    return row


@pytest.fixture
def spies(monkeypatch):  # type: ignore[no-untyped-def]
    """Record every call to the three shared ingest handlers while still
    delegating to the real implementation (a "spy", not a stub) — so both
    "was it called, with what args" and the real DB side effects can be
    asserted."""

    calls: dict[str, list[dict[str, Any]]] = {"mr_open": [], "human_push": [], "external_close": []}
    real_mr_open = ingest.handle_mr_open
    real_human_push = ingest.handle_human_push
    real_external_close = ingest.handle_external_close

    async def spy_mr_open(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["mr_open"].append(kwargs)
        return await real_mr_open(*args, **kwargs)

    async def spy_human_push(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["human_push"].append(kwargs)
        return await real_human_push(*args, **kwargs)

    async def spy_external_close(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["external_close"].append(kwargs)
        return await real_external_close(*args, **kwargs)

    monkeypatch.setattr(ingest, "handle_mr_open", spy_mr_open)
    monkeypatch.setattr(ingest, "handle_human_push", spy_human_push)
    monkeypatch.setattr(ingest, "handle_external_close", spy_external_close)
    return calls


# ---------------------------------------------------------------------------
# [1]/[6] poll_once — event detection + dispatch
# ---------------------------------------------------------------------------
def test_new_mr_calls_handle_mr_open_once_and_creates_session(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    conn = _conn(settings)
    client = FakeClient(opened_by_project={PROJECT_ID: [_raw_mr(iid=1, sha="sha1")]})

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 1, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}
    assert len(spies["mr_open"]) == 1
    assert spies["mr_open"][0]["mr_iid"] == 1
    assert spies["mr_open"][0]["sha"] == "sha1"
    assert spies["mr_open"][0]["source"] == "poller"

    row = _session_row(settings, 1)
    assert row is not None
    assert row["status"] == REVIEWING
    assert row["mr_sha"] == "sha1"


def test_sha_change_with_human_commit_calls_handle_human_push(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _insert_session(settings, mr_iid=2, sha="sha-old")
    conn = _conn(settings)
    client = FakeClient(
        opened_by_project={PROJECT_ID: [_raw_mr(iid=2, sha="sha-new")]},
        commits_by_iid={
            2: [{"id": "sha-new", "author_name": "a-human", "author_email": "human@example.com"}]
        },
    )

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 0, "reopened": 0, "pushed": 1, "merged": 0, "closed": 0}
    assert len(spies["human_push"]) == 1
    assert spies["human_push"][0]["new_sha"] == "sha-new"
    assert spies["human_push"][0]["commit_author"] == "a-human"
    assert spies["human_push"][0]["source"] == "poller"

    row = _session_row(settings, 2)
    assert row["mr_sha"] == "sha-new"  # reviewing branch touches sha
    assert row["status"] == REVIEWING


def test_sha_change_with_bot_commit_does_not_call_handle_human_push(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)  # bot_username="revise-bot"
    _insert_session(settings, mr_iid=3, sha="sha-old")
    conn = _conn(settings)
    client = FakeClient(
        opened_by_project={PROJECT_ID: [_raw_mr(iid=3, sha="sha-new")]},
        commits_by_iid={
            3: [{"id": "sha-new", "author_name": "revise-bot", "author_email": "bot@example.com"}]
        },
    )

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}
    assert spies["human_push"] == []  # bot echo: never called

    row = _session_row(settings, 3)
    assert row["mr_sha"] == "sha-old"  # untouched
    assert row["status"] == REVIEWING


def test_active_session_merged_upstream_calls_external_close_merge(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _insert_session(settings, mr_iid=4, sha="sha1", status=MERGING)
    conn = _conn(settings)
    client = FakeClient(
        opened_by_project={PROJECT_ID: []},  # no longer reported as open
        detail_by_iid={4: {"state": "merged", "title": "Add x"}},
    )

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 1, "closed": 0}
    assert len(spies["external_close"]) == 1
    assert spies["external_close"][0]["action"] == "merge"
    assert spies["external_close"][0]["source"] == "poller"

    row = _session_row(settings, 4)
    assert row["status"] == MERGED


def test_active_session_closed_upstream_calls_external_close_close(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _insert_session(settings, mr_iid=5, sha="sha1", status=REVIEWING)
    conn = _conn(settings)
    client = FakeClient(
        opened_by_project={PROJECT_ID: []},
        detail_by_iid={5: {"state": "closed", "title": "Add y"}},
    )

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 1}
    assert len(spies["external_close"]) == 1
    assert spies["external_close"][0]["action"] == "close"

    row = _session_row(settings, 5)
    assert row["status"] == MANUAL


# ---------------------------------------------------------------------------
# [1]/[6] reopen coverage — the anti-infinite-notification guard: a `manual`
# session is only ever auto-resumed when its own most recent event_log row is
# the `external_close` transition; every other manual reason must be a
# permanent no-op here (see app/gitlab_poller.py's module docstring /
# _is_external_close_manual).
# ---------------------------------------------------------------------------
def test_manual_session_from_external_close_is_reopened_and_renotified(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _insert_manual_session(settings, mr_iid=30, sha="sha1", manual_reason="external_close")
    conn = _conn(settings)
    client = FakeClient(opened_by_project={PROJECT_ID: [_raw_mr(iid=30, sha="sha2")]})

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 0, "reopened": 1, "pushed": 0, "merged": 0, "closed": 0}
    assert len(spies["mr_open"]) == 1  # re-notified via the same handler a brand-new MR gets
    assert spies["mr_open"][0]["mr_iid"] == 30
    assert spies["mr_open"][0]["sha"] == "sha2"
    assert spies["mr_open"][0]["source"] == "poller"

    row = _session_row(settings, 30)
    assert row["id"] == session_id
    assert row["status"] == REVIEWING  # manual -> reviewing CAS accepted (reason="mr_reopened")
    assert row["mr_sha"] == "sha2"  # refreshed by handle_mr_open's own _touch_sha
    assert row["round"] == 0  # untouched — CAS only ever writes status/updated_at/*_since columns


def test_manual_session_from_guard_reject_is_never_reopened(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    """Regression guard: a session that went `manual` because it had no
    reviewer mapping (guard_reject) must never be revived just because
    GitLab still (or again) reports its MR as opened — reviving it would
    recreate the 무한 알림 incident this guard exists to prevent."""

    settings = configure(monkeypatch, tmp_path)
    _insert_manual_session(settings, mr_iid=31, sha="sha1", manual_reason="guard_reject")
    conn = _conn(settings)
    client = FakeClient(opened_by_project={PROJECT_ID: [_raw_mr(iid=31, sha="sha1")]})

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}
    assert spies["mr_open"] == []
    assert spies["human_push"] == []
    assert spies["external_close"] == []

    row = _session_row(settings, 31)
    assert row["status"] == MANUAL  # left exactly as-is
    assert row["mr_sha"] == "sha1"


def test_manual_session_from_revise_failure_is_never_reopened(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    """Same regression guard, for the other manual-inducing family: a session
    that exhausted its revise attempts (revise_attempts_exceeded) must also
    never be auto-resumed by the poller."""

    settings = configure(monkeypatch, tmp_path)
    _insert_manual_session(settings, mr_iid=32, sha="sha1", manual_reason="revise_attempts_exceeded")
    conn = _conn(settings)
    client = FakeClient(opened_by_project={PROJECT_ID: [_raw_mr(iid=32, sha="sha1")]})

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert counts == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}
    assert spies["mr_open"] == []

    row = _session_row(settings, 32)
    assert row["status"] == MANUAL


def test_reopen_then_second_poll_once_detects_zero_events(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    """[2] Reopen idempotency: once a manual/external_close session has been
    auto-resumed, an immediate second poll_once over the same (now unchanged)
    GitLab state must not re-fire anything — the session has already left
    `manual`, so the guard's own DB-state re-derivation (not a cached "seen"
    set) naturally reports zero events the second time."""

    settings = configure(monkeypatch, tmp_path)
    _insert_manual_session(settings, mr_iid=33, sha="sha1", manual_reason="external_close")
    conn = _conn(settings)
    client = FakeClient(opened_by_project={PROJECT_ID: [_raw_mr(iid=33, sha="sha1")]})

    first = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    assert first == {"opened": 0, "reopened": 1, "pushed": 0, "merged": 0, "closed": 0}

    second = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert second == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}
    assert len(spies["mr_open"]) == 1  # re-notified exactly once total, not once per pass

    row = _session_row(settings, 33)
    assert row["status"] == REVIEWING


# ---------------------------------------------------------------------------
# [4] idempotency — a second poll_once over unchanged (DB, GitLab) state
# detects zero events, across all three categories at once.
# ---------------------------------------------------------------------------
def test_second_poll_once_call_detects_zero_events(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _insert_session(settings, mr_iid=11, sha="sha-old")  # reviewing -> will be pushed
    _insert_session(settings, mr_iid=12, sha="sha1", status=MERGING)  # -> merged externally
    conn = _conn(settings)
    client = FakeClient(
        opened_by_project={
            PROJECT_ID: [_raw_mr(iid=10, sha="sha-a"), _raw_mr(iid=11, sha="sha-new")]
        },
        commits_by_iid={
            11: [{"id": "sha-new", "author_name": "a-human", "author_email": "human@example.com"}]
        },
        detail_by_iid={12: {"state": "merged", "title": "Add z"}},
    )

    first = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    assert first == {"opened": 1, "reopened": 0, "pushed": 1, "merged": 1, "closed": 0}

    second = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert second == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}
    # Each shared ingest handler fired exactly once total, across both passes.
    assert len(spies["mr_open"]) == 1
    assert len(spies["human_push"]) == 1
    assert len(spies["external_close"]) == 1


# ---------------------------------------------------------------------------
# [4] poll_once's return-count contract: exactly these five keys
# (opened/reopened/pushed/merged/closed), even on a pass where nothing at all
# happens — pinning the dict shape independently of any one event scenario.
# ---------------------------------------------------------------------------
def test_poll_once_counts_have_exactly_five_keys_when_nothing_happens(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    conn = _conn(settings)
    client = FakeClient(opened_by_project={PROJECT_ID: []})

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type]
    conn.close()

    assert set(counts.keys()) == {"opened", "reopened", "pushed", "merged", "closed"}
    assert counts == {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}


# ---------------------------------------------------------------------------
# [6] a GitLab API failure on one item never propagates out of poll_once, and
# only that one item is skipped — every other item still processes normally.
# ---------------------------------------------------------------------------
def test_gitlab_exception_for_one_item_does_not_propagate_or_block_others(monkeypatch, tmp_path, spies) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _insert_session(settings, mr_iid=20, sha="sha-old")
    conn = _conn(settings)
    client = FakeClient(
        opened_by_project={
            PROJECT_ID: [
                _raw_mr(iid=20, sha="sha-new"),  # commit-author lookup raises below
                _raw_mr(iid=21, sha="sha-b"),  # brand new — must still be processed
            ]
        },
        raise_on={("list_mr_commits", 20)},
    )

    counts = asyncio.run(poller.poll_once(settings, conn, client))  # type: ignore[arg-type] # must not raise
    conn.close()

    assert counts["opened"] == 1  # MR 21 processed fine
    assert counts["pushed"] == 0  # MR 20's push attempt failed and was skipped

    row20 = _session_row(settings, 20)
    assert row20["mr_sha"] == "sha-old"  # untouched — failed item skipped, not partially applied
    row21 = _session_row(settings, 21)
    assert row21 is not None


# ---------------------------------------------------------------------------
# [3] config parsing helper (poll_project_ids_parsed)
# ---------------------------------------------------------------------------
def test_poll_project_ids_parsed_comma_separated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "poll_project_ids", " 12, 918 ,")
    monkeypatch.setattr(settings, "poll_project_id", None)

    assert settings.poll_project_ids_parsed == ["12", "918"]


def test_poll_project_ids_parsed_falls_back_to_legacy_single_project(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "poll_project_ids", "")
    monkeypatch.setattr(settings, "poll_project_id", 42)

    assert settings.poll_project_ids_parsed == ["42"]


def test_poll_project_ids_parsed_empty_when_nothing_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "poll_project_ids", "")
    monkeypatch.setattr(settings, "poll_project_id", None)

    assert settings.poll_project_ids_parsed == []


# ---------------------------------------------------------------------------
# [2]/[3] start_poller's on/off gate (thread/network mechanics themselves are
# intentionally left untested here, matching tests/test_sweeper.py's choice
# not to exercise start_sweeper/stop_sweeper directly).
# ---------------------------------------------------------------------------
def test_start_poller_is_noop_when_disabled(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "poll_enabled", False)

    thread = poller.start_poller(settings)

    assert thread is None
    assert poller._poller_thread is None


def test_start_poller_is_noop_when_no_project_ids_configured(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "poll_project_ids", "")
    monkeypatch.setattr(settings, "poll_project_id", None)

    thread = poller.start_poller(settings)

    assert thread is None
    assert poller._poller_thread is None
