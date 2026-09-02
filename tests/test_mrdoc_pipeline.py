"""Tests for app/mrdoc/dispatch + orchestrator — the wave loop contract.

The loop must be executor-agnostic: a fake agent that writes schema-valid
artifacts drives it to completion exactly like a satellite CLI would, and
a lying agent (returns True, writes nothing) must trip the no-progress
abort — the deterministic nodes never run past a missing dependency.
"""

from __future__ import annotations

from pathlib import Path

from app.mrdoc import dispatch
from app.mrdoc.analysis import AnalysisUnit, FileAnalysis, render_analysis
from app.mrdoc.changeset import parse_changeset
from app.mrdoc.frontmatter import parse_frontmatter, render_frontmatter, render_section
from app.mrdoc.orchestrator import PipelineInputs, run_to_completion
from app.mrdoc.satellites import parse_spec
from app.mrdoc.structure import parse_structure
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


def _read(work: Path, name: str) -> str:
    return (work / name).read_text(encoding="utf-8")


def _fake_agent(spec_text: str) -> bool:
    """Write the schema-valid artifact each spec promises — like a satellite."""

    mission = parse_spec(spec_text)
    work = mission.return_path.parent
    if mission.agent == "analyzer":
        structure = parse_structure(_read(work, "05-structure.md"))
        changeset = parse_changeset(_read(work, "00-changeset.md"))
        path_by_fid = {entry.fid: entry.path for entry in changeset.files}
        grouped: dict[str, list[object]] = {}
        for unit in structure.changed:
            grouped.setdefault(unit.file_id, []).append(unit)
        mission.return_path.mkdir(parents=True, exist_ok=True)
        for fid, units in grouped.items():
            analysis = FileAnalysis(
                file_id=fid,
                path=path_by_fid.get(fid, fid),
                units=tuple(
                    AnalysisUnit(
                        unit_id=unit.unit_id,  # type: ignore[attr-defined]
                        section_id=unit.section_id,  # type: ignore[attr-defined]
                        level="L2",
                        before="이전 안내",
                        after="이후 안내",
                    )
                    for unit in units
                ),
                confidence="high",
            )
            (mission.return_path / f"{fid}.md").write_text(
                render_analysis(analysis), encoding="utf-8"
            )
        return True
    if mission.agent == "verifier":
        changeset = parse_changeset(_read(work, "00-changeset.md"))
        structure = parse_structure(_read(work, "05-structure.md"))
        parts = [
            render_frontmatter(
                {
                    "mr_iid": changeset.mr_iid,
                    "round": 1,
                    "verdict": "APPROVE",
                    "checked": len(structure.changed),
                    "fidelity": {
                        "ok": len(structure.changed),
                        "distorted": 0,
                        "omitted": 0,
                    },
                    "levels": {"agree": len(structure.changed), "dispute": 0},
                    "required_fixes": 0,
                    "uncovered": "none",
                    "uncertain": "none",
                    "confidence": "high",
                }
            )
        ]
        for unit in structure.changed:
            parts.append("")
            parts.append(
                render_section(
                    "UNIT",
                    unit.unit_id,
                    {
                        "fidelity": "ok",
                        "level_claimed": "L2",
                        "level_opinion": "agree",
                    },
                )
            )
        mission.return_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
        return True
    if mission.agent == "reporter":
        verdict = str(parse_frontmatter(_read(work, "50-collect.md")).get("verdict", ""))
        parts = [
            render_frontmatter(
                {
                    "verdict": verdict,
                    "sentences": 2,
                    "STATUS": "OK",
                    "SOURCES": '"1/1 대응"',
                    "UNSOURCED": "none",
                    "CONFLICTS": "none",
                    "UNCOVERED": "none",
                    "CONFIDENCE": "high",
                }
            ),
            "",
            render_section("HEADLINE", "", {"refs": []}),
            "정상 흐름 요약 문장.",
            "",
            render_section("VERDICT_REASON", "", {"refs": []}),
            "verdict 사유 문장.",
        ]
        mission.return_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
        return True
    return False


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
    html = paths["render"].read_text(encoding="utf-8")
    assert "정상 흐름 요약 문장." in html
    assert "값 변경 (L2)" in html


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
