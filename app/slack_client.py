from typing import Any

import httpx


class SlackClient:
    """Minimal Slack Web API client."""

    def __init__(self, token: str) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://slack.com/api/{method}",
                headers=self._headers,
                json=payload,
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Slack API {method} failed: {result.get('error', 'unknown_error')}")
        return result

    async def post_pr_message(self, channel: str, pr: dict[str, Any], token: str) -> None:
        title = str(pr["title"])
        await self.call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": f"PR 리뷰 요청: {title}",
                "blocks": review_blocks(pr, token),
            },
        )

    async def open_rejection_modal(self, trigger_id: str, metadata: str) -> None:
        await self.call(
            "views.open",
            {
                "trigger_id": trigger_id,
                "view": {
                    "type": "modal",
                    "callback_id": "request_changes_submission",
                    "private_metadata": metadata,
                    "title": {"type": "plain_text", "text": "PR 변경 요청"},
                    "submit": {"type": "plain_text", "text": "변경 요청"},
                    "close": {"type": "plain_text", "text": "취소"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "reason_block",
                            "label": {"type": "plain_text", "text": "변경 요청 사유"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reason",
                                "multiline": True,
                                "min_length": 1,
                            },
                        }
                    ],
                },
            },
        )

    async def update_decision(
        self,
        channel: str,
        message_ts: str,
        pr: dict[str, Any],
        *,
        approved: bool,
        slack_user_id: str,
        reason: str | None = None,
    ) -> None:
        status = "✅ 승인됨" if approved else "❌ 변경 요청됨"
        details = f"{status} · Slack 리뷰어 <@{slack_user_id}>"
        if reason:
            details += f"\n> {reason}"
        await self.call(
            "chat.update",
            {
                "channel": channel,
                "ts": message_ts,
                "text": f"PR 리뷰 결과: {status}",
                "blocks": [
                    *review_summary_blocks(pr),
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": details}]},
                ],
            },
        )


def review_summary_blocks(pr: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*PR 리뷰 요청*\n<{pr['url']}|#{pr['number']} {pr['title']}>\n"
                    f"`{pr['head_ref']}` → `{pr['base_ref']}` · 작성자 `{pr['author']}`"
                ),
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"커밋 `{pr['sha']}`"}]},
    ]


def review_blocks(pr: dict[str, Any], token: str) -> list[dict[str, Any]]:
    return [
        *review_summary_blocks(pr),
        {
            "type": "actions",
            "block_id": "pr_review_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve_pr",
                    "text": {"type": "plain_text", "text": "Yes · 승인"},
                    "style": "primary",
                    "value": token,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "PR 승인"},
                        "text": {"type": "mrkdwn", "text": "이 PR을 승인할까요?"},
                        "confirm": {"type": "plain_text", "text": "승인"},
                        "deny": {"type": "plain_text", "text": "취소"},
                    },
                },
                {
                    "type": "button",
                    "action_id": "request_changes_pr",
                    "text": {"type": "plain_text", "text": "No · 변경 요청"},
                    "style": "danger",
                    "value": token,
                },
            ],
        },
    ]
