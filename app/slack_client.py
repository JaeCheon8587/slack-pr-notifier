import json
from typing import Any

import httpx


class SlackClient:
    """Minimal Slack Web API client."""

    def __init__(self, token: str) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}

    async def call(
        self, method: str, payload: dict[str, Any], *, form: bool = False
    ) -> dict[str, Any]:
        """Call a Slack Web API method.

        form=True posts application/x-www-form-urlencoded (complex values
        JSON-encoded) -- required by the files API v2 family, which rejects
        JSON bodies with invalid_arguments ("missing required field") even
        when every field is present.
        """
        async with httpx.AsyncClient(timeout=10) as client:
            if form:
                fields = {
                    key: value if isinstance(value, str) else json.dumps(value)
                    for key, value in payload.items()
                }
                response = await client.post(
                    f"https://slack.com/api/{method}",
                    headers=self._headers,
                    data=fields,
                )
            else:
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

    async def post_mr_message(
        self, channel: str, mr: dict[str, Any], token: str, review: Any = None
    ) -> dict[str, Any]:
        title = str(mr["title"])
        return await self.call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": f"MR 리뷰 요청: {title}",
                "blocks": review_blocks(mr, token, review),
            },
        )

    async def upload_report_file(
        self,
        channel: str,
        thread_ts: str,
        filename: str,
        content: str,
        *,
        initial_comment: str | None = None,
    ) -> dict[str, Any]:
        """Upload the HTML review report into a message's thread.

        Slack files API v2, three steps:
        1. files.getUploadURLExternal -- reserve an upload slot (a file_id
           and a pre-signed upload_url).
        2. POST the raw bytes to that upload_url. The URL is pre-signed: it
           must be called WITHOUT the Authorization header (sending one
           invalidates the signature).
        3. files.completeUploadExternal -- publish the file to the channel,
           in the thread under the notification message (thread_ts).

        Requires the files:write bot scope (the bot is already a member of
        the channel to post the notification itself).
        """

        data = content.encode("utf-8")
        outer = await self.call(
            "files.getUploadURLExternal",
            {"filename": filename, "length": len(data)},
            form=True,
        )
        upload_url = outer.get("upload_url")
        file_id = outer.get("file_id")
        if not upload_url or not file_id:
            raise RuntimeError("files.getUploadURLExternal returned no upload_url/file_id")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(str(upload_url), content=data)
        response.raise_for_status()
        payload: dict[str, Any] = {
            "files": [{"id": file_id}],
            "channel_id": channel,
            "thread_ts": thread_ts,
        }
        if initial_comment:
            payload["initial_comment"] = initial_comment
        return await self.call("files.completeUploadExternal", payload, form=True)
    async def open_opinion_modal(self, trigger_id: str, metadata: str) -> None:
        """Open the [의견] modal (§S3 step 1-2): free-text opinion only.

        The "대상 확인질문 번호" input was removed — AI-generated numbered
        confirmation questions are not implemented, so there was nothing for
        it to reference. ``metadata`` is the button's own signed action
        token, carried through as ``private_metadata`` so the submission
        handler can re-verify it (session_id/sha binding) exactly like the
        button click.
        """

        await self.call(
            "views.open",
            {
                "trigger_id": trigger_id,
                "view": {
                    "type": "modal",
                    "callback_id": "opinion_submission",
                    "private_metadata": metadata,
                    "title": {"type": "plain_text", "text": "MR 의견"},
                    "submit": {"type": "plain_text", "text": "제출"},
                    "close": {"type": "plain_text", "text": "취소"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "opinion_block",
                            "label": {"type": "plain_text", "text": "의견"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "opinion_body",
                                "multiline": True,
                                "min_length": 1,
                            },
                        },
                    ],
                },
            },
        )

    async def post_ephemeral(self, channel: str, user_id: str, text: str) -> None:
        """Post an ephemeral message via ``chat.postEphemeral``.

        Used from the ``view_submission`` (modal) path, which — unlike
        ``block_actions`` — carries no ``response_url`` (§S3/§S4①: "이미 접수됨"/
        "이미 개선 작업 진행 중" notices). ``channel`` is the session's stored
        Slack channel (the MR review message's channel).
        """
        await self.call(
            "chat.postEphemeral",
            {"channel": channel, "user": user_id, "text": text},
        )

    async def update_revising(self, channel: str, message_ts: str, header_text: str) -> None:
        """Update the main MR message once an opinion is accepted and revise starts.

        Per §S3 step 7 / §S4① step 5: "✏️ 의견 접수 · 🔄 개선 시작" — buttons are
        removed (완전 동결) but, unlike ``withdraw_buttons``, the language is not
        terminal since the session continues (revising -> reviewing on
        revise success).
        """
        await self.call(
            "chat.update",
            {
                "channel": channel,
                "ts": message_ts,
                "text": "MR 의견 접수 · 개선 작업 진행 중",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": "✏️ 의견 접수 · 🔄 개선 작업 진행 중"}
                        ],
                    },
                ],
            },
        )

    async def update_decision(
        self,
        channel: str,
        message_ts: str,
        mr: dict[str, Any],
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
                "text": f"MR 리뷰 결과: {status}",
                "blocks": [
                    *review_summary_blocks(mr),
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": details}]},
                ],
            },
        )


    async def reissue_approve_only(
        self, channel: str, message_ts: str, header_text: str, token: str
    ) -> None:
        """Reissue the message with only the [승인] button — used after a human push on a
        `reviewing` session. The [의견] button is intentionally dropped: automatic revise is
        not allowed once a human has committed on top of the reviewed SHA (설계 M5).
        """
        await self.call(
            "chat.update",
            {
                "channel": channel,
                "ts": message_ts,
                "text": header_text,
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "사람 커밋이 반영되어 [의견] 버튼은 비활성화되었습니다 (자동 revise 금지)",
                            }
                        ],
                    },
                    {
                        "type": "actions",
                        "block_id": "mr_review_actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "approve_mr",
                                "text": {"type": "plain_text", "text": "Yes · 승인"},
                                "style": "primary",
                                "value": token,
                                "confirm": {
                                    "title": {"type": "plain_text", "text": "MR 승인"},
                                    "text": {"type": "mrkdwn", "text": "이 MR을 승인할까요?"},
                                    "confirm": {"type": "plain_text", "text": "승인"},
                                    "deny": {"type": "plain_text", "text": "취소"},
                                },
                            },
                        ],
                    },
                ],
            },
        )

    async def update_revise_result(
        self,
        channel: str,
        message_ts: str,
        mr: dict[str, Any],
        token: str,
        *,
        round_number: int,
        unapplied: list[dict[str, Any]],
        summary: str | None = None,
        diff_stat: str | None = None,
        compare_url: str | None = None,
    ) -> None:
        """Re-notify after a revise round completes (§S4② step (e), kind=ok).

        Reuses ``review_blocks`` (via ``_revise_result_payload``) to rebuild
        the [승인]/[의견] buttons bound to the new SHA token, prefixed with a
        "라운드 N 완료" header and, when some opinions could not be applied
        this round, an "⚠️ 미반영" list of reasons (the 3c 대조검증 verdicts).
        Kept for other callers/tests; the revise-loop re-notify itself now
        uses ``post_revise_result`` instead (사용자 결정: 라운드마다 새 알림).

        ``summary``/``diff_stat``/``compare_url`` render the "이전 대비
        변경점" blocks (see ``_revise_result_payload``); all default to
        ``None`` so existing callers are unaffected.
        """
        text, blocks = _revise_result_payload(
            mr,
            token,
            round_number=round_number,
            unapplied=unapplied,
            summary=summary,
            diff_stat=diff_stat,
            compare_url=compare_url,
        )
        await self.call(
            "chat.update",
            {
                "channel": channel,
                "ts": message_ts,
                "text": text,
                "blocks": blocks,
            },
        )

    async def post_revise_result(
        self,
        channel: str,
        mr: dict[str, Any],
        token: str,
        *,
        round_number: int,
        unapplied: list[dict[str, Any]],
        summary: str | None = None,
        diff_stat: str | None = None,
        compare_url: str | None = None,
    ) -> dict[str, Any]:
        """Post a brand-new message for a completed revise round.

        Unlike ``update_revise_result`` (chat.update, in place), this sends a
        fresh ``chat.postMessage`` so each round produces its own Slack
        notification (사용자 결정: 라운드마다 새 알림). The caller
        (``app.revise_executor._notify_revise_success``) is responsible for
        moving ``review_session.slack_ts`` to the returned ``ts`` and
        withdrawing the previous message's buttons.

        ``summary``/``diff_stat``/``compare_url`` render the "이전 대비
        변경점" blocks (see ``_revise_result_payload``); all default to
        ``None`` so existing callers are unaffected.
        """
        text, blocks = _revise_result_payload(
            mr,
            token,
            round_number=round_number,
            unapplied=unapplied,
            summary=summary,
            diff_stat=diff_stat,
            compare_url=compare_url,
        )
        return await self.call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "blocks": blocks,
            },
        )

    async def withdraw_buttons(
        self, channel: str, message_ts: str, header_text: str, reason: str
    ) -> None:
        """Recall the review buttons by rewriting the message as closed/terminal.

        Used for external merge/close detection and for merging/revising sessions
        that are force-transitioned to ``manual`` by a human push.
        """
        await self.call(
            "chat.update",
            {
                "channel": channel,
                "ts": message_ts,
                "text": f"MR 리뷰 종료: {reason}",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": f"⛔ {reason}"}]},
                ],
            },
        )


