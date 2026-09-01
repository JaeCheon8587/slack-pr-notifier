"""Tests for app/mrdoc/dispatch + orchestrator — the wave loop contract.

The loop must be executor-agnostic: a fake agent that writes artifacts
drives it to completion exactly like a satellite CLI would, and a lying
agent (returns True, writes nothing) must trip the no-progress abort.
"""

from __future__ import annotations

from pathlib import Path

from app.mrdoc import dispatch
from app.mrdoc.orchestrator import PipelineInputs, run_to_completion
from app.mrdoc.workspace import artifact_paths, work_dir


def test_work_dir_is_push_scoped() -> None:
    first = work_dir(Path("ws"), 12, "a" * 40)
    second = work_dir(Path("ws"), 12, "b" * 40)
    assert first != second
    assert first.name.startswith("12-") and len(first.name) == len("12-") + 8


def _inputs() -> PipelineInputs:
    return PipelineInputs(
        mr_iid=1,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=[
            {
                "filename": "docs/a.md",
                "previous_filename": None,
                "status": "modified",
                "patch": "@@ -1,3 +1,3 @@",
            }
        ],
        base_tree={"docs/a.md": "# 개요\n본문\n\n# 설정\n30초\n"},
        head_tree={"docs/a.md": "# 개요\n본문\n\n# 구성\n60초\n"},
    )


def _fake_agent(spec: str) -> bool:
    lines = spec.splitlines()
    agent = lines[1].split()[1]
    target = Path(lines[4].split(" ", 1)[1])
    if agent == "analyzer":
        target.mkdir(parents=True, exist_ok=True)
        (target / "a.md").write_text("analysis", encoding="utf-8")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done", encoding="utf-8")
    return True


def test_run_to_completion_reaches_exit_4(tmp_path: Path) -> None:
    directory = tmp_path / ".work" / "1-h"
    code = run_to_completion(
        _inputs(), directory, _fake_agent, fanout=5, budget_usd=1.0
    )
    assert code == dispatch.EXIT_COMPLETE
    paths = artifact_paths(directory)
    for key in (
        "changeset",
        "structure",
        "literals",
        "levelcheck",
        "verifier",
        "collect",
        "reporter",
        "render",
    ):
        assert paths[key].exists(), key
    ledger = paths["ledger"].read_text(encoding="utf-8")
    assert "abort" not in ledger
    assert "pipeline complete" in ledger


def test_failure_agent_aborts_with_exit_2(tmp_path: Path) -> None:
    directory = tmp_path / ".work" / "1-h"

    def lying_agent(spec: str) -> bool:
        return True  # claims success, writes nothing

    code = run_to_completion(
        _inputs(), directory, lying_agent, fanout=5, budget_usd=1.0
    )
    assert code == dispatch.EXIT_ABORT
    ledger = artifact_paths(directory)["ledger"].read_text(encoding="utf-8")
    assert "no progress" in ledger


def test_exception_in_agent_aborts(tmp_path: Path) -> None:
    def exploding_agent(spec: str) -> bool:
        raise RuntimeError("satellite crashed")

    code = run_to_completion(
        _inputs(), tmp_path / "w", exploding_agent, fanout=5, budget_usd=1.0
    )
    assert code == dispatch.EXIT_ABORT
    ledger = artifact_paths(tmp_path / "w")["ledger"].read_text(encoding="utf-8")
    assert "satellite crashed" in ledger


def test_spec_block_render_format(tmp_path: Path) -> None:
    directory = tmp_path / "w"
    directory.mkdir()
    specs = dispatch.next_specs(directory, wave=3, fanout=5, budget_usd=1.5)
    assert specs == []  # nothing runnable yet — changeset missing
    artifact_paths(directory)["changeset"].write_text("x", encoding="utf-8")
    specs = dispatch.next_specs(directory, wave=3, fanout=5, budget_usd=1.5)
    rendered = specs[0].render()
    assert rendered.startswith("WAVE 3\nSPEC ")
    assert "BUDGET 1.5" in rendered
    assert "RETURN " in rendered


def test_fanout_batches_analysis_specs(tmp_path: Path) -> None:
    directory = tmp_path / "w"
    directory.mkdir()
    artifact_paths(directory)["changeset"].write_text("x", encoding="utf-8")
    (directory / ".analysis-expected").write_text("12", encoding="utf-8")
    specs = dispatch.next_specs(directory, wave=1, fanout=5, budget_usd=1.0)
    analyzer_specs = [s for s in specs if s.agent == "analyzer"]
    assert len(analyzer_specs) == 3  # 12 files / 5 per batch
    assert [s.scope for s in analyzer_specs] == [
        "files 0..4",
        "files 5..9",
        "files 10..11",
    ]
