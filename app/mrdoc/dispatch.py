"""Wave dispatcher — artifact existence is the only source of truth.

The design's exit contract: 0 after a wave ran, 4 when everything is done,
2 on abort. Nodes never ask each other for status; they look at the work
directory. The analyzer fans out over md files in batches (fanout, default
5) so one satellite failure costs one batch, and the whole first-order DAG
is declared here once. The tool chain (changeset -> structure -> literals)
completes first inside every wave, so analyzer specs — keyed off the
changeset alone — always execute with structure/literals materialized.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workspace import artifact_paths

EXIT_WAVE_RAN = 0
EXIT_COMPLETE = 4
EXIT_ABORT = 2

DEPS: dict[str, tuple[str, ...]] = {
    "changeset": (),
    "structure": ("changeset",),
    "literals": ("structure",),
    # The analyzer reads structure/literals, but the orchestrator finishes
    # the tool chain before any spec executes in the same wave, so the spec
    # edge is the changeset: fanout is plannable once files are known.
    "analysis": ("changeset",),
    "levelcheck": ("analysis",),
    "verifier": ("levelcheck",),
    "collect": ("verifier",),
    "reporter": ("collect",),
    "render": ("reporter",),
}

_NODES: tuple[str, ...] = (
    "changeset",
    "structure",
    "literals",
    "analysis",
    "levelcheck",
    "verifier",
    "collect",
    "reporter",
    "render",
)


@dataclass(frozen=True)
class SpecBlock:
    """One satellite assignment — the prompt skeleton the design pins."""

    wave: int
    agent: str
    read: tuple[Path, ...]
    budget_usd: float
    return_path: Path
    scope: str = ""

    def render(self) -> str:
        lines = [
            f"WAVE {self.wave}",
            f"SPEC {self.agent}",
            f"READ {', '.join(str(p) for p in self.read)}",
            f"BUDGET {self.budget_usd}",
            f"RETURN {self.return_path}",
        ]
        if self.scope:
            lines.append(f"SCOPE {self.scope}")
        return "\n".join(lines)


def _analysis_expected(work_dir: Path) -> int:
    """How many 20-analysis artifacts the changeset demands (0 files -> 0)."""

    marker = work_dir / ".analysis-expected"
    if marker.exists():
        try:
            return int(marker.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            return 0
    return 0


def derive_state(work_dir: Path) -> dict[str, str]:
    """{node: done|pending} from artifact existence alone."""

    paths = artifact_paths(work_dir)
    expected = _analysis_expected(work_dir)
    analysis_done = (work_dir / ".analysis-expected").exists() and (
        expected == 0
        or (
            paths["analysis_dir"].is_dir()
            and len([p for p in paths["analysis_dir"].iterdir() if p.is_file()])
            >= expected
        )
    )
    state: dict[str, str] = {}
    for node in _NODES:
        if node == "analysis":
            state[node] = "done" if analysis_done else "pending"
        else:
            state[node] = "done" if paths[node].exists() else "pending"
    return state


def runnable_nodes(state: dict[str, str]) -> list[str]:
    """Pending nodes whose deps are all done, in DAG order."""

    return [
        node
        for node in _NODES
        if state.get(node) == "pending"
        and all(state.get(dep) == "done" for dep in DEPS[node])
    ]


def next_specs(
    work_dir: Path, *, wave: int, fanout: int, budget_usd: float
) -> list[SpecBlock]:
    """Spec blocks for every runnable node — agents and tools alike.

    Tool specs (structure/literals) mirror the design's SPEC tool block:
    contract text the orchestrator satisfies in-process before any satellite
    runs. The changeset is exempt — it materializes from the caller's
    PipelineInputs, not from the work directory. levelcheck/collect/render
    are deterministic tool nodes too — they never get a spec, the
    orchestrator runs them in-process at the end of the wave their
    dependencies completed in.
    """

    paths = artifact_paths(work_dir)
    state = derive_state(work_dir)
    runnable = runnable_nodes(state)
    specs: list[SpecBlock] = []
    for node in runnable:
        if node == "changeset":
            continue
        if node in ("structure", "literals"):
            read = (
                (paths["changeset"],)
                if node == "structure"
                else (paths["changeset"], paths["structure"])
            )
            specs.append(
                SpecBlock(
                    wave=wave,
                    agent=node,
                    read=read,
                    budget_usd=budget_usd,
                    return_path=paths[node],
                    scope=f"RUN: mrdoc {node} --work {work_dir.resolve()}",
                )
            )
        elif node == "analysis":
            expected = _analysis_expected(work_dir)
            if expected == 0:
                continue  # nothing to fan out over
            for start in range(0, max(expected, 1), max(fanout, 1)):
                specs.append(
                    SpecBlock(
                        wave=wave,
                        agent="analyzer",
                        read=(paths["changeset"], paths["structure"], paths["literals"]),
                        budget_usd=budget_usd,
                        return_path=paths["analysis_dir"],
                        scope=f"files {start}..{min(start + fanout, expected) - 1}",
                    )
                )
        elif node == "verifier":
            specs.append(
                SpecBlock(
                    wave=wave,
                    agent="verifier",
                    read=(
                        paths["levelcheck"],
                        paths["changeset"],
                        paths["structure"],
                        paths["literals"],
                        paths["analysis_dir"],
                    ),
                    budget_usd=budget_usd,
                    return_path=paths[node],
                )
            )
        elif node == "reporter":
            specs.append(
                SpecBlock(
                    wave=wave,
                    agent="reporter",
                    read=(paths["collect"], paths["levelcheck"], paths["analysis_dir"]),
                    budget_usd=budget_usd,
                    return_path=paths[node],
                )
            )
    return specs
