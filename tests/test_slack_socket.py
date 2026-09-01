"""Tests for the Slack Socket Mode inbound path (app/slack_socket.py).

Drives ``start_socket_mode``/``stop_socket_mode`` against a hand-written
``FakeSocketModeClient`` (monkeypatched in place of ``slack_sdk``'s real
``SocketModeClient`` — no real network/WebSocket I/O), and exercises the one
registered listener directly (grabbed off the fake client's
``socket_mode_request_listeners`` list) with real ``SocketModeRequest``
objects. ``app.slack_dispatch.dispatch_interaction`` itself is monkeypatched
to a call-recording fake in every test below — this module's own contract is
"forward to dispatch_interaction unchanged", not dispatch_interaction's
internal rail logic (already covered by tests/test_slack_actions.py).
"""

from __future__ import annotations

from typing import Any

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

import app.slack_socket as slack_socket
from app.config import get_settings


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeSocketModeClient:
    """Duck-typed ``slack_sdk.socket_mode.SocketModeClient`` replacement.

    Constructed exactly the way ``start_socket_mode`` builds the real client
    (keyword ``app_token=``/``web_client=``), and records ``connect``/
    ``close``/ack calls instead of doing any real network I/O.
    """

    def __init__(self, app_token: str, web_client: Any) -> None:
        self.app_token = app_token
        self.web_client = web_client
        self.socket_mode_request_listeners: list[Any] = []
        self.connected = False
        self.closed = False
        self.acks: list[str] = []

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def send_socket_mode_response(self, response: Any) -> None:
        self.acks.append(response.envelope_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_socket_client_global():
    """The module-level ``_socket_client`` is process-global state — reset it
    around every test so tests never see another test's connected client."""

    slack_socket._socket_client = None
    yield
    slack_socket._socket_client = None


def _enable(monkeypatch):  # type: ignore[no-untyped-def]
    """Turn Socket Mode on and swap in ``FakeSocketModeClient``."""

    settings = get_settings()
    monkeypatch.setattr(settings, "slack_socket_mode", True)
    monkeypatch.setattr(settings, "slack_app_token", "xapp-test-token")
    monkeypatch.setattr(slack_socket, "SocketModeClient", FakeSocketModeClient)
    return settings


# ---------------------------------------------------------------------------
# Activation gate
# ---------------------------------------------------------------------------
def test_start_socket_mode_returns_none_when_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_socket_mode", False)
    monkeypatch.setattr(settings, "slack_app_token", "xapp-test-token")
    monkeypatch.setattr(slack_socket, "SocketModeClient", FakeSocketModeClient)

    assert slack_socket.start_socket_mode(settings) is None


def test_start_socket_mode_returns_none_without_app_token(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_socket_mode", True)
    monkeypatch.setattr(settings, "slack_app_token", None)
    monkeypatch.setattr(slack_socket, "SocketModeClient", FakeSocketModeClient)

    assert slack_socket.start_socket_mode(settings) is None


def test_start_socket_mode_connects_and_is_idempotent(monkeypatch):
    settings = _enable(monkeypatch)

    client = slack_socket.start_socket_mode(settings)

    assert isinstance(client, FakeSocketModeClient)
    assert client.connected is True
    assert len(client.socket_mode_request_listeners) == 1

    again = slack_socket.start_socket_mode(settings)
    assert again is client
    assert len(client.socket_mode_request_listeners) == 1  # not re-registered


# ---------------------------------------------------------------------------
# Listener behaviour
# ---------------------------------------------------------------------------
def test_listener_acks_before_dispatch(monkeypatch):
    settings = _enable(monkeypatch)
    order: list[str] = []

    class RecordingFakeClient(FakeSocketModeClient):
        def send_socket_mode_response(self, response: Any) -> None:
            order.append("ack")
            super().send_socket_mode_response(response)

    monkeypatch.setattr(slack_socket, "SocketModeClient", RecordingFakeClient)

    async def fake_dispatch(settings_arg, payload, *, source="http", background_tasks=None):
        order.append("dispatch")
        return {}

    monkeypatch.setattr(slack_socket, "dispatch_interaction", fake_dispatch)

    client = slack_socket.start_socket_mode(settings)
    listener = client.socket_mode_request_listeners[0]
    request = SocketModeRequest(type="interactive", envelope_id="E1", payload={"type": "block_actions"})

    listener(client, request)

    assert order == ["ack", "dispatch"]


def test_listener_dispatches_with_payload_and_source_socket(monkeypatch):
    settings = _enable(monkeypatch)
    calls: list[tuple[Any, Any, Any, Any]] = []

    async def fake_dispatch(settings_arg, payload, *, source="http", background_tasks=None):
        calls.append((settings_arg, payload, source, background_tasks))
        return {}

    monkeypatch.setattr(slack_socket, "dispatch_interaction", fake_dispatch)

    client = slack_socket.start_socket_mode(settings)
    listener = client.socket_mode_request_listeners[0]
    payload = {"type": "block_actions", "actions": [{"action_id": "approve_mr", "value": "tok"}]}
    request = SocketModeRequest(type="interactive", envelope_id="E2", payload=payload)

    listener(client, request)

    assert len(calls) == 1
    called_settings, called_payload, called_source, called_background_tasks = calls[0]
    assert called_settings is settings
    assert called_payload == payload
    assert called_source == "socket"
    # Regression: this must never be None (see app.slack_socket's module
    # docstring) — passing None straight through would crash the [승인]
    # rail's background_tasks.add_task(...) call with AttributeError,
    # silently dropping the merge-status poll.
    assert called_background_tasks is not None
    assert hasattr(called_background_tasks, "add_task")
    assert client.acks == ["E2"]


def test_listener_swallows_dispatch_exception(monkeypatch):
    settings = _enable(monkeypatch)

    async def raising_dispatch(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(slack_socket, "dispatch_interaction", raising_dispatch)

    client = slack_socket.start_socket_mode(settings)
    listener = client.socket_mode_request_listeners[0]
    request = SocketModeRequest(type="interactive", envelope_id="E3", payload={"type": "block_actions"})

    listener(client, request)  # must not raise

    assert client.acks == ["E3"]  # the ack still happened before the failing dispatch


def test_listener_handles_view_submission_payload(monkeypatch):
    settings = _enable(monkeypatch)
    calls: list[Any] = []

    async def fake_dispatch(settings_arg, payload, *, source="http", background_tasks=None):
        calls.append(payload)
        return {}

    monkeypatch.setattr(slack_socket, "dispatch_interaction", fake_dispatch)

    client = slack_socket.start_socket_mode(settings)
    listener = client.socket_mode_request_listeners[0]
    payload = {
        "type": "view_submission",
        "view": {
            "callback_id": "opinion_submission",
            "private_metadata": "signed-token",
            "state": {"values": {}},
        },
        "user": {"id": "U1"},
    }
    request = SocketModeRequest(type="interactive", envelope_id="E4", payload=payload)

    listener(client, request)

    assert calls == [payload]
    assert client.acks == ["E4"]


# ---------------------------------------------------------------------------
# Background-task execution (bug fix regression: dispatch_interaction's
# background_tasks must not be None on this path — the [승인] rail's
# merge-status poll relies on background_tasks.add_task(...) actually being
# callable *and* actually running before this listener call returns).
# ---------------------------------------------------------------------------
def test_listener_actually_executes_background_tasks_scheduled_during_dispatch(monkeypatch):
    """A real ``add_task`` call made *during* ``dispatch_interaction`` (as the
    [승인] rail's merge-status poll does — see ``app.slack_dispatch``'s
    ``_handle_approve_click``) must actually run, not merely avoid crashing.
    This reproduces the merge-poll-never-runs bug directly, without needing
    the full approve rail (already covered end-to-end by
    ``tests/test_slack_actions.py`` — see this file's own docstring for why
    ``dispatch_interaction`` stays a monkeypatched fake here)."""

    settings = _enable(monkeypatch)
    poll_calls: list[str] = []

    async def fake_poll(marker: str) -> None:
        poll_calls.append(marker)

    async def fake_dispatch(settings_arg, payload, *, source="http", background_tasks=None):
        background_tasks.add_task(fake_poll, "merge-poll")
        return {"accepted": True, "action": "approve_in_progress"}

    monkeypatch.setattr(slack_socket, "dispatch_interaction", fake_dispatch)

    client = slack_socket.start_socket_mode(settings)
    listener = client.socket_mode_request_listeners[0]
    payload = {"type": "block_actions", "actions": [{"action_id": "approve_mr", "value": "tok"}]}
    request = SocketModeRequest(type="interactive", envelope_id="E5", payload=payload)

    listener(client, request)

    assert poll_calls == ["merge-poll"]
    assert client.acks == ["E5"]


def test_listener_survives_background_task_exception_and_still_runs_the_rest(monkeypatch):
    """A background task's own exception must neither kill the listener nor
    stop any other task scheduled in the same dispatch — fail-soft at the
    per-task level (``_SocketBackgroundTasks.run_all``), not merely because
    the whole coroutine happened to get caught upstream."""

    settings = _enable(monkeypatch)
    ran: list[str] = []

    async def raising_task() -> None:
        raise RuntimeError("poll boom")

    async def ok_task() -> None:
        ran.append("ok_task")

    async def fake_dispatch(settings_arg, payload, *, source="http", background_tasks=None):
        background_tasks.add_task(raising_task)
        background_tasks.add_task(ok_task)
        return {"accepted": True, "action": "approve_in_progress"}

    monkeypatch.setattr(slack_socket, "dispatch_interaction", fake_dispatch)

    client = slack_socket.start_socket_mode(settings)
    listener = client.socket_mode_request_listeners[0]
    request = SocketModeRequest(type="interactive", envelope_id="E6", payload={"type": "block_actions"})

    listener(client, request)  # must not raise

    assert client.acks == ["E6"]
    assert ran == ["ok_task"]


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
def test_stop_socket_mode_closes_and_resets(monkeypatch):
    settings = _enable(monkeypatch)
    client = slack_socket.start_socket_mode(settings)
    assert client.closed is False

    slack_socket.stop_socket_mode()

    assert client.closed is True
    assert slack_socket._socket_client is None

    slack_socket.stop_socket_mode()  # no-op the second time — must not raise


def test_stop_socket_mode_noop_when_never_started():
    slack_socket.stop_socket_mode()  # must not raise
