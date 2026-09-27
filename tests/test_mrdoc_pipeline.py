"""Tests for app/mrdoc/dispatch + orchestrator — the wave loop contract.

The loop must be executor-agnostic: a fake agent that writes schema-valid
artifacts drives it to completion exactly like a satellite CLI would, and
a lying agent (returns True, writes nothing) must trip the no-progress
abort — the deterministic nodes never run past a missing dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.mrdoc import dispatch, satellites
from app.mrdoc.analysis import AnalysisUnit, FileAnalysis, render_analysis
from app.mrdoc.changeset import parse_changeset
from app.mrdoc.collect import EXPLANATION_FAILED, parse_collect
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
from app.mrdoc.workspace import artifact_paths, fix_rounds_used, work_dir


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
    fixes_round3: bool = False,
    omit_on_first_call: bool = False,
    fix_call_fails: bool = False,
    fix_target: str | None = None,
) -> Callable[[str], bool]:
    """A satellite double whose two gates can be made to complain on demand.

    The knobs exist so the retry branch can be driven from both sides: a
    verifier that files a FIX, and an analyzer whose 설명 trips the vocab gate
    — or whose first call leaves a unit out, or whose re-call crashes after
    it already scribbled over 20-analysis.
    """

    def run(spec_text: str) -> bool:
        mission = parse_spec(spec_text)
        work = mission.return_path.parent
        if mission.agent == "analyzer":
            fix_call = mission.scope.startswith("units ")
            if fix_call and fix_call_fails:
                for path in mission.return_path.glob("*.md"):
                    path.write_text("half-written by a crashed satellite", encoding="utf-8")
                return False
            return _write_analyses(
                mission,
                work,
                explanation,
                rewritten,
                omit_last=omit_on_first_call and not fix_call,
            )
        if mission.agent == "verifier":
            round_no = 1 + fix_rounds_used(work)
            flag = (fixes_round1, fixes_round2, fixes_round3)[round_no - 1]
            return _write_verifier(mission, work, round_no, flag, fix_target)
        if mission.agent == "themes":
            return _write_themes(mission, work)
        return False

    return run


def _write_analyses(
    mission, work: Path, explanation: str, rewritten: str, *, omit_last: bool = False
) -> bool:
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
        if omit_last:
            units = units[:-1]  # the block a real satellite sometimes forgets
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


def _write_verifier(
    mission, work: Path, round_no: int, flag: bool, fix_target: str | None = None
) -> bool:
    changeset = parse_changeset(_read(work, "00-changeset.md"))
    structure = parse_structure(_read(work, "05-structure.md"))
    # fix_target aims the FIX somewhere other than a unit, so the units
    # themselves pass — the FIX is the only finding
    judged = flag and fix_target is None
    units = tuple(
        VerifierUnit(
            unit_id=unit.unit_id,
            fidelity="omitted" if judged else "ok",
            class_stated="",
            class_opinion="agree",
            why="바뀐 줄 하나가 설명에 없다." if judged else "",
        )
        for unit in structure.changed
    )
    fixes = (
        (Fix("r-01", fix_target or units[0].unit_id, "설명", "fidelity_omitted"),)
        if flag and units
        else ()
    )
    report = Verifier(
        mr_iid=changeset.mr_iid,
        round=round_no,
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
        "01 / CHANGE MATRIX",
        "02 / WHAT CHANGED",
        "03 / ATTENTION",
        "04 / FILE CHANGES",
        "05 / ANALYSIS QUALITY",
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


def test_spent_retries_do_not_buy_another_call(tmp_path: Path) -> None:
    """The loop must end: after two retries the findings hit the report."""

    code, directory = _run(
        _agent(fixes_round1=True, fixes_round2=True, fixes_round3=True),
        tmp_path,
    )
    paths = artifact_paths(directory)
    ledger = paths["ledger"].read_text(encoding="utf-8")
    assert code == dispatch.EXIT_COMPLETE
    assert ledger.count("fix round 1") == 1
    assert ledger.count("fix round 2") == 1
    assert paths["verifier_r1"].is_file() and paths["verifier_r2"].is_file()
    collect = parse_frontmatter(_read(directory, "50-collect.md"))
    verify = collect["verify"]
    assert verify == {
        "status": "ok",
        "rounds": 3,
        "outstanding": 1,
        "counts_mismatch": 0,
        "audited": 1,
        "unaudited": 0,
    }


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
    verifier_text = render_verifier(
        Verifier(
            mr_iid=1,
            units=(VerifierUnit("u-a", "omitted", "", "agree"),),
            fixes=(Fix("r-01", "u-a", "설명", "fidelity_omitted"),),
        )
    )
    paths["verifier"].write_text(verifier_text, encoding="utf-8")
    levelcheck_text = render_levelcheck(
        LevelCheck(
            mr_iid=1,
            units=(
                LevelRow("u-b", "", "L2", "b", ("의미",), vocab_violation=("개선",)),
                LevelRow("u-c", "", "L2", "c", ("의미",)),
            ),
        )
    )
    paths["levelcheck"].write_text(levelcheck_text, encoding="utf-8")
    assert dispatch.fix_targets(directory) == ("u-a", "u-b")
    assert dispatch.fix_round_due(directory) == ("u-a", "u-b")
    dispatch.close_fix_round(directory)
    assert paths["verifier_r1"].is_file() and not paths["verifier"].is_file()
    # round 2's gates flagging again still buys the second — the last — re-call
    paths["verifier"].write_text(verifier_text, encoding="utf-8")
    paths["levelcheck"].write_text(levelcheck_text, encoding="utf-8")
    assert dispatch.fix_round_due(directory) == ("u-a", "u-b")
    dispatch.close_fix_round(directory)
    assert dispatch.fix_round_due(directory) == ()  # both spent, never again
    assert paths["verifier_r2"].is_file() and not paths["verifier"].is_file()


def test_missing_explanation_is_written_in_the_fix_round(tmp_path: Path) -> None:
    """A unit the first call forgot is a re-call target, not a permanent hole."""

    code, directory = _run(
        _agent(omit_on_first_call=True), tmp_path, _two_unit_inputs()
    )
    assert code == dispatch.EXIT_COMPLETE
    missing = parse_structure(_read(directory, "05-structure.md")).changed[-1].unit_id
    ledger = artifact_paths(directory)["ledger"].read_text(encoding="utf-8")
    assert f"fix round 1: {missing}" in ledger
    collect = parse_collect(_read(directory, "50-collect.md"))
    assert all(unit.explanation != EXPLANATION_FAILED for unit in collect.units)


def test_failed_fix_round_restores_and_still_reports(tmp_path: Path) -> None:
    """A crashed re-call costs the round, never the report it could not improve."""

    code, directory = _run(
        _agent(fixes_round1=True, fix_call_fails=True), tmp_path
    )
    paths = artifact_paths(directory)
    ledger = paths["ledger"].read_text(encoding="utf-8")
    assert code == dispatch.EXIT_COMPLETE
    assert "abort" not in ledger
    assert ledger.count("analyzer failed (fix round)") == 2  # both rounds spent
    assert paths["render"].is_file()
    for path in (directory / "20-analysis").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "half-written" not in text
        assert _CLEAN in text  # the pre-round prose is what the report prints
    verify = parse_frontmatter(_read(directory, "50-collect.md"))["verify"]
    assert verify["outstanding"] == 1  # the unanswered FIX still shows
    # themes waited for the owed second round instead of grouping in between
    assert ledger.index("fix round 2") < ledger.index("themes done")


def test_fix_round_that_raises_degrades_like_a_rejection(tmp_path: Path) -> None:
    inner = _agent(fixes_round1=True)

    def agent(spec_text: str) -> bool:
        if parse_spec(spec_text).scope.startswith("units "):
            raise RuntimeError("codex vanished mid-round")
        return inner(spec_text)

    code, directory = _run(agent, tmp_path)
    ledger = artifact_paths(directory)["ledger"].read_text(encoding="utf-8")
    assert code == dispatch.EXIT_COMPLETE
    assert "codex vanished mid-round" in ledger
    assert ledger.count("analyzer failed (fix round)") == 2
    assert artifact_paths(directory)["render"].is_file()


def test_fix_aimed_at_an_unknown_id_does_not_abort(tmp_path: Path) -> None:
    """Nothing to rewrite means no round — the FIX still counts as 잔존."""

    code, directory = _run(
        _agent(fixes_round1=True, fix_target="u-nosuch0"), tmp_path
    )
    paths = artifact_paths(directory)
    assert code == dispatch.EXIT_COMPLETE
    assert not paths["fix_marker"].exists()
    assert "fix round" not in paths["ledger"].read_text(encoding="utf-8")
    verify = parse_frontmatter(_read(directory, "50-collect.md"))["verify"]
    assert verify["outstanding"] == 1
    # no card can wear it, so 05 names it by the id the verifier wrote
    html = paths["render"].read_text(encoding="utf-8")
    assert "u-nosuch0 — 검증 지적 (가리키는 대상 없음)" in html


def test_fix_without_unit_block_marks_the_card_not_unaudited(tmp_path: Path) -> None:
    """A FIX alone is a verdict: 검증 지적 · 빠뜨림 on the card, never 미검증."""

    inner = _agent()

    def agent(spec_text: str) -> bool:
        mission = parse_spec(spec_text)
        if mission.agent != "verifier":
            return inner(spec_text)
        unit = parse_structure(_read(mission.return_path.parent, "05-structure.md"))
        target = unit.changed[0].unit_id
        report = Verifier(
            mr_iid=1, fixes=(Fix("r-01", target, "설명", "fidelity_omitted"),)
        )
        mission.return_path.write_text(render_verifier(report), encoding="utf-8")
        return True

    code, directory = _run(agent, tmp_path)
    assert code == dispatch.EXIT_COMPLETE
    verify = _verify_of(directory)
    assert (verify["outstanding"], verify["audited"], verify["unaudited"]) == (1, 1, 0)
    collect = parse_collect(_read(directory, "50-collect.md"))
    assert [unit.audit for unit in collect.units] == ["omitted"]
    html = artifact_paths(directory)["render"].read_text(encoding="utf-8")
    assert "검증 지적 · 빠뜨림" in html
    assert 'tag-audit-none">미검증<' not in html


def test_real_fix_round_prompt_carries_the_gates_reasons(tmp_path: Path) -> None:
    """The mission is built after the gates were archived — it must still see them."""

    captured: dict[str, object] = {}
    inner = _agent(fixes_round1=True)

    def agent(spec_text: str) -> bool:
        mission = parse_spec(spec_text)
        if mission.agent == "analyzer" and mission.scope.startswith("units "):
            plan = satellites._analyzer_mission(mission, mission.return_path.parent)
            captured["prompt"] = plan.prompt
            captured["unreadable"] = [path for path in mission.read if not path.exists()]
        return inner(spec_text)

    code, _directory = _run(agent, tmp_path)
    assert code == dispatch.EXIT_COMPLETE
    assert "fidelity_omitted" in str(captured["prompt"])
    assert captured["unreadable"] == []


def _verify_of(directory: Path) -> dict:
    return parse_frontmatter(_read(directory, "50-collect.md"))["verify"]


def test_verifier_that_writes_nothing_degrades_to_failed(tmp_path: Path) -> None:
    """A silent verifier costs the audit, not the report — and says so."""

    inner = _agent()

    def agent(spec_text: str) -> bool:
        if parse_spec(spec_text).agent == "verifier":
            return False  # nothing written
        return inner(spec_text)

    code, directory = _run(agent, tmp_path)
    paths = artifact_paths(directory)
    assert code == dispatch.EXIT_COMPLETE
    assert "verifier degraded" in paths["ledger"].read_text(encoding="utf-8")
    assert parse_verifier(_read(directory, "40-verifier.md")).is_stub
    assert _verify_of(directory)["status"] == "failed"
    summary = paths["slack_summary"].read_text(encoding="utf-8")
    assert "검증 실패" in summary and "지적 잔존 —" in summary


def test_unreadable_audit_is_failed_not_clean(tmp_path: Path) -> None:
    """The measured hole: garbage in 40-verifier used to read as zero findings."""

    inner = _agent()

    def agent(spec_text: str) -> bool:
        mission = parse_spec(spec_text)
        if mission.agent == "verifier":
            mission.return_path.write_text("검사 완료, 문제 없음.\n", encoding="utf-8")
            return True
        return inner(spec_text)

    code, directory = _run(agent, tmp_path)
    assert code == dispatch.EXIT_COMPLETE
    verify = _verify_of(directory)
    assert (verify["status"], verify["unaudited"]) == ("failed", 1)
    html = artifact_paths(directory)["render"].read_text(encoding="utf-8")
    assert "확인이 필요한 항목 없음" not in html
    assert "<h3>검증 실패</h3>" in html


def test_partial_audit_marks_the_skipped_unit(tmp_path: Path) -> None:
    inner = _agent()

    def agent(spec_text: str) -> bool:
        mission = parse_spec(spec_text)
        if mission.agent != "verifier":
            return inner(spec_text)
        first = parse_structure(_read(mission.return_path.parent, "05-structure.md"))
        report = Verifier(
            mr_iid=1,
            units=(VerifierUnit(first.changed[0].unit_id, "ok", "", "agree"),),
        )
        mission.return_path.write_text(render_verifier(report), encoding="utf-8")
        return True

    code, directory = _run(agent, tmp_path, _two_unit_inputs())
    assert code == dispatch.EXIT_COMPLETE
    verify = _verify_of(directory)
    assert (verify["status"], verify["audited"], verify["unaudited"]) == ("ok", 1, 1)
    collect = parse_collect(_read(directory, "50-collect.md"))
    assert [unit.audit for unit in collect.units] == ["ok", "unaudited"]
    paths = artifact_paths(directory)
    assert "미검증 1" in paths["slack_summary"].read_text(encoding="utf-8")
    html = paths["render"].read_text(encoding="utf-8")
    assert html.count('class="pill tag-audit-none">미검증<') == 1
    assert "<h3>미검증 1유닛</h3>" in html


def test_outstanding_finding_is_marked_on_its_card(tmp_path: Path) -> None:
    """지적 잔존 used to be a bare count — the reader now lands on the unit."""

    code, directory = _run(
        _agent(fixes_round1=True, fixes_round2=True, fixes_round3=True), tmp_path
    )
    assert code == dispatch.EXIT_COMPLETE
    html = artifact_paths(directory)["render"].read_text(encoding="utf-8")
    assert "검증 지적 · 빠뜨림" in html
    assert "검증 근거 — 바뀐 줄 하나가 설명에 없다." in html
    assert '<details class="change-card" open>' in html


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
