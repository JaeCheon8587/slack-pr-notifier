"""Tests for the revise re-notify payload builder (app/slack_client.py).

Covers the two Part 4 rendering changes — the 되물음(clarify) block and the
richer 미반영 list — plus the no-regression property they hang on: with no
``clarify`` and an old-shape ``unapplied`` list, ``_revise_result_payload``
produces exactly what it produced before.

Pure payload assertions — no Slack API call is made (``_revise_result_payload``
is the shared body builder behind both ``update_revise_result`` and
``post_revise_result``).
"""

from __future__ import annotations

from typing import Any

from app.slack_client import _clarify_block, _revise_result_payload, _unapplied_line

TOKEN = "signed-action-token"


def _mr() -> dict[str, Any]:
    return {
        "project_id": 42,
        "repository": "group/project",
        "iid": 1,
        "title": "Test MR",
        "url": "https://gitlab.example.com/group/project/-/merge_requests/1",
        "sha": "abc123",
        "head_ref": "feature/test",
        "base_ref": "main",
        "author": "author1",
    }


def _texts(blocks: list[dict[str, Any]]) -> list[str]:
    """Every rendered mrkdwn string in a block list, section and context alike."""

    collected: list[str] = []
    for block in blocks:
        if "text" in block and isinstance(block["text"], dict):
            collected.append(block["text"].get("text", ""))
        for element in block.get("elements", []) or []:
            if isinstance(element, dict) and isinstance(element.get("text"), str):
                collected.append(element["text"])
    return collected


