"""Tests for app/mrdoc/dispatch + orchestrator — the wave loop contract.

The loop must be executor-agnostic: a fake agent that writes schema-valid
artifacts drives it to completion exactly like a satellite CLI would, and
a lying agent (returns True, writes nothing) must trip the no-progress
abort — the deterministic nodes never run past a missing dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.mrdoc import dispatch
from app.mrdoc.analysis import AnalysisUnit, FileAnalysis, render_analysis
from app.mrdoc.changeset import parse_changeset
from app.mrdoc.excerpt import parse_excerpts, render_excerpts
from app.mrdoc.frontmatter import parse_frontmatter
from app.mrdoc.levelcheck import (
    LevelCheck,
    LevelRow,
    parse_levelcheck,
    render_levelcheck,
)
from app.mrdoc.orchestrator import PipelineInputs, run_to_completion
from app.mrdoc.satellites import parse_spec
from app.mrdoc.structure import parse_structure
from app.mrdoc.themes import Theme, Themes, render_themes
from app.mrdoc.verifier import (
    Fix,
    Verifier,
    VerifierUnit,
    parse_verifier,
    render_verifier,
)
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


_CLEAN = "타임아웃 값이 30초 에서 60초 로 바뀌었다."


def _agent(
    *,
    explanation: str = _CLEAN,
    rewritten: str = _CLEAN,
    fixes_round1: bool = False,
    fixes_round2: bool = False,
) -> Callable[[str], bool]:
    """A satellite double whose two gates can be made to complain on demand.

    The knobs exist so the retry branch can be driven from both sides: a
    verifier that files a FIX, and an analyzer whose 설명 trips the vocab gate.
    """

    def run(spec_text: str) -> bool:
        mission = parse_spec(spec_text)
        work = mission.return_path.parent
        if mission.agent == "analyzer":
            return _write_analyses(mission, work, explanation, rewritten)
        if mission.agent == "verifier":
            round_two = (work / "40-verifier.r1.md").is_file()
            flag = fixes_round2 if round_two else fixes_round1
            return _write_verifier(mission, work, round_two, flag)
        if mission.agent == "themes":
            return _write_themes(mission, work)
        return False

    return run


def _write_analyses(mission, work: Path, explanation: str, rewritten: str) -> bool:
    structure = parse_structure(_read(work, "05-structure.md"))
    changeset = parse_changeset(_read(work, "00-changeset.md"))
    path_by_fid = {entry.fid: entry.path for entry in changeset.files}
    targets = (
        {
            part.strip()
            for part in mission.scope.partition("units ")[2].split(",")
            if part.strip()
        }
        if mission.scope.startswith("units ")
        else None
    )
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
                    klass="",
                    explanation=(
                        rewritten
                        if targets is not None and unit.unit_id in targets  # type: ignore[attr-defined]
                        else explanation
                    ),
                )
                for unit in units
            ),
            summary_refs=tuple(
                unit.unit_id for unit in units  # type: ignore[attr-defined]
            ),
            summary="값이 바뀐 절 1곳.",
            confidence="high",
        )
        (mission.return_path / f"{fid}.md").write_text(
            render_analysis(analysis), encoding="utf-8"
        )
    return True


def _write_verifier(mission, work: Path, round_two: bool, flag: bool) -> bool:
    changeset = parse_changeset(_read(work, "00-changeset.md"))
    structure = parse_structure(_read(work, "05-structure.md"))
    units = tuple(
        VerifierUnit(
            unit_id=unit.unit_id,
            fidelity="omitted" if flag else "ok",
            class_stated="",
            class_opinion="agree",
            why="바뀐 줄 하나가 설명에 없다." if flag else "",
        )
        for unit in structure.changed
    )
    fixes = (
        (Fix("r-01", units[0].unit_id, "설명", "fidelity_omitted"),)
        if flag and units
        else ()
    )
    report = Verifier(
        mr_iid=changeset.mr_iid,
        round=2 if round_two else 1,
        checked=len(units),
        confidence="high",
        units=units,
        fixes=fixes,
    )
    mission.return_path.write_text(render_verifier(report), encoding="utf-8")
    return True


def _write_themes(mission, work: Path) -> bool:
    """One cross-cutting theme when the units allow it, none when they don't."""

    changeset = parse_changeset(_read(work, "00-changeset.md"))
    structure = parse_structure(_read(work, "05-structure.md"))
    unit_ids = tuple(unit.unit_id for unit in structure.changed)
    themes: tuple[Theme, ...] = ()
    if len(unit_ids) >= 2:
        themes = (
            Theme(
                theme_id="t-01",
                title="타임아웃 정책",
                line=_CLEAN,
                units=unit_ids,
            ),
        )
    report = Themes(mr_iid=changeset.mr_iid, themes=themes)
    mission.return_path.write_text(render_themes(report), encoding="utf-8")
    return True