def review_summary_blocks(mr: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*MR 리뷰 요청*\n<{mr['url']}|!{mr['iid']} {mr['title']}>\n"
                    f"`{mr['head_ref']}` → `{mr['base_ref']}` · 작성자 `{mr['author']}`"
                ),
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"커밋 `{mr['sha']}`"}]},
    ]


def ai_review_blocks(review: Any) -> list[dict[str, Any]]:
    """Render the compact AI summary; the full report travels as report.html.

    The Slack message carries only the "요약본" -- the one-line summary and
    the key-change bullets. points_to_watch and the per-file diffs live in
    the HTML review report uploaded to the notification's thread
    (upload_report_file), which has no 3000-char section limit.
    """

    if review is None:
        return []
    sections = [f"*🤖 AI 요약*\n{_clip(str(review.summary).strip(), 900)}"]
    if getattr(review, "key_changes", None):
        bullets = "\n".join(
            f"• {_clip(str(item), 200)}" for item in list(review.key_changes)[:6]
        )
        sections.append("*주요 변경점*\n" + bullets)
    sections.append("_📄 상세 리뷰 리포트는 이 메시지 스레드에 첨부된 report.html을 확인하세요_")
    text = "\n\n".join(sections)
    if len(text) > 2900:  # Slack section text limit is 3000 chars
        text = text[:2900] + "…"
    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


