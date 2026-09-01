from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import gitlab_poller, notify_queue, slack_socket, sweeper
from app.config import get_settings
from app.gitlab_webhook import router as gitlab_webhook_router
from app.slack_actions import router as slack_actions_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the P6 timeout sweeper + Slack notify outbox worker + the P.2
    GitLab MR poller + the Slack Socket Mode client at app startup, and stop
    them cleanly at shutdown (uvicorn --workers 1, daemon threads/fail-soft
    connections so a hard process kill never blocks on them). The poller
    (settings.poll_enabled) and the Socket Mode client
    (settings.slack_socket_mode + settings.slack_app_token) are both opt-in —
    start_poller/start_socket_mode are no-ops when their own settings gate is
    off, so both calls are safe to make unconditionally."""

    sweeper.start_sweeper(settings)
    notify_queue.start_worker(settings)
    gitlab_poller.start_poller(settings)
    slack_socket.start_socket_mode(settings)
    try:
        yield
    finally:
        slack_socket.stop_socket_mode()
        gitlab_poller.stop_poller()
        notify_queue.stop_worker()
        sweeper.stop_sweeper()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(gitlab_webhook_router)
app.include_router(slack_actions_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Return the middleware health status."""

    return {
        "status": "ok",
        "environment": settings.app_env,
    }
