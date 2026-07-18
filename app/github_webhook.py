import json
import logging
from json import JSONDecodeError
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.security import verify_github_signature

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
