"""Slack interaction HTTP route — v4.1/P4a design (docs/mr-review-pipeline.html
§②/§S3/§S4①).

Route: POST /webhooks/slack/actions. This module is intentionally thin: Slack
request-signature verification (fail-closed on a missing secret, constant-time
comparison in app.security) and raw body parsing (form-urlencoded ->
``payload`` JSON extraction) are its only jobs here — the actual interaction
handling (the [승인] button's 0-5 step flow, the [의견] button's modal open,
and the [의견] modal's ``view_submission``) lives in ``app.slack_dispatch`` so
a future Socket Mode connection (Slack -> this host inbound HTTP is blocked by
network policy in some deployments) can drive the exact same logic without
going through this route — see ``app.slack_dispatch``'s module docstring for
the full rail description, and ``app.ingest``'s identical webhook/poller split
for the GitLab side. Socket Mode has no request signature to verify (it
authenticates via an App-Level Token instead), so signature verification
stays here, HTTP-only, and is never moved into the shared dispatcher.
"""

from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from app.config import Settings, get_settings, secret_value
from app.security import verify_slack_signature
from app.slack_dispatch import dispatch_interaction

router = APIRouter(prefix="/webhooks/slack", tags=["webhooks"])


@router.post("/actions")
async def receive_slack_action(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    background_tasks: BackgroundTasks,
    slack_timestamp: Annotated[str | None, Header(alias="X-Slack-Request-Timestamp")] = None,
    slack_signature: Annotated[str | None, Header(alias="X-Slack-Signature")] = None,
) -> dict[str, Any]:
    """Verify and parse the Slack interaction request, then delegate the
    [승인]/[의견] handling itself to ``app.slack_dispatch.dispatch_interaction``."""

    # I1: fail-closed if the secret is not configured.
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="SLACK_SIGNING_SECRET is not configured")

    body = await request.body()
    # I2/I3: verify (constant-time, in security.py) before any parsing.
    if not verify_slack_signature(
        body, slack_timestamp, slack_signature, secret_value(settings.slack_signing_secret)
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack request signature")

    try:
        form = parse_qs(body.decode("utf-8"), strict_parsing=True)
        payload = json.loads(form["payload"][0])
    except (UnicodeDecodeError, ValueError, KeyError, IndexError, JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid Slack interaction payload") from error

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Slack payload must be an object")

    return await dispatch_interaction(settings, payload, source="http", background_tasks=background_tasks)
