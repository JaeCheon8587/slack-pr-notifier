"""Tests for app/mrdoc/satellites.py — spec parsing + executor contract."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.mrdoc import satellites
from app.mrdoc.changeset import build_changeset, render_changeset
from app.mrdoc.dispatch import SpecBlock
from app.mrdoc.satellites import Mission, parse_spec


def test_parse_spec_full_block() -> None:
    text = (
        "WAVE 2\n"
        "SPEC verifier\n"
        "READ w/30-levelcheck.md, w/00-changeset.md\n"
        "BUDGET 0.25\n"
        "RETURN w/40-verifier.md\n"
        "SCOPE files 0..4"
    )
    assert parse_spec(text) == Mission(
        wave=2,
        agent="verifier",
        read=(Path("w/30-levelcheck.md"), Path("w/00-changeset.md")),
        budget_usd=0.25,
        return_path=Path("w/40-verifier.md"),
        scope="files 0..4",
    )


def test_parse_spec_requires_return() -> None:
    with pytest.raises(ValueError):
        parse_spec("WAVE 1\nSPEC analyzer\nBUDGET 0.1")


def _spec(agent: str, return_path: Path, scope: str = "") -> str:
    return SpecBlock(
        wave=2, agent=agent, read=(), budget_usd=0.25, return_path=return_path, scope=scope
    ).render()


def _proc(returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="{}", stderr="")


def _fake_run_writer(monkeypatch, env_capture: dict | None = None):
    """Fake codex run that writes every '[산출 위치]' target and file_id."""

    def fake_run(cmd, **kwargs):
        if env_capture is not None:
            env_capture["cmd"] = cmd
            env_capture["env"] = kwargs.get("env")
        cwd = Path(str(kwargs.get("cwd")))
        for line in str(kwargs.get("input") or "").splitlines():
            if line.startswith("[산출 위치] "):
                target = Path(line.partition("[산출 위치] ")[2].split(" — ")[0])
                target.parent.mkdir(parents=True, exist_ok=True)
                if "이 파일 하나만" in line:
                    target.write_text("written by fake", encoding="utf-8")
            elif line.startswith("- file_id "):
                fid = line.split()[2]
                out_dir = cwd / "20-analysis"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{fid}.md").write_text("written by fake", encoding="utf-8")
        return _proc()

    monkeypatch.setattr(satellites.subprocess, "run", fake_run)


def _settings(monkeypatch, work: Path):
    settings = get_settings()
    monkeypatch.setattr(settings, "codex_bin", "codex")
    monkeypatch.setattr(
        satellites, "_process_env", lambda: {"PATH": "p", "ANTHROPIC_API_KEY": "k"}
    )
    return settings


def _write_changeset(work: Path) -> None:
    changeset = build_changeset(
        mr_iid=17,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=[
            {
                "filename": "docs/a.md",
                "status": "modified",
                "patch": "@@ -1,2 +1,2 @@",
            }
        ],
    )
    (work / "00-changeset.md").write_text(render_changeset(changeset), encoding="utf-8")


def test_executor_verifier_success(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    _fake_run_writer(monkeypatch, captured)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    assert executor(_spec("verifier", tmp_path / "40-verifier.md")) is True
    assert (tmp_path / "40-verifier.md").read_text(encoding="utf-8") == "written by fake"
    cmd = captured["cmd"]
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert "workspace-write" in cmd
    assert "--ephemeral" in cmd


def test_executor_analyzer_writes_batch_files(tmp_path, monkeypatch) -> None:
    _write_changeset(tmp_path)
    _fake_run_writer(monkeypatch)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope="files 0..0")
    assert executor(spec) is True
    written = list((tmp_path / "20-analysis").glob("*.md"))
    assert len(written) == 1


def test_executor_missing_artifact_returns_false(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        satellites.subprocess,
        "run",
        lambda cmd, **kwargs: _proc(),
    )
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    assert executor(_spec("reporter", tmp_path / "60-report.md")) is False


def test_executor_timeout_returns_false(tmp_path, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 600)

    monkeypatch.setattr(satellites.subprocess, "run", fake_run)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    assert executor(_spec("verifier", tmp_path / "40-verifier.md")) is False


def test_executor_nonzero_exit_returns_false(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        satellites.subprocess,
        "run",
        lambda cmd, **kwargs: _proc(returncode=1),
    )
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    assert executor(_spec("verifier", tmp_path / "40-verifier.md")) is False


def test_executor_rejects_unparseable_and_unknown(tmp_path, monkeypatch) -> None:
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    assert executor("not a spec") is False
    assert executor(_spec("structure", tmp_path / "05-structure.md")) is False


def test_executor_rejects_bad_scope(tmp_path, monkeypatch) -> None:
    _write_changeset(tmp_path)
    _fake_run_writer(monkeypatch)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope="files 5..9")
    assert executor(spec) is False
