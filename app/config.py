from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    app_name: str = "Slack PR Notifier"
    app_env: str = "local"
    github_webhook_secret: str | None = None
    github_token: str | None = None
    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    slack_channel_id: str | None = None
    slack_allowed_user_ids: str = ""
    action_token_secret: str | None = None

    @property
    def allowed_slack_users(self) -> set[str]:
        """Return the configured Slack reviewer allowlist."""

        return {
            user_id.strip()
            for user_id in self.slack_allowed_user_ids.split(",")
            if user_id.strip()
        }

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