def review_blocks(mr: dict[str, Any], token: str, review: Any = None) -> list[dict[str, Any]]:
    actions = {
        "type": "actions",
        "block_id": "mr_review_actions",
        "elements": [
            {
                "type": "button",
                "action_id": "approve_mr",
                "text": {"type": "plain_text", "text": "Yes · 승인"},
                "style": "primary",
                "value": token,
                "confirm": {
                    "title": {"type": "plain_text", "text": "MR 승인"},
                    "text": {"type": "mrkdwn", "text": "이 MR을 승인할까요?"},
                    "confirm": {"type": "plain_text", "text": "승인"},
                    "deny": {"type": "plain_text", "text": "취소"},
                },
            },
            {
                "type": "button",
                "action_id": "request_changes_mr",
                "text": {"type": "plain_text", "text": "No · 변경 요청"},
                "style": "danger",
                "value": token,
            },
        ],
    }
    return [*review_summary_blocks(mr), *ai_review_blocks(review), actions]


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` chars, marking clips with '…'."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _revise_result_payload(
    mr: dict[str, Any],
    token: str,
    *,
    round_number: int,
    unapplied: list[dict[str, Any]],
    summary: str | None = None,
    diff_stat: str | None = None,
    compare_url: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build the (text, blocks) pair for a completed revise round's re-notify.

    Shared by ``update_revise_result`` (chat.update) and ``post_revise_result``
    (chat.postMessage) so the two delivery mechanisms render an identical
    message body.

    ``summary``/``diff_stat``/``compare_url`` render the "이전 대비 변경점"
    blocks, inserted right after the header and before the existing 미반영
    의견 context, in that fixed order (요약 → diff stat → compare 링크).
    Each is omitted when ``None`` or blank (after stripping); wiring real
    values in is a follow-up step — this only adds the rendering.
    """
    blocks = review_blocks(mr, token)
    header = f"🔄 라운드 {round_number} 완료 — 재확인 후 승인해주세요"
    blocks.insert(0, {"type": "section", "text": {"type": "mrkdwn", "text": header}})

    insert_at = 1
    if summary is not None and summary.strip():
        clipped_summary = _clip(summary.strip(), 700)
        blocks.insert(
            insert_at,
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📝 변경 요약\n{clipped_summary}"},
            },
        )
        insert_at += 1

    if diff_stat is not None and diff_stat.strip():
        stat_lines = diff_stat.strip().split("\n")
        if len(stat_lines) > 12:
            hidden = len(stat_lines) - 12
            stat_text = "\n".join(stat_lines[:12]) + f"\n…외 {hidden}줄"
        else:
            stat_text = "\n".join(stat_lines)
        stat_text = _clip(stat_text, 900)
        blocks.insert(
            insert_at,
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{stat_text}```"},
            },
        )
        insert_at += 1

    if compare_url is not None and compare_url.strip():
        blocks.insert(
            insert_at,
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"<{compare_url.strip()}|이전 대비 변경 보기>",
                    }
                ],
            },
        )
        insert_at += 1

    if unapplied:
        lines = "\n".join(f"• {item.get('reason') or '(사유 없음)'}" for item in unapplied)
        blocks.insert(
            insert_at,
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"⚠️ 미반영 의견 {len(unapplied)}건\n{lines}"}
                ],
            },
        )
    text = f"MR 리뷰 요청 (라운드 {round_number})"
    return text, blocks
