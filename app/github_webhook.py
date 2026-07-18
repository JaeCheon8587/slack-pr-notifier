import json
import logging
from json import JSONDecodeError
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.action_token import create_action_token
from app.config import Settings, get_settings
from app.security import verify_github_signature
from app.slack_client import SlackClient

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/github")
async def receive_github_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    github_signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> dict[str, Any]:
    """Receive and verify GitHub webhook events."""

    if not settings.github_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITHUB_WEBHOOK_SECRET is not configured",
        )

    body = await request.body()
    if not verify_github_signature(body, github_signature, settings.github_webhook_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature",
        )

    if github_event is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-GitHub-Event header is missing",
        )

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

    if github_event == "ping":
        logger.info("GitHub ping received: delivery=%s", github_delivery)
        return {"accepted": True, "event": "ping"}

    if github_event == "pull_request":
        pull_request = payload.get("pull_request")
        if not isinstance(pull_request, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="pull_request object is missing",
            )

        repository = payload.get("repository")
        if not isinstance(repository, dict):
            repository = {}

        head = pull_request.get("head")
        if not isinstance(head, dict):
            head = {}

        logger.info(
            "GitHub PR received: delivery=%s action=%s repository=%s pr=%s title=%s sha=%s",
            github_delivery,
            payload.get("action"),
            repository.get("full_name"),
            pull_request.get("number"),
            pull_request.get("title"),
            head.get("sha"),
        )

        action = payload.get("action")
        if action in {"opened", "reopened"}:
            await _notify_slack(settings, repository, pull_request, head)

        return {
            "accepted": True,
            "event": "pull_request",
            "action": payload.get("action"),
            "repository": repository.get("full_name"),
            "pull_request_number": pull_request.get("number"),
            "title": pull_request.get("title"),
            "head_sha": head.get("sha"),
        }

    logger.info(
        "GitHub event ignored: delivery=%s event=%s",
        github_delivery,
        github_event,
    )
    return {"accepted": True, "event": github_event, "ignored": True}


async def _notify_slack(
    settings: Settings,
    repository: dict[str, Any],
    pull_request: dict[str, Any],
    head: dict[str, Any],
) -> None:
    """Send a review request when Slack integration is configured."""

    required = (
        settings.slack_bot_token,
        settings.slack_channel_id,
        settings.action_token_secret,
    )
    if not all(required):
        logger.warning(
            "Slack notification skipped: SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, "
            "and ACTION_TOKEN_SECRET must be configured"
        )
        return

    base = pull_request.get("base")
    if not isinstance(base, dict):
        base = {}
    author = pull_request.get("user")
    if not isinstance(author, dict):
        author = {}

    pr = {
        "repository": repository.get("full_name"),
        "number": pull_request.get("number"),
        "title": pull_request.get("title"),
        "url": pull_request.get("html_url"),
        "sha": head.get("sha"),
        "head_ref": head.get("ref"),
        "base_ref": base.get("ref"),
        "author": author.get("login"),
    }
    if not all(pr.values()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pull request payload is missing fields required for Slack",
        )

    token = create_action_token(
        {
            "repository": pr["repository"],
            "number": pr["number"],
            "title": pr["title"],
            "url": pr["url"],
            "sha": pr["sha"],
            "head_ref": pr["head_ref"],
            "base_ref": pr["base_ref"],
            "author": pr["author"],
        },
        settings.action_token_secret,  # type: ignore[arg-type]
    )
    client = SlackClient(settings.slack_bot_token)  # type: ignore[arg-type]
    await client.post_pr_message(settings.slack_channel_id, pr, token)  # type: ignore[arg-type]
