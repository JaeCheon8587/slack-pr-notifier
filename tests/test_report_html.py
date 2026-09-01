"""Tests for app/report_html.py -- the standalone HTML review report.

Pins three properties the renderer promises (module docstring):
* deterministic structure: every section renders, in order, for any MR;
* safety: every untrusted value (diff text, titles, file contents) is
  HTML-escaped before interpolation;
* completeness: diffs and full file contents ride along in the file, which
  is exactly the payload too large for the 3000-char Slack section limit.
"""

from __future__ import annotations

from app.ai_reviewer import MRReview
from app.report_html import render_review_report


MR = {
    "iid": 7,
    "title": "Fix <bug> & improve",
    "url": "https://gitlab.example.com/group/project/-/merge_requests/7",
    "repository": "group/project",
    "author": "alice",
    "sha": "deadbeef1234",
    "head_ref": "feature/x",
    "base_ref": "main",
}

REVIEW = MRReview(
    summary="이 MR은 <버그>를 수정하고 성능을 향상",
    key_changes=["모듈 A 리팩터링", "API 응답 30% 단축"],
    points_to_watch=["롤백 시나리오 미검증"],
)

CONTEXT = {
    "files": [
        {
            "filename": "src/app.py",
            "status": "modified",
            "additions": 12,
            "deletions": 3,
            "patch": "@@ -1,3 +1,4 @@\n-old line\n+new line with <tag> & special\n context",
        }
    ],
    "contents": {"src/app.py": "print('<hello>')"},
    "files_truncated": False,
}


def test_renders_all_sections_in_order() -> None:
    html = render_review_report(MR, REVIEW, CONTEXT)
    order = [
        "AI 요약",
        "주요 변경점",
        "살펴볼 지점",
        "변경 파일",
        "파일별 diff",
        "변경 파일 전체 내용",
    ]
    positions = [html.find(section) for section in order]
    assert all(p != -1 for p in positions), positions
    assert positions == sorted(positions)


def test_escapes_untrusted_values() -> None:
    html = render_review_report(MR, REVIEW, CONTEXT)
    # Title / summary / diff / contents all carry markup-looking payloads.
    assert "Fix &lt;bug&gt; &amp; improve" in html
    assert "&lt;버그&gt;" in html
    assert "new line with &lt;tag&gt; &amp; special" in html
    assert "print(&#x27;&lt;hello&gt;&#x27;)" in html
    # And no raw, unescaped payload survives anywhere.
    assert "<bug>" not in html
    assert "<tag>" not in html


def test_diff_and_contents_ride_along() -> None:
    html = render_review_report(MR, REVIEW, CONTEXT)
    assert 'class="add"' in html  # +line colored
    assert 'class="del"' in html  # -line colored
    assert 'class="hunk"' in html  # @@ header colored
    assert "src/app.py" in html


def test_handles_missing_review_fields_and_empty_context() -> None:
    empty_review = MRReview(summary="", key_changes=[], points_to_watch=[])
    html = render_review_report({**MR, "url": None}, empty_review, None)
    assert "요약이 없습니다" in html
    assert "항목이 없습니다" in html
    assert "변경 파일 정보가 없습니다" in html
    assert "표시할 diff가 없습니다" in html

