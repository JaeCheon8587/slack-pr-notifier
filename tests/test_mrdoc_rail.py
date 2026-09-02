"""Tests for app/mrdoc/rail.py -- gate + thread-launch contract.

The rail's job is routing, not reviewing: an md-dominant MR under an
enabled setting launches exactly one background thread, everything else
returns None without side effects.
"""

from __future__ import annotations

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
