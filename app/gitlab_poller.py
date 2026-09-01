"""GitLab MR poller (P.2 폴링 수집 경로) — outbound-only MR-event detection for
environments where inbound GitLab webhooks are blocked by network/firewall
policy (see .orchestration/ledgers/20260730-polling-ingestion.md for the
measured failure: GitLab -> this host TCP connect times out, while every
outbound call — GitLab API, Slack, Claude — succeeds normally).

This module never re-implements event-handling policy: it only *detects*
that an MR-lifecycle event must have happened (by diffing GitLab's current
state against ``review_session``) and then calls the exact same shared
handler app.ingest already exposes to the webhook rail (handle_mr_open /
handle_human_push / handle_external_close), passing ``source="poller"``. All
``review_session.status`` changes therefore still go exclusively through
``app.state_machine.cas_transition`` (inside app.ingest) — this module never
writes the ``status`` column directly, and never duplicates ingest's guard
logic.

Concurrency / duplicate-safety note (CHANGE SPEC [5]): the poller and the
webhook rail (app.gitlab_webhook) may both observe and react to the very same
GitLab event (e.g. both see a push, or both see a merge) if a deployment ever
runs with webhooks *and* polling enabled side by side. This is safe by
construction, not by any polling-side lock:
  - every ``status`` transition is a CAS (``UPDATE ... WHERE status = ?``), so
    whichever caller's transition lands first wins and the second one's
    ``rowcount`` is 0 (silently ignored, per app.state_machine / app.ingest);
  - the poller's own event detection (session missing / sha differs / active
    status vs. GitLab's true state) is re-derived from the live DB and live
    GitLab state on every pass, never from a locally cached "seen" set — so
    two back-to-back ``poll_once`` calls over an unchanged (DB, GitLab) state
    detect the same zero events (CHANGE SPEC [4]), and a webhook delivery that
    lands between two polls simply makes the *next* poll's diff come up empty
    for that MR instead of double-processing it.

Reopen coverage (event-coverage hardening): a session sitting in ``manual``
whose MR GitLab now reports back as ``opened`` is only ever auto-resumed
(CAS ``manual`` -> ``reviewing``, reason ``mr_reopened``) when that session's
own most recent ``event_log`` row is the ``external_close`` transition — i.e.
it went ``manual`` *because* the MR was closed externally, not for any other
reason. A session that reached ``manual`` via ``guard_reject`` (no reviewer
mapping), ``human_push``/``push_during_merge`` (a person pushed while
revising/merging), or a revise failure (``revise_attempts_exceeded``,
``revise_timeout``) is never revived here even though GitLab still (or again)
reports the MR as open — reviving those would recreate the exact "매 주기
되살아나는 무한 알림" incident this guard exists to prevent, since none of
those other manual reasons imply the MR was ever actually closed and
reopened. See ``_is_external_close_manual`` below.

``poll_once`` is a pure, fully-awaitable function taking an explicit
``client`` (a ``GitLabClient``, or any duck-typed fake exposing the same three
async methods) — unit-testable without real HTTP or real sleeps. The
background thread wrapper below (``start_poller``/``stop_poller``) is a thin
periodic caller around it, mirroring app.sweeper's daemon-thread pattern.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from typing import Any

from app import ingest
from app.config import Settings, get_settings, secret_value
from app.db import get_connection, init_db
from app.gitlab_client import GitLabClient
from app.state_machine import MANUAL, MERGING, REVIEWING, REVISING, cas_transition

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# [1] poll_once — pure detection + dispatch pass
# ---------------------------------------------------------------------------
async def poll_once(settings: Settings, conn: sqlite3.Connection, client: GitLabClient) -> dict[str, int]:
    """Run one polling pass over every configured target project.

    For each ``settings.poll_project_ids_parsed`` project id:
      (a) list GitLab's currently ``state=opened`` MRs;
      (b) for each of those MRs, diff against ``review_session``:
          - no session -> ``ingest.handle_mr_open`` ("opened");
          - session exists and its status is ``manual`` -> auto-resume it
            ("reopened") iff the session's manual status was itself caused by
            an external close (see ``_is_external_close_manual`` / the module
            docstring's reopen-coverage note) — otherwise a deliberate no-op;
          - otherwise, if GitLab's head sha differs from ``session.mr_sha`` ->
            check the newest commit's author (GitLab commits API) and, unless
            it is the middleware's own bot account, -> ``ingest.handle_human_push``
            ("pushed");
      (c) for every DB session active in this project (status in
          reviewing/revising/merging) that was *not* seen in (a)'s opened
          list, fetch that one MR directly to learn whether it is now
          ``merged`` ("merged") or ``closed`` ("closed") -> ``ingest.handle_external_close``.

    Returns ``{"opened": n, "reopened": n, "pushed": n, "merged": n, "closed": n}``
    — how many times each event kind was dispatched this pass (logged as one
    summary line), not a guarantee that every call produced a state change:
    ingest's own CAS/status branching already governs that (see the module
    docstring's concurrency note).

    A failure anywhere (listing a project, processing one MR, or resolving
    one external-close candidate) is caught, logged, and skipped — it never
    aborts the rest of the pass or propagates out of ``poll_once``.
    """

    counts = {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}
    project_ids = settings.poll_project_ids_parsed
    for project_id in project_ids:
        project_counts = await _poll_project(settings, conn, client, project_id)
        for key in counts:
            counts[key] += project_counts[key]

    logger.info(
        "Poller: pass complete — projects=%d opened=%d reopened=%d pushed=%d merged=%d closed=%d",
        len(project_ids),
        counts["opened"],
        counts["reopened"],
        counts["pushed"],
        counts["merged"],
        counts["closed"],
    )
    return counts


async def _poll_project(
    settings: Settings, conn: sqlite3.Connection, client: GitLabClient, project_id: str
) -> dict[str, int]:
    counts = {"opened": 0, "reopened": 0, "pushed": 0, "merged": 0, "closed": 0}

    try:
        opened_mrs = await client.list_merge_requests(project_id, state="opened")
    except Exception:
        logger.exception(
            "Poller: listing opened MRs failed (project=%s) — skipping this project this cycle",
            project_id,
        )
        return counts

    opened_iids: set[Any] = set()
    for raw_mr in opened_mrs:
        mr_iid = raw_mr.get("iid")
        if mr_iid is None:
            continue
        opened_iids.add(mr_iid)
        try:
            kind = await _process_open_mr(settings, conn, client, project_id, raw_mr)
        except Exception:
            logger.exception(
                "Poller: MR !%s (project=%s) processing failed — skipped this cycle", mr_iid, project_id
            )
            continue
        if kind is not None:
            counts[kind] += 1

    try:
        close_counts = await _process_external_closes(settings, conn, client, project_id, opened_iids)
        counts["merged"] += close_counts["merged"]
        counts["closed"] += close_counts["closed"]
    except Exception:
        logger.exception("Poller: external-close scan failed (project=%s) — skipped this cycle", project_id)

    return counts


def _project_id_int(project_id: str) -> int | None:
    """Convert the poller's configured project id (a string --
    ``settings.poll_project_ids_parsed``) to ``int`` for an ``ingest.*`` call.

    ``app.slack_dispatch._decode_mr`` requires the signed action token's
    ``project_id`` to be ``int`` (isinstance check) -- the webhook rail
    already hands ``ingest`` an int (JSON payload's ``project.id``), so this
    normalizes the poller's own string-typed config value the same way (see
    ``app.ingest._normalize_id_for_token``, its identical counterpart on the
    ingest side).

    Deliberately left as its own conversion at each ``ingest.*`` call site
    below rather than done once upfront in ``poll_once``/``_poll_project`` --
    ``project_id`` must stay a ``str`` for every GitLab-client call in this
    module (``list_merge_requests``/``get_merge_request``/``list_mr_commits``
    already accept ``int | str``, but changing what is actually passed here
    would ripple into every fixture keyed by the configured string).

    Returns ``None`` (never raises) if ``project_id`` cannot be parsed as an
    int; callers log a warning identifying the skipped project and skip only
    that dispatch, never the rest of the poll pass.
    """

    try:
        return int(project_id)
    except (TypeError, ValueError):
        return None


async def _process_open_mr(
    settings: Settings,
    conn: sqlite3.Connection,
    client: GitLabClient,
    project_id: str,
    raw_mr: dict[str, Any],
) -> str | None:
    """Diff one currently-open GitLab MR against its review_session.

    Returns "opened", "reopened", "pushed", or None (no event: a manual
    session ineligible for auto-resume, session already reflects GitLab's
    current head sha, or the sha-diff was the middleware's own bot commit —
    see the module docstring's idempotency/reopen-coverage notes).
    """

    mr_iid = raw_mr.get("iid")
    head_sha = raw_mr.get("sha")
    session = ingest._find_session(conn, project_id, mr_iid)

    if session is None:
        await _dispatch_mr_open(settings, conn, project_id, raw_mr)
        return "opened"

    if session["status"] == MANUAL:
        return await _process_reopen(settings, conn, project_id, raw_mr, session)

    if not head_sha or head_sha == session["mr_sha"]:
        return None

    commit_author, commit_author_email = await _latest_commit_author(client, project_id, mr_iid)
    if ingest._is_bot_actor(settings, commit_author, commit_author_email):
        logger.info(
            "Poller: push ignored (bot actor echo): project=%s mr=%s sha=%s", project_id, mr_iid, head_sha
        )
        return None

    project_id_int = _project_id_int(project_id)
    if project_id_int is None:
        logger.warning(
            "Poller: project id %r is not a valid integer -- skipping this project this cycle",
            project_id,
        )
        return None

    await ingest.handle_human_push(
        settings,
        conn,
        project_id=project_id_int,
        mr_iid=mr_iid,
        new_sha=head_sha,
        commit_author=commit_author,
        commit_author_email=commit_author_email,
        source="poller",
    )
    return "pushed"


async def _dispatch_mr_open(
    settings: Settings, conn: sqlite3.Connection, project_id: str, raw_mr: dict[str, Any]
) -> dict[str, Any] | None:
    """Shared ``ingest.handle_mr_open`` call-site for both a brand-new MR and a
    reopen re-notification — same payload shape, only the caller differs.

    Returns ``None`` (no ``ingest.handle_mr_open`` call made) if ``project_id``
    cannot be normalized to int (see ``_project_id_int``) -- both callers
    already treat this function's return value as fire-and-forget.
    """

    project_id_int = _project_id_int(project_id)
    if project_id_int is None:
        logger.warning(
            "Poller: project id %r is not a valid integer -- skipping this project this cycle",
            project_id,
        )
        return None

    author = raw_mr.get("author")
    return await ingest.handle_mr_open(
        settings,
        conn,
        project_id=project_id_int,
        repo_slug=_repo_slug(raw_mr),
        mr_iid=raw_mr.get("iid"),
        sha=raw_mr.get("sha"),
        title=raw_mr.get("title"),
        url=raw_mr.get("web_url"),
        source_branch=raw_mr.get("source_branch"),
        target_branch=raw_mr.get("target_branch"),
        actor=author.get("username") if isinstance(author, dict) else None,
        source="poller",
    )


# ---------------------------------------------------------------------------
# [1] reopen detection — the anti-infinite-notification guard
# ---------------------------------------------------------------------------
# Must match the literal ``reason`` app.ingest.handle_external_close passes to
# ``cas_transition`` for an ``action="close"`` edge (see app/ingest.py's
# handle_external_close) — that reason string is what lands in
# ``event_log.kind`` (app.state_machine.cas_transition writes
# ``kind = reason or f"{from_status}->{to_status}"``), which is exactly what
# ``_is_external_close_manual`` below reads back.
_EXTERNAL_CLOSE_REASON = "external_close"


def _is_external_close_manual(conn: sqlite3.Connection, session_id: int) -> bool:
    """True iff ``session_id``'s most recent event_log row is the
    external-close transition (i.e. the session went ``manual`` *because* the
    MR was closed externally, and nothing has happened to it since).

    No schema change: this reuses ``event_log`` exactly as ingest/state_machine
    already write it. A session with no event_log row at all (should not
    happen — every transition into ``manual`` writes one) fails closed (False)
    rather than guessing.

    This is the single choke point that must reject every other manual
    reason (guard_reject, human_push, push_during_merge,
    revise_attempts_exceeded, revise_timeout, merge_poll_failed) — reviving
    any of those would recreate the "매 주기 되살아나는 무한 알림" incident.
    """

    row = conn.execute(
        "SELECT kind FROM event_log WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row is not None and row["kind"] == _EXTERNAL_CLOSE_REASON


async def _process_reopen(
    settings: Settings,
    conn: sqlite3.Connection,
    project_id: str,
    raw_mr: dict[str, Any],
    session: sqlite3.Row,
) -> str | None:
    """Auto-resume a ``manual`` session whose MR GitLab now reports as
    ``opened`` again — but only when it is eligible (see
    ``_is_external_close_manual``). Ineligible sessions (guard_reject/
    human_push/push_during_merge/revise-failure manual) are a deliberate
    no-op: they are left exactly as-is, every poll, forever — never revived.

    On success: CAS ``manual`` -> ``reviewing`` (reason ``mr_reopened``,
    logged to event_log by ``cas_transition`` itself), then re-runs
    ``ingest.handle_mr_open`` for the same session — which refreshes
    ``mr_sha`` to the current head and re-notifies Slack with a fresh
    [승인][의견] message (the same full re-notification a brand-new MR gets).
    Neither step touches ``round``/``revise_attempts`` (state_machine's CAS
    only ever writes status/updated_at/*_since columns) — deliberately
    preserved so the round cap cannot be bypassed by closing and reopening.
    """

    mr_iid = raw_mr.get("iid")
    if not _is_external_close_manual(conn, session["id"]):
        logger.info(
            "Poller: reopen ignored (manual session not from external_close): project=%s mr=%s session=%s",
            project_id,
            mr_iid,
            session["id"],
        )
        return None

    accepted = cas_transition(conn, session["id"], MANUAL, REVIEWING, reason="mr_reopened", detail="poller")
    if not accepted:
        # Lost the race (e.g. a concurrent resolution already moved the
        # session elsewhere) — per the CAS convention used everywhere else in
        # this module, this is a silent no-op, not an error.
        return None

    await _dispatch_mr_open(settings, conn, project_id, raw_mr)
    return "reopened"


async def _latest_commit_author(
    client: GitLabClient, project_id: str, mr_iid: Any
) -> tuple[str | None, str | None]:
    """Return (author_name, author_email) of the MR's newest commit.

    ``list_mr_commits`` is newest-first (see app.gitlab_client); an empty
    result (e.g. a transient empty response) is treated as "unknown author",
    which ``ingest._is_bot_actor`` conservatively resolves to *not* a bot —
    failing safe toward still notifying a human push rather than silently
    dropping one because the author could not be identified.
    """

    commits = await client.list_mr_commits(project_id, mr_iid)
    if not commits:
        return None, None
    latest = commits[0]
    return latest.get("author_name"), latest.get("author_email")


async def _process_external_closes(
    settings: Settings,
    conn: sqlite3.Connection,
    client: GitLabClient,
    project_id: str,
    opened_iids: set[Any],
) -> dict[str, int]:
    """Resolve every locally-active session not present in this pass's
    opened-MR list (b) to GitLab's true current state, and dispatch
    ``ingest.handle_external_close`` for the ones that turn out merged/closed.

    Returns ``{"merged": n, "closed": n}`` — how many ``handle_external_close``
    calls this pass dispatched for each action.
    """

    counts = {"merged": 0, "closed": 0}

    project_id_int = _project_id_int(project_id)
    if project_id_int is None:
        logger.warning(
            "Poller: project id %r is not a valid integer -- skipping this project this cycle",
            project_id,
        )
        return counts

    rows = conn.execute(
        "SELECT * FROM review_session WHERE project_id = ? AND status IN (?, ?, ?)",
        (str(project_id), REVIEWING, REVISING, MERGING),
    ).fetchall()

    for row in rows:
        mr_iid = row["mr_iid"]
        if mr_iid in opened_iids:
            continue  # still open per this pass — no external-close event

        try:
            detail = await client.get_merge_request(project_id, mr_iid)
        except Exception:
            logger.exception(
                "Poller: get_merge_request failed (project=%s mr=%s) — skipped this cycle",
                project_id,
                mr_iid,
            )
            continue

        state = detail.get("state")
        if state == "merged":
            action = "merge"
        elif state == "closed":
            action = "close"
        else:
            # e.g. "locked", or an unexpected/transient value — not a
            # recognized external-close action; leave the session untouched
            # this cycle rather than guess.
            continue

        await ingest.handle_external_close(
            settings,
            conn,
            project_id=project_id,
            mr_iid=mr_iid,
            title=detail.get("title") or row["repo_slug"],
            action=action,
            source="poller",
        )
        counts["merged" if action == "merge" else "closed"] += 1
    return counts


def _repo_slug(raw_mr: dict[str, Any]) -> str | None:
    """Best-effort ``group/project`` slug from a GitLab MR list/get response."""

    references = raw_mr.get("references")
    if isinstance(references, dict):
        full = references.get("full")
        if isinstance(full, str) and "!" in full:
            return full.rsplit("!", 1)[0]

    web_url = raw_mr.get("web_url")
    if isinstance(web_url, str) and web_url:
        slug = web_url.split("/-/")[0].split("//", 1)[-1].split("/", 1)[-1]
        return slug or None
    return None


# ---------------------------------------------------------------------------
# [2] Background thread wrapper — mirrors app.sweeper's daemon-thread pattern
# ---------------------------------------------------------------------------
_poller_thread: threading.Thread | None = None
_poller_lock = threading.Lock()
_stop_event = threading.Event()


def start_poller(settings: Settings | None = None) -> threading.Thread | None:
    """Start the periodic GitLab poller as a daemon thread (idempotent).

    A no-op (returns None, no thread spawned) unless ``settings.poll_enabled``
    is True *and* at least one project id is configured
    (``poll_project_ids_parsed``) — polling is strictly opt-in (CHANGE SPEC
    [3]), so a deployment that never sets these stays byte-for-byte identical
    to webhook-only behaviour.
    """

    global _poller_thread
    settings = settings or get_settings()
    if not settings.poll_enabled or not settings.poll_project_ids_parsed:
        return None
    with _poller_lock:
        if _poller_thread is None or not _poller_thread.is_alive():
            _stop_event.clear()
            _poller_thread = threading.Thread(
                target=_poller_loop, args=(settings,), name="gitlab-poller", daemon=True
            )
            _poller_thread.start()
        return _poller_thread


def stop_poller(timeout: float = 5.0) -> None:
    """Signal the poller loop to stop and join it (used at app shutdown)."""

    _stop_event.set()
    thread = _poller_thread
    if thread is not None:
        thread.join(timeout=timeout)


def _poller_loop(settings: Settings) -> None:
    while not _stop_event.is_set():
        try:
            client = GitLabClient(
                settings.gitlab_url, secret_value(settings.gitlab_token), verify_ssl=settings.gitlab_verify_ssl
            )
            conn = get_connection(settings.db_path)
            init_db(conn)
            try:
                asyncio.run(poll_once(settings, conn, client))
            finally:
                conn.close()
        except Exception:
            logger.exception("Poller: unhandled error during poll_once")
        _stop_event.wait(settings.poll_interval)
