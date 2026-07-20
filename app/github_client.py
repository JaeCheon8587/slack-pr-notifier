from datetime import UTC, datetime
from typing import Any

import httpx


class GitHubClient:
    """Minimal GitHub client for submitting pull-request reviews."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def submit_review(
        self,
        repository: str,
        pull_request_number: int,
        commit_sha: str,
        event: str,
        body: str,
    ) -> dict[str, Any]:
        url = (
            f"https://api.github.com/repos/{repository}/pulls/"
            f"{pull_request_number}/reviews"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                headers=self._headers,
                json={"commit_id": commit_sha, "event": event, "body": body},
            )
        response.raise_for_status()
        return response.json()


def build_review_body(
    *,
    approved: bool,
    slack_user_id: str,
    commit_sha: str,
    reason: str | None = None,
) -> str:
    """Build the standard Markdown body shown on GitHub."""

    result = "✅ 승인" if approved else "❌ 변경 요청"
    lines = [
        "## Slack PR Review",
        "",
        f"- 결과: {result}",
        f"- Slack 리뷰어: `{slack_user_id}`",
        f"- 검토 시각: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- 대상 커밋: `{commit_sha}`",
    ]
    if reason:
        lines.extend(["", "### 변경 요청 사유", "", reason])
    return "\n".join(lines)
