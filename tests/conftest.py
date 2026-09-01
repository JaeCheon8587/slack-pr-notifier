import pytest

from app.config import get_settings

_ISOLATED_FIELDS = (
    "gitlab_webhook_secret",
    "gitlab_token",
    "slack_bot_token",
    "slack_signing_secret",
    "slack_channel_id",
    "action_token_secret",
)


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch):
    """Neutralize external credentials so tests never hit real Slack/GitLab/Claude.

    Tests that exercise a specific path set the values they need explicitly.
    """

    settings = get_settings()
    for field in _ISOLATED_FIELDS:
        monkeypatch.setattr(settings, field, None)
    monkeypatch.setattr(settings, "reviewer_map", "")
    monkeypatch.setattr(settings, "ai_enabled", False)