#: The quiet path — both gates clean, so the retry branch never fires.
_fake_agent = _agent()


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
        "excerpt",
        "levelcheck",
        "verifier",
        "themes",
        "collect",
        "render",
        "slack_summary",
    ):
        assert paths[key].exists(), key
    ledger = paths["ledger"].read_text(encoding="utf-8")
    assert "abort" not in ledger
    assert "pipeline complete" in ledger
    html = paths["render"].read_text(encoding="utf-8")
    for heading in (
        "1. 개요",
        "2. 주요 변경사항",
        "3. 변경 매트릭스",
        "4. 파일별 상세",
        "5. 분석 상태",
        "6. 부록",
    ):
        assert heading in html, heading
    assert _CLEAN in html  # the 20-analysis 설명 reached the page
    # slack-summary.txt is section 1 and carries no document text
    summary = paths["slack_summary"].read_text(encoding="utf-8")
    assert "[리포트 신뢰도]" in summary
    assert "60초" not in summary


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


def test_themes_failure_degrades_instead_of_aborting(tmp_path: Path) -> None:
    """A themes satellite that wrote nothing costs section 2, not the MR."""

    def themes_down(spec: str) -> bool:
        mission = parse_spec(spec)
        if mission.agent != "themes":
            return _fake_agent(spec)
        return False  # rejected, artifact missing

    directory = tmp_path / ".work" / "1-h"
    code = run_to_completion(
        _inputs(), directory, themes_down, fanout=5, budget_usd=1.0
    )
    assert code == dispatch.EXIT_COMPLETE
    paths = artifact_paths(directory)
    ledger = paths["ledger"].read_text(encoding="utf-8")
    assert "themes degraded" in ledger
    assert "주제 생성 실패" in paths["render"].read_text(encoding="utf-8")


def test_themes_rejected_artifact_is_kept_and_degrades(tmp_path: Path) -> None:
    def themes_rejected(spec: str) -> bool:
        mission = parse_spec(spec)
        if mission.agent != "themes":
            return _fake_agent(spec)
        mission.return_path.write_text("주제 없음.\n", encoding="utf-8")
        return False

    directory = tmp_path / ".work" / "1-h"
    code = run_to_completion(
        _inputs(), directory, themes_rejected, fanout=5, budget_usd=1.0
    )
    assert code == dispatch.EXIT_COMPLETE
    paths = artifact_paths(directory)
    assert paths["themes"].read_text(encoding="utf-8") == "주제 없음.\n"
    assert "themes degraded" in paths["ledger"].read_text(encoding="utf-8")
    assert "주제 생성 실패" in paths["render"].read_text(encoding="utf-8")


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


def test_excerpt_waits_for_literals_then_becomes_runnable(tmp_path: Path) -> None:
    directory = tmp_path / "w"
    directory.mkdir()
    paths = artifact_paths(directory)
    paths["changeset"].write_text("x", encoding="utf-8")
    paths["structure"].write_text("x", encoding="utf-8")
    assert "excerpt" not in dispatch.runnable_nodes(dispatch.derive_state(directory))
    paths["literals"].write_text("x", encoding="utf-8")
    assert "excerpt" in dispatch.runnable_nodes(dispatch.derive_state(directory))


def test_analyzer_spec_reads_the_excerpts(tmp_path: Path) -> None:
    """07 is the analyzer's evidence — a spec without it invites recall."""

    directory = tmp_path / "w"
    directory.mkdir()
    artifact_paths(directory)["changeset"].write_text("x", encoding="utf-8")
    (directory / ".analysis-expected").write_text("1", encoding="utf-8")
    specs = dispatch.next_specs(directory, wave=1, fanout=5, budget_usd=1.0)
    analyzer = next(spec for spec in specs if spec.agent == "analyzer")
    assert artifact_paths(directory)["excerpt"] in analyzer.read


def test_excerpt_artifact_round_trips_through_the_wave(tmp_path: Path) -> None:
    directory = tmp_path / ".work" / "1-h"
    run_to_completion(_inputs(), directory, _fake_agent, fanout=5, budget_usd=1.0)
    text = artifact_paths(directory)["excerpt"].read_text(encoding="utf-8")
    excerpts = parse_excerpts(text)
    assert render_excerpts(excerpts) == text
    assert [unit.kind for unit in excerpts.units] == ["changed"]
    assert "30초" in excerpts.units[0].before
    assert "60초" in excerpts.units[0].after


