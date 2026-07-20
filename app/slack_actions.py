import json
import logging
from json import JSONDecodeError
from typing import Annotated, Any
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.action_token import InvalidActionToken, decode_action_token
from app.config import Settings, get_settings
from app.github_client import GitHubClient, build_review_body
from app.security import verify_slack_signature
from app.slack_client import SlackClient

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/webhooks/slack", tags=["webhooks"])


@router.post("/actions")
async def receive_slack_action(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    slack_timestamp: Annotated[str | None, Header(alias="X-Slack-Request-Timestamp")] = None,
    slack_signature: Annotated[str | None, Header(alias="X-Slack-Signature")] = None,
) -> dict[str, Any]:
    """Handle Slack review buttons and the rejection-reason modal."""

    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="SLACK_SIGNING_SECRET is not configured")

    body = await request.body()
    if not verify_slack_signature(
        body, slack_timestamp, slack_signature, settings.slack_signing_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack request signature")

    try:
        form = parse_qs(body.decode("utf-8"), strict_parsing=True)
        payload = json.loads(form["payload"][0])
    except (UnicodeDecodeError, ValueError, KeyError, IndexError, JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid Slack interaction payload") from error

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Slack payload must be an object")

    user_id = _slack_user_id(payload)
    _authorize_user(user_id, settings)

    try:
        if payload.get("type") == "block_actions":
            return await _handle_block_action(payload, user_id, settings)
        if payload.get("type") == "view_submission":
            return await _handle_view_submission(payload, user_id, settings)
    except InvalidActionToken as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        logger.exception("External API request failed")
        raise HTTPException(status_code=502, detail="GitHub or Slack API request failed") from error
    except RuntimeError as error:
        logger.exception("Slack API request failed")
        raise HTTPException(status_code=502, detail=str(error)) from error

    return {"accepted": True, "ignored": True}


async def _handle_block_action(
    payload: dict[str, Any], user_id: str, settings: Settings
) -> dict[str, Any]:
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
        raise HTTPException(status_code=400, detail="Slack action is missing")
    action = actions[0]
    token = action.get("value")
    if not isinstance(token, str):
        raise HTTPException(status_code=400, detail="Slack action token is missing")

    pr = _decode_pr(token, settings)
    channel, message_ts = _message_location(payload)
    slack = _slack_client(settings)

    if action.get("action_id") == "request_changes_pr":
        trigger_id = payload.get("trigger_id")
        if not isinstance(trigger_id, str):
            raise HTTPException(status_code=400, detail="Slack trigger_id is missing")
        metadata = json.dumps(
            {"token": token, "channel": channel, "message_ts": message_ts},
            separators=(",", ":"),
        )
        await slack.open_rejection_modal(trigger_id, metadata)
        return {"accepted": True, "action": "request_changes_modal_opened"}

    if action.get("action_id") != "approve_pr":
        return {"accepted": True, "ignored": True}

    await _submit_github_review(pr, user_id, settings, approved=True)
    await slack.update_decision(
        channel, message_ts, pr, approved=True, slack_user_id=user_id
    )
    return {"accepted": True, "action": "approved"}


async def _handle_view_submission(
    payload: dict[str, Any], user_id: str, settings: Settings
) -> dict[str, Any]:
    view = payload.get("view")
    if not isinstance(view, dict) or view.get("callback_id") != "request_changes_submission":
        return {"accepted": True, "ignored": True}

    try:
        metadata = json.loads(view["private_metadata"])
        token = metadata["token"]
        channel = metadata["channel"]
        message_ts = metadata["message_ts"]
        reason = view["state"]["values"]["reason_block"]["reason"]["value"]
    except (KeyError, TypeError, JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Slack modal data is incomplete") from error
    if not all(isinstance(value, str) and value for value in (token, channel, message_ts, reason)):
        raise HTTPException(status_code=400, detail="Slack modal data is invalid")

    pr = _decode_pr(token, settings)
    await _submit_github_review(pr, user_id, settings, approved=False, reason=reason)
    await _slack_client(settings).update_decision(
        channel,
        message_ts,
        pr,
        approved=False,
        slack_user_id=user_id,
        reason=reason,
    )
    return {"accepted": True, "action": "changes_requested"}


async def _submit_github_review(
    pr: dict[str, Any],
    user_id: str,
    settings: Settings,
    *,
    approved: bool,
    reason: str | None = None,
) -> None:
    if not settings.github_token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN is not configured")
    body = build_review_body(
        approved=approved,
        slack_user_id=user_id,
        commit_sha=pr["sha"],
        reason=reason,
    )
    await GitHubClient(settings.github_token).submit_review(
        pr["repository"],
        pr["number"],
        pr["sha"],
        "APPROVE" if approved else "REQUEST_CHANGES",
        body,
    )


def _decode_pr(token: str, settings: Settings) -> dict[str, Any]:
    if not settings.action_token_secret:
        raise HTTPException(status_code=503, detail="ACTION_TOKEN_SECRET is not configured")
    pr = decode_action_token(token, settings.action_token_secret)
    required = ("repository", "number", "title", "url", "sha", "head_ref", "base_ref", "author")
    if not all(pr.get(key) for key in required) or not isinstance(pr["number"], int):
        raise InvalidActionToken("Action token is missing pull-request data")
    return pr


def _slack_user_id(payload: dict[str, Any]) -> str:
    user = payload.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise HTTPException(status_code=400, detail="Slack user is missing")
    return user["id"]


def _authorize_user(user_id: str, settings: Settings) -> None:
    allowed = settings.allowed_slack_users
    if allowed and user_id not in allowed:
        raise HTTPException(status_code=403, detail="This Slack user cannot review pull requests")


def _message_location(payload: dict[str, Any]) -> tuple[str, str]:
    channel = payload.get("channel")
    container = payload.get("container")
    if not isinstance(channel, dict) or not isinstance(container, dict):
        raise HTTPException(status_code=400, detail="Slack message location is missing")
    channel_id = channel.get("id")
    message_ts = container.get("message_ts")
    if not isinstance(channel_id, str) or not isinstance(message_ts, str):
        raise HTTPException(status_code=400, detail="Slack message location is invalid")
    return channel_id, message_ts


def _slack_client(settings: Settings) -> SlackClient:
    if not settings.slack_bot_token:
        raise HTTPException(status_code=503, detail="SLACK_BOT_TOKEN is not configured")
    return SlackClient(settings.slack_bot_token)
