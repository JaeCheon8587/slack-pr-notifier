"""Slack Socket Mode inbound path — an outbound-only WebSocket connection used
to receive [승인]/[의견] button and modal interactions when Slack -> this host
inbound HTTP is blocked by network policy (same class of constraint as
``app.gitlab_poller``'s polling path for the GitLab side; see that module's
docstring for the measured failure mode this mirrors).

This module never re-implements interaction-handling policy: every incoming
request is forwarded, unchanged, to the exact same shared entry point the
HTTP route already uses — ``app.slack_dispatch.dispatch_interaction`` (see
that module's docstring: "the shared ``dispatch_interaction`` entry point ...
a future Socket Mode caller with no HTTP response to attach it to"). This
module is intentionally thin, mirroring ``app.slack_actions``'s own division
of labour: transport plumbing here, all approval/opinion-rail policy in
``app.slack_dispatch`` (never imported by this module beyond that one entry
point, and never modified by this change).

Ack-then-dispatch: Slack requires every Socket Mode envelope to be
acknowledged (``send_socket_mode_response``, keyed by ``envelope_id``) within
a ~3s budget. ``dispatch_interaction``'s own GitLab/Slack API calls can run
well past that (the same reason the HTTP route defers its merge-status poll
via ``BackgroundTasks`` — see ``app.slack_dispatch``'s docstring), and there
is no HTTP response here to attach a background task to, so the listener
below always acks *first* (an empty payload — behaviourally identical to the
HTTP route's ``view_submission`` ``{}`` response, since this codebase's
``_handle_view_submission`` never returns a ``response_action`` payload) and
only then runs ``dispatch_interaction``.

``dispatch_interaction`` still declares ``background_tasks`` as an optional
keyword (``None`` by default), but this module never actually passes
``None`` for it: doing so would reach the [승인] rail's
``background_tasks.add_task(...)`` calls in ``app.slack_dispatch`` with
nothing to call ``add_task`` on (``AttributeError``), silently dropping the
post-merge status poll. Instead, every request gets its own
``_SocketBackgroundTasks`` (below) — an ``add_task``-compatible shim that
only *records* each call rather than running it immediately. That matters
because ``asyncio.run`` below builds and tears down a brand-new event loop
for every single request: anything merely *scheduled* (e.g. via
``asyncio.create_task``) during ``dispatch_interaction`` and still pending
once it returns would be silently cancelled the moment that loop closes —
long before the poll's worst-case 3s x 10 = 30s could ever finish.
``_dispatch_and_run_background_tasks`` avoids exactly that by awaiting the
shim's recorded tasks itself, sequentially, right after
``dispatch_interaction`` returns — still inside the one coroutine
``asyncio.run`` is driving, so the loop only closes once every task has
actually finished.

Any exception ``dispatch_interaction`` raises — or that a background task it
scheduled raises (caught per-task inside ``_SocketBackgroundTasks.run_all``,
so one failing task never stops the rest) — is caught and logged right
here — one bad interaction must never take down the socket connection
(fail-soft, matching every other background-worker module in this codebase).

``start_socket_mode``/``stop_socket_mode`` mirror ``app.sweeper`` /
``app.gitlab_poller``'s ``start_x``/``stop_x`` daemon pattern: a module-level
client reference guarded by a lock (idempotent — a second
``start_socket_mode`` call while already connected is a no-op returning the
existing client), and strictly opt-in — ``start_socket_mode`` is a no-op
(returns ``None``, no connection attempted) unless
``settings.slack_socket_mode`` is True *and* ``settings.slack_app_token`` is
configured (same activation-gate shape as
``app.gitlab_poller.start_poller``'s ``poll_enabled`` + project-ids gate), so
a deployment that never sets these stays byte-for-byte identical to
HTTP-only behaviour. Unlike the sweeper/poller's own hand-rolled
``threading.Thread`` loop, ``slack_sdk``'s ``SocketModeClient.connect()``
already manages its own background threads (message receiver/processor,
auto-reconnect) internally, so there is no loop function to wrap here:
``start_socket_mode`` only builds the client, registers the one listener, and
calls ``connect()`` once; ``stop_socket_mode`` calls the client's own
``close()``. A connection failure is caught and logged (fail-soft), never
raised into the caller (``app.main``'s lifespan).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web import WebClient

from app.config import Settings, get_settings, secret_value
from app.slack_dispatch import dispatch_interaction

logger = logging.getLogger("uvicorn.error")

_socket_client: SocketModeClient | None = None
_lock = threading.Lock()


class _SocketBackgroundTasks:
    """``fastapi.BackgroundTasks``-compatible ``add_task`` shim for this
    Socket Mode path (see module docstring for why ``None`` cannot be passed
    to ``dispatch_interaction`` here instead).

    ``add_task`` mirrors ``BackgroundTasks.add_task``'s own signature but
    only *records* the call — it builds the coroutine eagerly (assuming an
    ``async def`` callable, true of both of this module's actual callees,
    ``app.slack_dispatch``'s ``_poll_merge_and_finalize`` and
    ``_notify_merge_manual``) without starting it. ``run_all`` — called by
    ``_dispatch_and_run_background_tasks`` only once ``dispatch_interaction``
    has already returned — awaits each one in registration order.
    """

    def __init__(self) -> None:
        self._coroutines: list[Any] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self._coroutines.append(func(*args, **kwargs))

    async def run_all(self) -> None:
        """Await every recorded task in turn; each one's own exception is
        caught and logged right here rather than cancelling the rest or
        escaping to the caller — fail-soft at the per-task level, matching
        ``_on_socket_mode_request``'s own listener-level guard (defense in
        depth, and what keeps a failed merge-status poll from ever taking
        the socket connection down)."""

        for coro in self._coroutines:
            try:
                await coro
            except Exception:
                logger.exception("Socket Mode: background task failed")


async def _dispatch_and_run_background_tasks(settings: Settings, payload: dict[str, Any]) -> None:
    """Run ``dispatch_interaction`` to completion, then run whatever it
    scheduled via ``_SocketBackgroundTasks.add_task`` (the [승인] rail's
    merge-status poll) — both awaited inside this one coroutine, the same
    one ``_on_socket_mode_request`` hands to ``asyncio.run`` (see module
    docstring for why finishing the poll before this coroutine returns is
    what keeps ``asyncio.run`` from tearing its loop down mid-poll)."""

    background_tasks = _SocketBackgroundTasks()
    await dispatch_interaction(settings, payload, source="socket", background_tasks=background_tasks)
    await background_tasks.run_all()


def _on_socket_mode_request(client: Any, request: SocketModeRequest, settings: Settings) -> None:
    """The one registered listener — ack first, then dispatch (see module docstring).

    Never raises: any exception from ``dispatch_interaction``, from a
    background task it scheduled, or from driving either's event loop, is
    caught and logged so a single bad interaction can never take down the
    socket connection or the SDK's calling worker thread
    (``BaseSocketModeClient.run_message_listeners`` already has its own outer
    per-listener try/except, but this inner guard is what keeps the
    *listener function itself* exception-free for direct unit testing and
    defense in depth).
    """

    client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
    try:
        asyncio.run(_dispatch_and_run_background_tasks(settings, request.payload))
    except Exception:
        logger.exception("Socket Mode: dispatch_interaction failed (envelope_id=%s)", request.envelope_id)


def start_socket_mode(settings: Settings | None = None) -> SocketModeClient | None:
    """Connect the Socket Mode client (idempotent, fail-soft — see module docstring).

    Returns ``None`` without attempting a connection unless
    ``settings.slack_socket_mode`` is True *and* ``settings.slack_app_token``
    is set; also returns ``None`` (logged, never raised) if the connection
    attempt itself fails.
    """

    global _socket_client
    settings = settings or get_settings()
    if not settings.slack_socket_mode or not settings.slack_app_token:
        logger.warning(
            "Socket Mode: not started (slack_socket_mode=%s, slack_app_token_set=%s)",
            settings.slack_socket_mode,
            bool(settings.slack_app_token),
        )
        return None

    with _lock:
        if _socket_client is not None:
            return _socket_client
        try:
            web_client = WebClient(token=secret_value(settings.slack_bot_token))
            client = SocketModeClient(app_token=secret_value(settings.slack_app_token), web_client=web_client)
            client.socket_mode_request_listeners.append(lambda c, req: _on_socket_mode_request(c, req, settings))
            client.connect()
        except Exception:
            logger.exception("Socket Mode: failed to start")
            return None

        _socket_client = client
        logger.info("Socket Mode: connected (interactions now received over WebSocket)")
        return _socket_client


def stop_socket_mode() -> None:
    """Close the Socket Mode client, if one was started (fail-soft, idempotent;
    used at app shutdown)."""

    global _socket_client
    with _lock:
        client = _socket_client
        _socket_client = None

    if client is None:
        return
    try:
        client.close()
    except Exception:
        logger.exception("Socket Mode: error while closing")