def _two_unit_inputs() -> PipelineInputs:
    """Two changed sections in one file — enough to notice a lost block."""

    inputs = _inputs()
    return PipelineInputs(
        mr_iid=inputs.mr_iid,
        project_id=inputs.project_id,
        base_sha=inputs.base_sha,
        head_sha=inputs.head_sha,
        start_sha=inputs.start_sha,
        raw_files=[
            {
                "filename": "docs/a.md",
                "previous_filename": None,
                "status": "modified",
                "patch": "@@ -1,5 +1,5 @@",
            }
        ],
        base_tree={"docs/a.md": "# 개요\n30초\n\n# 설정\n5회\n"},
        head_tree={"docs/a.md": "# 개요\n60초\n\n# 설정\n10회\n"},
    )


def _run(agent, tmp_path: Path, inputs: PipelineInputs | None = None):
    directory = tmp_path / ".work" / "1-h"
    code = run_to_completion(
        inputs or _inputs(), directory, agent, fanout=5, budget_usd=1.0
    )
    return code, directory


def test_clean_run_never_opens_a_fix_round(tmp_path: Path) -> None:
    code, directory = _run(_agent(), tmp_path)
    paths = artifact_paths(directory)
    assert code == dispatch.EXIT_COMPLETE
    assert not paths["fix_marker"].exists()
    assert not paths["verifier_r1"].exists()
    assert "fix round" not in paths["ledger"].read_text(encoding="utf-8")


def test_verifier_fix_triggers_exactly_one_re_analysis(tmp_path: Path) -> None:
    redone = "타임아웃 값이 30초 에서 60초 로 바뀌고 단위 표기가 통일되었다."
    code, directory = _run(_agent(rewritten=redone, fixes_round1=True), tmp_path)
    paths = artifact_paths(directory)
    assert code == dispatch.EXIT_COMPLETE
    assert paths["fix_marker"].exists()
    assert paths["verifier_r1"].exists()  # round 1's audit is kept, not lost
    assert paths["levelcheck_r1"].exists()
    ledger = paths["ledger"].read_text(encoding="utf-8")
    assert ledger.count("fix round 1") == 1
    assert "abort" not in ledger
    # the rewrite landed in 20-analysis, and round 2 signed off on it
    written = (directory / "20-analysis").glob("*.md")
    assert any(redone in path.read_text(encoding="utf-8") for path in written)
    assert parse_verifier(_read(directory, "40-verifier.md")).round == 2


def test_second_round_does_not_buy_another_call(tmp_path: Path) -> None:
    """The loop must end: round 2's fixes go to the report, not the analyzer."""

    code, directory = _run(
        _agent(fixes_round1=True, fixes_round2=True), tmp_path
    )
    paths = artifact_paths(directory)
    assert code == dispatch.EXIT_COMPLETE
    assert paths["ledger"].read_text(encoding="utf-8").count("fix round 1") == 1
    collect = parse_frontmatter(_read(directory, "50-collect.md"))
    verify = collect["verify"]
    assert verify == {"rounds": 2, "outstanding": 1, "counts_mismatch": 0}


def test_vocab_violation_alone_opens_the_fix_round(tmp_path: Path) -> None:
    """levelcheck's gate is the other half of the union, per the design."""

    code, directory = _run(
        _agent(explanation="값이 상향되어 문서가 개선되었다.", rewritten=_CLEAN),
        tmp_path,
    )
    paths = artifact_paths(directory)
    assert code == dispatch.EXIT_COMPLETE
    assert paths["fix_marker"].exists()
    assert "fix round 1" in paths["ledger"].read_text(encoding="utf-8")
    levelcheck = parse_levelcheck(_read(directory, "30-levelcheck.md"))
    assert levelcheck.vocab_violations == 0  # the rewrite cleared it
    archived = parse_levelcheck(_read(directory, "30-levelcheck.r1.md"))
    assert archived.vocab_violations == 1


def test_fix_targets_union_both_gates(tmp_path: Path) -> None:
    directory = tmp_path / "w"
    directory.mkdir()
    paths = artifact_paths(directory)
    paths["verifier"].write_text(
        render_verifier(
            Verifier(
                mr_iid=1,
                units=(VerifierUnit("u-a", "omitted", "", "agree"),),
                fixes=(Fix("r-01", "u-a", "설명", "fidelity_omitted"),),
            )
        ),
        encoding="utf-8",
    )
    paths["levelcheck"].write_text(
        render_levelcheck(
            LevelCheck(
                mr_iid=1,
                units=(
                    LevelRow("u-b", "", "L2", "b", ("의미",), vocab_violation=("개선",)),
                    LevelRow("u-c", "", "L2", "c", ("의미",)),
                ),
            )
        ),
        encoding="utf-8",
    )
    assert dispatch.fix_targets(directory) == ("u-a", "u-b")
    assert dispatch.fix_round_due(directory) == ("u-a", "u-b")
    dispatch.close_fix_round(directory)
    assert dispatch.fix_round_due(directory) == ()  # spent, never again
    assert paths["verifier_r1"].is_file() and not paths["verifier"].is_file()


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
