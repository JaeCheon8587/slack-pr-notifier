from fastapi import FastAPI

from app.config import get_settings
from app.github_webhook import router as github_webhook_router
from app.slack_actions import router as slack_actions_router

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
)
app.include_router(github_webhook_router)
app.include_router(slack_actions_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Return the middleware health status."""

    return {
        "status": "ok",
        "environment": settings.app_env,
    }
