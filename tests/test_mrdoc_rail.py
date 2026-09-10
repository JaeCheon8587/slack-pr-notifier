"""Tests for app/mrdoc/rail.py -- gate + thread-launch contract.

The rail's job is routing, not reviewing: an md-dominant MR under an
enabled setting launches exactly one background thread, everything else
returns None without side effects.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.mrdoc import rail


def _files(*names: str) -> list[dict[str, Any]]:
    return [{"filename": name} for name in names]


def test_md_ratio_values() -> None:
    assert rail.md_ratio(_files("a.md", "b.mdx")) == 1.0
    assert rail.md_ratio(_files("a.md", "b.py", "c.md", "d.rs")) == 0.5
    assert rail.md_ratio([]) is None
    assert rail.md_ratio(None) is None


def test_passes_gate_threshold_boundary(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_doc_ratio_threshold", 0.8)
    below = _files("a.md", "b.md", "c.md", "d.py")  # 0.75
    at_threshold = _files("a.md", "b.md", "c.md", "d.md", "e.py")  # 0.8
    assert rail.passes_gate(settings, below) is False
    assert rail.passes_gate(settings, at_threshold) is True


def test_passes_gate_disabled_setting(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", False)
    assert rail.passes_gate(settings, _files("a.md")) is False


def test_handles_mr_mirrors_rail_gating(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_satellite_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_doc_ratio_threshold", 0.8)
    assert rail.handles_mr(settings, {"files": _files("a.md", "b.mdx")}) is True
    assert rail.handles_mr(settings, {"files": _files("a.md", "b.py")}) is False
    assert rail.handles_mr(settings, None) is False


def test_handles_mr_requires_satellites(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_satellite_enabled", False)
    assert rail.handles_mr(settings, {"files": _files("a.md")}) is False


def test_start_returns_none_when_gate_fails(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_satellite_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_doc_ratio_threshold", 0.8)
    context = {"files": _files("a.md", "b.py")}
    result = rail.start_mrdoc_review(
        settings, {"iid": 1, "project_id": 10}, {"channel": "C", "ts": "1"}, context=context
    )
    assert result is None


def test_start_launches_thread_when_gate_passes(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_satellite_enabled", True)
    launched: list[Any] = []
    monkeypatch.setattr(rail, "_run_thread", lambda *args: launched.append(args))
    context = {"files": _files("a.md")}
    thread = rail.start_mrdoc_review(
        settings, {"iid": 1, "project_id": 10}, {"channel": "C", "ts": "1"}, context=context
    )
    assert thread is not None
    thread.join(timeout=5)
    assert len(launched) == 1


def test_start_without_context_defers_gate_to_thread(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_satellite_enabled", True)
    launched: list[Any] = []
    monkeypatch.setattr(rail, "_run_thread", lambda *args: launched.append(args))
    thread = rail.start_mrdoc_review(
        settings, {"iid": 2, "project_id": 10}, {"channel": "C", "ts": "2"}, context=None
    )
    assert thread is not None
    thread.join(timeout=5)
    assert len(launched) == 1


def test_start_returns_none_when_satellites_disabled(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mrdoc_enabled", True)
    monkeypatch.setattr(settings, "mrdoc_satellite_enabled", False)
    launched: list[Any] = []
    monkeypatch.setattr(rail, "_run_thread", lambda *args: launched.append(args))
    thread = rail.start_mrdoc_review(
        settings, {"iid": 1, "project_id": 10}, {"channel": "C", "ts": "1"},
        context={"files": _files("a.md")},
    )
    assert thread is None
    assert launched == []


def test_summarize_extracts_frontmatter_counts(tmp_path: Path) -> None:
    (tmp_path / "00-changeset.md").write_text(
        "---\n"
        "mr_iid: 17\n"
        "counts: {files: 2, md: 1, non_md: 1, skipped: false}\n"
        "---\n",
        encoding="utf-8",
    )
    summary = rail._summarize(tmp_path, 4, 17)
    assert "MR !17" in summary
    assert "exit=4" in summary
    assert "counts: files=2, md=1, non_md=1, skipped=False" in summary


def test_summarize_prefers_the_render_nodes_slack_summary(tmp_path: Path) -> None:
    """One overview, two destinations — Slack must not compute its own."""

    (tmp_path / "00-changeset.md").write_text(
        "---\nmr_iid: 17\ncounts: {files: 2, md: 1}\n---\n", encoding="utf-8"
    )
    (tmp_path / "slack-summary.txt").write_text(
        "MR !17\n구조 0 · 의미 1 (값 1) · 표현 0\n[리포트 신뢰도] 지적 잔존 0\n",
        encoding="utf-8",
    )
    summary = rail._summarize(tmp_path, 4, 17)
    assert "MR !17" in summary
    assert "exit=4" in summary
    assert "[리포트 신뢰도] 지적 잔존 0" in summary
    assert "counts:" not in summary  # the fallback stayed out of the way


def test_summary_keys_are_exactly_the_two_count_blocks() -> None:
    """The fallback reads counts only — nothing that could read as a ruling."""

    assert rail._SUMMARY_KEYS == {
        "changeset": ("counts", ("files", "md", "non_md", "skipped")),
        "literals": ("totals", ("removed", "added", "changed")),
    }


def test_uploadable_report_rejects_stub_output(tmp_path: Path) -> None:
    (tmp_path / "report.html").write_text(
        "<html>RAIL-STUB placeholder</html>", encoding="utf-8"
    )
    assert rail._uploadable_report(tmp_path) is None


def test_uploadable_report_returns_real_content(tmp_path: Path) -> None:
    (tmp_path / "report.html").write_text(
        "<html>real review content</html>", encoding="utf-8"
    )
    assert rail._uploadable_report(tmp_path) == "<html>real review content</html>"


def test_uploadable_report_missing_file(tmp_path: Path) -> None:
    assert rail._uploadable_report(tmp_path) is None


class _RecordingSlack:
    """Fake SlackClient capturing chat calls and file uploads separately."""

    def __init__(self, token: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"method": method, "payload": payload})
        return {"ok": True}

    async def upload_report_file(
        self, channel, thread_ts, filename, content, *, initial_comment=None
    ):  # noqa: ANN001
        self.uploads.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "filename": filename,
                "content": content,
                "initial_comment": initial_comment,
            }
        )
        return {"ok": True}


class _ExplodingUploadSlack(_RecordingSlack):
    async def upload_report_file(
        self, channel, thread_ts, filename, content, *, initial_comment=None
    ):  # noqa: ANN001
        raise RuntimeError("upload boom")


def _delivery_settings(monkeypatch) -> Any:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    return settings


def test_post_summary_single_message_when_report_exists(monkeypatch, tmp_path: Path) -> None:
    """요약 텍스트와 report.html가 슬랙 메시지 하나(initial_comment)로 합쳐진다."""
    settings = _delivery_settings(monkeypatch)
    recorder = _RecordingSlack("xoxb-test")
    monkeypatch.setattr(rail, "SlackClient", lambda token: recorder)
    (tmp_path / "report.html").write_text("<html>real content</html>", encoding="utf-8")

    asyncio.run(
        rail._post_summary(settings, {"channel": "C1", "ts": "111.222"}, 31, tmp_path, 4)
    )

    assert recorder.calls == [], "no separate text message may be posted"
    (upload,) = recorder.uploads
    assert upload["filename"] == "mrdoc-report.html"
    assert upload["thread_ts"] == "111.222"
    assert upload["initial_comment"] is not None
    assert "mrdoc 문서 변경 리포트 -- MR !31" in upload["initial_comment"]
    assert "exit=4" in upload["initial_comment"]


def test_post_summary_text_only_without_report(monkeypatch, tmp_path: Path) -> None:
    settings = _delivery_settings(monkeypatch)
    recorder = _RecordingSlack("xoxb-test")
    monkeypatch.setattr(rail, "SlackClient", lambda token: recorder)

    asyncio.run(
        rail._post_summary(settings, {"channel": "C1", "ts": "111.222"}, 31, tmp_path, 4)
    )

    assert recorder.uploads == []
    (call,) = recorder.calls
    assert call["method"] == "chat.postMessage"
    assert call["payload"]["thread_ts"] == "111.222"
    assert "MR !31" in call["payload"]["text"]


def test_post_summary_falls_back_to_text_when_upload_fails(monkeypatch, tmp_path: Path) -> None:
    """업로드 실패 시에도 요약 텍스트는 반드시 도착한다."""
    settings = _delivery_settings(monkeypatch)
    recorder = _ExplodingUploadSlack("xoxb-test")
    monkeypatch.setattr(rail, "SlackClient", lambda token: recorder)
    (tmp_path / "report.html").write_text("<html>real content</html>", encoding="utf-8")

    asyncio.run(
        rail._post_summary(settings, {"channel": "C1", "ts": "111.222"}, 31, tmp_path, 4)
    )

    (call,) = recorder.calls
    assert call["method"] == "chat.postMessage"
    assert "MR !31" in call["payload"]["text"]


def test_post_summary_without_slack_target_posts_nothing(monkeypatch, tmp_path: Path) -> None:
    settings = _delivery_settings(monkeypatch)
    recorder = _RecordingSlack("xoxb-test")
    monkeypatch.setattr(rail, "SlackClient", lambda token: recorder)

    asyncio.run(rail._post_summary(settings, None, 31, tmp_path, 4))

    assert recorder.calls == [] and recorder.uploads == []
