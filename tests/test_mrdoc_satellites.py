"""Tests for app/mrdoc/satellites.py — spec parsing + executor contract."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.mrdoc import satellites, vocab
from app.mrdoc.analysis import (
    AnalysisUnit,
    FileAnalysis,
    parse_analysis,
    render_analysis,
)
from app.mrdoc.changeset import build_changeset, render_changeset
from app.mrdoc.dispatch import SpecBlock
from app.mrdoc.literals import build_literals, render_literals
from app.mrdoc.frontmatter import parse_sections
from app.mrdoc.satellites import Mission, parse_spec
from app.mrdoc.structure import build_structure, render_structure
from app.mrdoc.verifier import (
    CountMismatch,
    CountsCheck,
    Fix,
    Verifier,
    VerifierUnit,
    render_verifier,
)


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


_BASE = {"docs/a.md": "# 설정\n타임아웃 30초\n"}
_HEAD = {"docs/a.md": "# 설정\n타임아웃 60초\n"}


def _changeset():
    return build_changeset(
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


def _write_inputs(work: Path) -> None:
    """00-changeset + 05-structure + 06-literals — what the mission builders read."""

    changeset = _changeset()
    (work / "00-changeset.md").write_text(render_changeset(changeset), encoding="utf-8")
    structure = build_structure(_BASE, _HEAD, changeset)
    (work / "05-structure.md").write_text(render_structure(structure), encoding="utf-8")
    literals = build_literals(structure, changeset, _BASE, _HEAD)
    (work / "06-literals.md").write_text(render_literals(literals), encoding="utf-8")


def test_executor_verifier_success(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    _write_inputs(tmp_path)
    _fake_run_writer(monkeypatch, captured)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    assert executor(_spec("verifier", tmp_path / "40-verifier.md")) is True
    assert (tmp_path / "40-verifier.md").read_text(encoding="utf-8") == "written by fake"
    cmd = captured["cmd"]
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert "workspace-write" in cmd
    assert "--ephemeral" in cmd


def _analyzer_prompt(work: Path, scope: str = "files 0..0") -> tuple[str, str]:
    """(system prompt, user prompt) the executor would actually send."""

    _write_inputs(work)
    mission = parse_spec(_spec("analyzer", work / "20-analysis", scope=scope))
    plan = satellites._analyzer_mission(mission, work)
    return plan.system, plan.prompt


def test_analyzer_template_is_what_the_parser_reads(tmp_path) -> None:
    """Drift gate: the exact string in the prompt must survive parse_analysis.

    The measured failure was a hand-written template the parser rejected —
    tests passed because they only ever parsed renderer output, never the
    prompt's own copy.
    """

    _system, prompt = _analyzer_prompt(tmp_path)
    template = satellites._ANALYZER_TEMPLATE
    assert template in prompt
    parsed = parse_analysis(template)
    assert [unit.unit_id for unit in parsed.units] == ["u-xxxxxxxx", "u-yyyyyyyy"]
    assert [unit.klass for unit in parsed.units] == ["", "표현"]
    assert parsed.summary_refs == ("u-xxxxxxxx", "u-yyyyyyyy")


def test_analyzer_template_uses_inline_flow_for_list_objects(tmp_path) -> None:
    """Block sequences ('key:' then '- {...}') are what the parser refuses."""

    _system, prompt = _analyzer_prompt(tmp_path)
    assert "refs: [" in prompt
    assert "\n  - {" not in prompt


def test_analyzer_prompt_shares_the_vocab_gate_list(tmp_path) -> None:
    system, _prompt = _analyzer_prompt(tmp_path)
    for term in vocab.BANNED_TERMS:
        assert term in system


def test_analyzer_prompt_offers_only_the_v2_schema(tmp_path) -> None:
    """The template is the contract — it must have nowhere to file a ruling."""

    system, prompt = _analyzer_prompt(tmp_path)
    text = system + prompt
    for gone in ("category", "evidence", "L1", "L2", "L3"):
        assert gone not in text, gone
    blocks = parse_sections(satellites._ANALYZER_TEMPLATE)
    assert {tuple(fields) for fields in blocks.values()} == {
        ("section_id", "class", "topic_hint"),
        ("refs",),
    }


def test_analyzer_prompt_names_the_units_and_the_excerpts(tmp_path) -> None:
    _system, prompt = _analyzer_prompt(tmp_path)
    structure = build_structure(_BASE, _HEAD, _changeset())
    for unit in structure.changed:
        assert unit.unit_id in prompt
    assert "07-excerpts.md" in prompt
    assert "06-literals.md" in prompt
    assert "05-structure.md" in prompt


def test_executor_analyzer_writes_batch_files(tmp_path, monkeypatch) -> None:
    _write_inputs(tmp_path)
    _fake_run_writer(monkeypatch)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope="files 0..0")
    assert executor(spec) is True
    written = list((tmp_path / "20-analysis").glob("*.md"))
    assert len(written) == 1


_TWO_BASE = {"docs/a.md": "# 개요\n30초\n\n# 설정\n5회\n"}
_TWO_HEAD = {"docs/a.md": "# 개요\n60초\n\n# 설정\n10회\n"}


def _two_unit_workspace(work: Path) -> tuple[str, list[str]]:
    """A file with two change units, already analyzed. Returns (file_id, units)."""

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
                "patch": "@@ -1,5 +1,5 @@",
            }
        ],
    )
    (work / "00-changeset.md").write_text(render_changeset(changeset), encoding="utf-8")
    structure = build_structure(_TWO_BASE, _TWO_HEAD, changeset)
    (work / "05-structure.md").write_text(render_structure(structure), encoding="utf-8")
    literals = build_literals(structure, changeset, _TWO_BASE, _TWO_HEAD)
    (work / "06-literals.md").write_text(render_literals(literals), encoding="utf-8")
    fid = structure.changed[0].file_id
    units = [unit.unit_id for unit in structure.changed]
    assert len(units) == 2
    analysis = FileAnalysis(
        file_id=fid,
        path="docs/a.md",
        units=tuple(
            AnalysisUnit(
                unit_id=unit.unit_id,
                section_id=unit.section_id,
                klass="",
                explanation="값이 바뀌었다.",
            )
            for unit in structure.changed
        ),
        summary_refs=tuple(units),
        summary="값이 바뀐 절 2곳.",
    )
    out = work / "20-analysis"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{fid}.md").write_text(render_analysis(analysis), encoding="utf-8")
    return fid, units


def _rewriter(monkeypatch, build: Callable[[str, list[str]], FileAnalysis]):
    """Fake codex that replaces the analysis file with whatever `build` says."""

    def fake_run(cmd, **kwargs):
        cwd = Path(str(kwargs.get("cwd")))
        for line in str(kwargs.get("input") or "").splitlines():
            if not line.startswith("- file_id "):
                continue
            fid = line.split()[2]
            path = cwd / "20-analysis" / f"{fid}.md"
            prior = parse_analysis(path.read_text(encoding="utf-8"))
            path.write_text(
                render_analysis(build(fid, [u.unit_id for u in prior.units])),
                encoding="utf-8",
            )
        return _proc()

    monkeypatch.setattr(satellites.subprocess, "run", fake_run)


def _analysis_of(fid: str, unit_ids: list[str]) -> FileAnalysis:
    return FileAnalysis(
        file_id=fid,
        path="docs/a.md",
        units=tuple(
            AnalysisUnit(
                unit_id=uid, section_id="s-x", klass="", explanation="다시 썼다."
            )
            for uid in unit_ids
        ),
        summary_refs=tuple(dict.fromkeys(unit_ids)),
        summary="다시 쓴 요약.",
    )


def test_fix_round_keeps_the_units_it_was_not_asked_to_touch(
    tmp_path, monkeypatch
) -> None:
    _fid, units = _two_unit_workspace(tmp_path)
    _rewriter(monkeypatch, lambda fid, ids: _analysis_of(fid, ids))
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope=f"units {units[0]}")
    assert executor(spec) is True


def test_fix_round_rejects_a_rewrite_that_drops_other_units(
    tmp_path, monkeypatch
) -> None:
    """Overwriting the file with only the targeted unit loses the rest."""

    _fid, units = _two_unit_workspace(tmp_path)
    _rewriter(monkeypatch, lambda fid, ids: _analysis_of(fid, [units[0]]))
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope=f"units {units[0]}")
    assert executor(spec) is False


def test_fix_round_rejects_duplicate_unit_ids(tmp_path, monkeypatch) -> None:
    """Two blocks under one id: the yaml lookup keeps only the second."""

    _fid, units = _two_unit_workspace(tmp_path)
    _rewriter(monkeypatch, lambda fid, ids: _analysis_of(fid, [*ids, units[0]]))
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope=f"units {units[0]}")
    assert executor(spec) is False


def test_first_call_rejects_duplicate_unit_ids(tmp_path, monkeypatch) -> None:
    """The doubled-id check is not the re-call's alone — the first call has it.

    Nothing was overwritten here, so the preservation half has nothing to
    say; the id still keys the yaml lookup, so one of the two 설명 is lost
    inside the artifact the wave is about to accept.
    """

    _write_inputs(tmp_path)
    unit = build_structure(_BASE, _HEAD, _changeset()).changed[0]

    def fake_run(cmd, **kwargs):
        out = Path(str(kwargs.get("cwd"))) / "20-analysis"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{unit.file_id}.md").write_text(
            render_analysis(
                _analysis_of(unit.file_id, [unit.unit_id, unit.unit_id])
            ),
            encoding="utf-8",
        )
        return _proc()

    monkeypatch.setattr(satellites.subprocess, "run", fake_run)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope="files 0..0")
    assert executor(spec) is False


def test_first_call_tolerates_an_unparseable_file(tmp_path, monkeypatch) -> None:
    """A file with no readable 설명 is isolated downstream, not a wave abort."""

    _write_inputs(tmp_path)
    _fake_run_writer(monkeypatch)  # writes 'written by fake' — no UNIT blocks
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope="files 0..0")
    assert executor(spec) is True


def test_fix_prompt_names_the_targets_and_the_preservation_rule(tmp_path) -> None:
    _fid, units = _two_unit_workspace(tmp_path)
    (tmp_path / "40-verifier.md").write_text(
        render_verifier(
            Verifier(
                mr_iid=17,
                units=(VerifierUnit(units[0], "omitted", "", "agree"),),
                fixes=(Fix("r-01", units[0], "설명", "fidelity_omitted"),),
            )
        ),
        encoding="utf-8",
    )
    mission = parse_spec(
        _spec("analyzer", tmp_path / "20-analysis", scope=f"units {units[0]}")
    )
    plan = satellites._analyzer_mission(mission, tmp_path)
    assert units[0] in plan.prompt
    assert "fidelity_omitted" in plan.prompt  # the reason came from the file
    assert "보존" in plan.prompt
    assert plan.expected == (tmp_path / "20-analysis" / f"{_fid}.md",)


def test_fix_prompt_rewrites_a_file_summary_on_counts_mismatch(tmp_path) -> None:
    """counts_mismatch FIX 의 대상은 file_id — 요약 문단 재작성 미션을 탄다."""

    _fid, _units = _two_unit_workspace(tmp_path)
    (tmp_path / "40-verifier.md").write_text(
        render_verifier(
            Verifier(
                mr_iid=17,
                counts=(
                    CountsCheck(
                        file_id=_fid,
                        mismatches=(CountMismatch(_fid, "삭제 0", "삭제 2"),),
                        why="FILE_SUMMARY 가 삭제를 부정했다.",
                    ),
                ),
                fixes=(Fix("r-02", _fid, "FILE_SUMMARY", "counts_mismatch"),),
            )
        ),
        encoding="utf-8",
    )
    mission = parse_spec(
        _spec("analyzer", tmp_path / "20-analysis", scope=f"units {_fid}")
    )
    plan = satellites._analyzer_mission(mission, tmp_path)
    assert "FILE_SUMMARY" in plan.prompt
    assert "counts_mismatch" in plan.prompt  # the reason came from the file
    assert "원자 집계: 의미(변경 2)" in plan.prompt  # the measured atoms ride along
    assert plan.expected == (tmp_path / "20-analysis" / f"{_fid}.md",)


def test_fix_prompt_accepts_f_prefixed_file_target(tmp_path) -> None:
    """doc-verifier 가 f- 접두 file_id 를 내놓아도 파일 타깃으로 받는다."""

    _fid, _units = _two_unit_workspace(tmp_path)
    (tmp_path / "40-verifier.md").write_text(
        render_verifier(
            Verifier(
                mr_iid=17,
                fixes=(
                    Fix("r-02", f"f-{_fid}", "FILE_SUMMARY", "counts_mismatch"),
                ),
            )
        ),
        encoding="utf-8",
    )
    mission = parse_spec(
        _spec("analyzer", tmp_path / "20-analysis", scope=f"units f-{_fid}")
    )
    plan = satellites._analyzer_mission(mission, tmp_path)
    assert "FILE_SUMMARY" in plan.prompt
    assert plan.expected == (tmp_path / "20-analysis" / f"{_fid}.md",)


def test_fix_scope_pointing_at_nothing_is_refused(tmp_path) -> None:
    _two_unit_workspace(tmp_path)
    mission = parse_spec(
        _spec("analyzer", tmp_path / "20-analysis", scope="units u-nosuch")
    )
    with pytest.raises(ValueError):
        satellites._analyzer_mission(mission, tmp_path)


def test_executor_missing_artifact_returns_false(tmp_path, monkeypatch) -> None:
    """Exit 0 is not success — the declared artifact has to be on disk."""

    _write_inputs(tmp_path)
    monkeypatch.setattr(
        satellites.subprocess,
        "run",
        lambda cmd, **kwargs: _proc(),  # exits 0, writes nothing
    )
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    assert executor(_spec("verifier", tmp_path / "40-verifier.md")) is False


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
    _write_inputs(tmp_path)
    _fake_run_writer(monkeypatch)
    executor = satellites.satellite_executor(_settings(monkeypatch, tmp_path), tmp_path)
    spec = _spec("analyzer", tmp_path / "20-analysis", scope="files 5..9")
    assert executor(spec) is False