def _action_block(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return next(block for block in blocks if block.get("type") == "actions")


def _clarify_entries() -> list[dict[str, Any]]:
    return [
        {
            "opinion_id": 7,
            "question": "어느 절을 말씀하신 건가요?",
            "candidates": ["docs/guide.md#설치", "docs/guide.md#설정", "docs/intro.md#개요"],
        }
    ]


# ---------------------------------------------------------------------------
# No clarify — the payload is unchanged
# ---------------------------------------------------------------------------
def test_payload_without_clarify_keeps_the_round_header_and_text() -> None:
    text, blocks = _revise_result_payload(_mr(), TOKEN, round_number=2, unapplied=[])

    assert text == "MR 리뷰 요청 (라운드 2)"
    assert blocks[0]["text"]["text"] == "🔄 라운드 2 완료 — 재확인 후 승인해주세요"
    assert not any("❓" in rendered for rendered in _texts(blocks))


def test_clarify_none_and_clarify_empty_produce_the_identical_payload() -> None:
    """The new keyword defaults to ``None``; passing ``[]`` must be the same
    thing, and both must equal what the pre-change signature produced."""

    baseline = _revise_result_payload(
        _mr(), TOKEN, round_number=1, unapplied=[{"reason": "사유 A"}]
    )
    explicit_none = _revise_result_payload(
        _mr(), TOKEN, round_number=1, unapplied=[{"reason": "사유 A"}], clarify=None
    )
    explicit_empty = _revise_result_payload(
        _mr(), TOKEN, round_number=1, unapplied=[{"reason": "사유 A"}], clarify=[]
    )

    assert baseline == explicit_none == explicit_empty


def test_old_shape_unapplied_entry_still_renders_the_bare_reason() -> None:
    """A caller passing only ``{"reason": ...}`` (every pre-Part-4 call site)
    gets the byte-identical old line — no stray '#None' or empty brackets."""

    _, blocks = _revise_result_payload(
        _mr(), TOKEN, round_number=1, unapplied=[{"reason": "사유 A"}]
    )

    rendered = next(item for item in _texts(blocks) if "미반영" in item)
    assert rendered == "⚠️ 미반영 의견 1건\n• 사유 A"


# ---------------------------------------------------------------------------
# 미반영 목록 개선 — opinion_id + body head alongside the reason
# ---------------------------------------------------------------------------
def test_unapplied_line_carries_opinion_id_and_body_head() -> None:
    line = _unapplied_line(
        {
            "opinion_id": 12,
            "reason": "대상 절을 찾지 못함",
            "body": "설치 절의 버전 표기를 고쳐주세요",
        }
    )

    assert "#12" in line
    assert "설치 절의 버전 표기를 고쳐주세요" in line
    assert "대상 절을 찾지 못함" in line


def test_unapplied_line_collapses_whitespace_and_clips_a_long_body() -> None:
    line = _unapplied_line(
        {"opinion_id": 3, "reason": "사유", "body": "머리말\n\n두 번째 줄  " + ("가" * 200)}
    )

    assert "\n" not in line
    assert "머리말 두 번째 줄" in line
    assert "…" in line  # the body head was clipped


def test_unapplied_line_falls_back_when_reason_is_blank() -> None:
    assert _unapplied_line({}) == "(사유 없음)"
    assert _unapplied_line({"reason": "   "}) == "(사유 없음)"
    assert _unapplied_line({"opinion_id": 5, "reason": ""}) == "#5 — (사유 없음)"


def test_unapplied_list_distinguishes_two_opinions(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, blocks = _revise_result_payload(
        _mr(),
        TOKEN,
        round_number=1,
        unapplied=[
            {"opinion_id": 1, "reason": "사유 A", "body": "첫 의견"},
            {"opinion_id": 2, "reason": "사유 B", "body": "둘째 의견"},
        ],
    )

    rendered = next(item for item in _texts(blocks) if "미반영" in item)
    assert "미반영 의견 2건" in rendered
    assert "#1" in rendered and "#2" in rendered
    assert "첫 의견" in rendered and "둘째 의견" in rendered


# ---------------------------------------------------------------------------
# 되물음 block
# ---------------------------------------------------------------------------
def test_clarify_swaps_the_header_and_the_fallback_text() -> None:
    text, blocks = _revise_result_payload(
        _mr(), TOKEN, round_number=3, unapplied=[], clarify=_clarify_entries()
    )

    assert blocks[0]["text"]["text"] == "❓ 대상을 찾지 못했습니다 — 확인 부탁드립니다"
    assert "라운드 3 완료" not in blocks[0]["text"]["text"]
    assert text == "MR 리뷰 요청 (라운드 3) — 대상 확인 필요"


def test_clarify_renders_the_question_and_all_three_candidates() -> None:
    _, blocks = _revise_result_payload(
        _mr(), TOKEN, round_number=1, unapplied=[], clarify=_clarify_entries()
    )

    rendered = next(item for item in _texts(blocks) if item.startswith("❓ 의견 #7"))
    assert "어느 절을 말씀하신 건가요?" in rendered
    for candidate in ("docs/guide.md#설치", "docs/guide.md#설정", "docs/intro.md#개요"):
        assert candidate in rendered
    # The human answers through the button they already have.
    assert "[의견] 버튼을 다시 눌러" in rendered


def test_clarify_does_not_add_or_change_any_button() -> None:
    """되물음 rides the existing rail: same two buttons, same action_ids, same
    token — no new button, no modal (docs/revise-workflow.html Part 2)."""

    _, plain = _revise_result_payload(_mr(), TOKEN, round_number=1, unapplied=[])
    _, asked = _revise_result_payload(
        _mr(), TOKEN, round_number=1, unapplied=[], clarify=_clarify_entries()
    )

    assert _action_block(asked) == _action_block(plain)
    assert [element["action_id"] for element in _action_block(asked)["elements"]] == [
        "approve_mr",
        "request_changes_mr",
    ]
    assert all(element["value"] == TOKEN for element in _action_block(asked)["elements"])


def test_clarify_entry_without_a_question_is_ignored() -> None:
    """A malformed entry must not silently swap the header to 되물음 while
    rendering nothing to answer."""

    text, blocks = _revise_result_payload(
        _mr(),
        TOKEN,
        round_number=4,
        unapplied=[],
        clarify=[{"opinion_id": 9, "question": "   ", "candidates": ["docs/a.md"]}],
    )

    assert blocks[0]["text"]["text"] == "🔄 라운드 4 완료 — 재확인 후 승인해주세요"
    assert text == "MR 리뷰 요청 (라운드 4)"


def test_clarify_block_without_candidates_still_renders_the_question() -> None:
    block = _clarify_block({"opinion_id": 2, "question": "어디를 말씀하시나요?", "candidates": []})

    rendered = block["elements"][0]["text"]
    assert block["type"] == "context"
    assert "어디를 말씀하시나요?" in rendered
    assert "후보:" not in rendered


def test_clarify_block_without_opinion_id_omits_the_number() -> None:
    block = _clarify_block({"question": "어디를 말씀하시나요?", "candidates": []})

    rendered = block["elements"][0]["text"]
    assert rendered.startswith("❓ 의견: ")
    assert "#None" not in rendered


def test_clarify_strings_are_clipped_for_slack_exposure() -> None:
    block = _clarify_block(
        {
            "opinion_id": 1,
            "question": "가" * 500,
            "candidates": ["나" * 500],
        }
    )

    rendered = block["elements"][0]["text"]
    assert "…" in rendered
    assert len(rendered) < 800  # question capped at 300, candidate at 200


def test_clarify_and_unapplied_render_together_in_a_partial_round() -> None:
    """"일부만 MISSING": the round committed something, so the 미반영 list and
    the 되물음 block both appear on the same message."""

    _, blocks = _revise_result_payload(
        _mr(),
        TOKEN,
        round_number=2,
        unapplied=[{"opinion_id": 7, "reason": "대상 미확인", "body": "그 절 고쳐줘"}],
        summary="한 건 반영",
        clarify=_clarify_entries(),
    )

    rendered = _texts(blocks)
    assert any(item.startswith("❓ 의견 #7") for item in rendered)
    assert any("미반영 의견 1건" in item for item in rendered)
    assert any("한 건 반영" in item for item in rendered)
    # Order: header → 요약 → 되물음 → 미반영 → 버튼.
    clarify_at = next(i for i, item in enumerate(rendered) if item.startswith("❓ 의견 #7"))
    unapplied_at = next(i for i, item in enumerate(rendered) if "미반영 의견" in item)
    summary_at = next(i for i, item in enumerate(rendered) if "한 건 반영" in item)
    assert summary_at < clarify_at < unapplied_at
