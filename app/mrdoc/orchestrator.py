"""Pipeline orchestrator — one wave per call, injectable executors.

The design's loop is: look at artifacts, run what is runnable, record it,
exit. Tool nodes (changeset/structure/literals) run the deterministic
toolchain in-process; agent nodes go to an injected executor that gets the
SPEC block text and writes the artifact itself — production wires a
satellite CLI there, tests wire a fake, and the loop logic under test is
identical either way. The ledger is append-only per wave; no timestamps,
so artifacts diff cleanly between runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import dispatch
from .analysis import load_analyses_split
from .changeset import build_changeset, parse_changeset, render_changeset
from .collect import build_collect, parse_collect, render_collect
from .levelcheck import build_levelcheck, parse_levelcheck, render_levelcheck
from .literals import build_literals, parse_literals, render_literals
from .report_render import render_report_html
from .structure import build_structure, parse_structure, render_structure
from .workspace import artifact_paths

AgentExecutor = Callable[[str], bool]


@dataclass(frozen=True)
class PipelineInputs:
    """Everything wave 1-3 needs — raw diffs plus both md trees."""

    mr_iid: int
    project_id: int | str
    base_sha: str
    head_sha: str
    start_sha: str
    raw_files: list[dict[str, Any]]
    base_tree: dict[str, str]
    head_tree: dict[str, str]
    diff_url: str = ""
    skipped: bool = False


def _append_ledger(work_dir: Path, lines: list[str]) -> None:
    ledger = artifact_paths(work_dir)["ledger"]
    with ledger.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _run_tool_node(
    node: str, inputs: PipelineInputs, work_dir: Path, paths: dict[str, Path]
) -> None:
    """Execute one deterministic node — assumes its deps are all done."""

    if node == "changeset":
        changeset = build_changeset(
            mr_iid=inputs.mr_iid,
            project_id=inputs.project_id,
            base_sha=inputs.base_sha,
            head_sha=inputs.head_sha,
            start_sha=inputs.start_sha,
            raw_files=inputs.raw_files,
            skipped=inputs.skipped,
        )
        paths["changeset"].write_text(render_changeset(changeset), encoding="utf-8")
        (work_dir / ".analysis-expected").write_text(
            str(len(changeset.files)), encoding="utf-8"
        )
    elif node == "structure":
        changeset = parse_changeset(paths["changeset"].read_text(encoding="utf-8"))
        structure = build_structure(inputs.base_tree, inputs.head_tree, changeset)
        paths["structure"].write_text(render_structure(structure), encoding="utf-8")
    elif node == "literals":
        changeset = parse_changeset(paths["changeset"].read_text(encoding="utf-8"))
        structure = parse_structure(paths["structure"].read_text(encoding="utf-8"))
        literals = build_literals(
            structure, changeset, inputs.base_tree, inputs.head_tree
        )
        paths["literals"].write_text(render_literals(literals), encoding="utf-8")
    elif node == "levelcheck":
        analyses, _failed = load_analyses_split(paths["analysis_dir"])
        literals = parse_literals(paths["literals"].read_text(encoding="utf-8"))
        levelcheck = build_levelcheck(inputs.mr_iid, literals, analyses)
        paths["levelcheck"].write_text(render_levelcheck(levelcheck), encoding="utf-8")
    elif node == "collect":
        analyses, failed = load_analyses_split(paths["analysis_dir"])
        literals = parse_literals(paths["literals"].read_text(encoding="utf-8"))
        levelcheck = parse_levelcheck(paths["levelcheck"].read_text(encoding="utf-8"))
        structure = parse_structure(paths["structure"].read_text(encoding="utf-8"))
        changeset = parse_changeset(paths["changeset"].read_text(encoding="utf-8"))
        collect = build_collect(
            mr_iid=inputs.mr_iid,
            analyses=analyses,
            failed_files=failed,
            levelcheck=levelcheck,
            literals=literals,
            structure=structure,
            changeset=changeset,
            verifier_text=paths["verifier"].read_text(encoding="utf-8"),
            base_tree=inputs.base_tree,
            head_tree=inputs.head_tree,
        )
        paths["collect"].write_text(render_collect(collect), encoding="utf-8")
    elif node == "render":
        collect = parse_collect(paths["collect"].read_text(encoding="utf-8"))
        page = render_report_html(
            collect,
            paths["reporter"].read_text(encoding="utf-8"),
            mr_iid=inputs.mr_iid,
            diff_url=inputs.diff_url,
        )
        paths["render"].write_text(page, encoding="utf-8")
    else:  # pragma: no cover — _TOOL_NODES is closed
        raise ValueError(f"unknown tool node: {node}")


_TOOL_NODES = ("changeset", "structure", "literals", "levelcheck", "collect", "render")


def _run_tool_nodes(inputs: PipelineInputs, work_dir: Path) -> list[str]:
    """Execute pending deterministic nodes; return ledger lines.

    Runs at both ends of every wave: the changeset chain first (so agents
    always execute with structure/literals materialized), then — after the
    satellite specs for the wave ran — the levelcheck/collect/render nodes
    whose dependencies the satellites just produced. Nodes only run when
    dispatch's DAG says their deps are done, so a lying agent that claims
    success without writing anything leaves the wave with no progress —
    the loop's no-progress abort, not a crash on a missing artifact.
    """

    paths = artifact_paths(work_dir)
    done: list[str] = []
    while True:
        state = dispatch.derive_state(work_dir)
        runnable = [
            node for node in dispatch.runnable_nodes(state) if node in _TOOL_NODES
        ]
        if not runnable:
            return done
        node = runnable[0]
        _run_tool_node(node, inputs, work_dir, paths)
        done.append(node + " done")


def run_wave(
    inputs: PipelineInputs,
    work_dir: Path,
    agent_executor: AgentExecutor,
    *,
    wave: int,
    fanout: int,
    budget_usd: float,
) -> int:
    """Run one wave. Returns EXIT_WAVE_RAN / EXIT_COMPLETE / EXIT_ABORT."""

    work_dir.mkdir(parents=True, exist_ok=True)
    before = dispatch.derive_state(work_dir)
    ledger: list[str] = [f"wave {wave} start"]

    try:
        ledger.extend(_run_tool_nodes(inputs, work_dir))
        specs = dispatch.next_specs(
            work_dir, wave=wave, fanout=fanout, budget_usd=budget_usd
        )
        for spec in specs:
            if not agent_executor(spec.render()):
                raise RuntimeError(f"agent failed: {spec.agent}")
            ledger.append(f"{spec.agent} done ({spec.return_path.name})")
        ledger.extend(_run_tool_nodes(inputs, work_dir))
    except Exception as error:  # noqa: BLE001 — abort contract covers all
        ledger.append(f"abort: {error}")
        _append_ledger(work_dir, ledger)
        return dispatch.EXIT_ABORT

    after = dispatch.derive_state(work_dir)
    if all(value == "done" for value in after.values()):
        ledger.append("pipeline complete")
        _append_ledger(work_dir, ledger)
        return dispatch.EXIT_COMPLETE
    if after == before:
        ledger.append("abort: no progress")
        _append_ledger(work_dir, ledger)
        return dispatch.EXIT_ABORT
    _append_ledger(work_dir, ledger)
    return dispatch.EXIT_WAVE_RAN


def run_to_completion(
    inputs: PipelineInputs,
    work_dir: Path,
    agent_executor: AgentExecutor,
    *,
    fanout: int,
    budget_usd: float,
    max_waves: int = 20,
) -> int:
    """Loop waves until complete/abort — the CLI entry's convenience."""

    for wave in range(1, max_waves + 1):
        code = run_wave(
            inputs,
            work_dir,
            agent_executor,
            wave=wave,
            fanout=fanout,
            budget_usd=budget_usd,
        )
        if code != dispatch.EXIT_WAVE_RAN:
            return code
    return dispatch.EXIT_ABORT
