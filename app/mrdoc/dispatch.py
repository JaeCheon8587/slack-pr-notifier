"""Wave dispatcher — artifact existence is the only source of truth.

The design's exit contract: 0 after a wave ran, 4 when everything is done,
2 on abort. Nodes never ask each other for status; they look at the work
directory. The analyzer fans out over md files in batches (fanout, default
5) so one satellite failure costs one batch, and the whole first-order DAG
is declared here once. The tool chain (changeset -> structure -> literals ->
excerpt) completes first inside every wave, so analyzer specs — keyed off the
changeset alone — always execute with every tool artifact materialized.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .levelcheck import parse_levelcheck
from .verifier import parse_verifier
from .workspace import artifact_paths

EXIT_WAVE_RAN = 0
EXIT_COMPLETE = 4
EXIT_ABORT = 2

DEPS: dict[str, tuple[str, ...]] = {
    "changeset": (),
    "structure": ("changeset",),
    "literals": ("structure",),
    "excerpt": ("literals",),
    # The analyzer reads structure/literals/excerpt, but the orchestrator finishes
    # the tool chain before any spec executes in the same wave, so the spec
    # edge is the changeset: fanout is plannable once files are known.
    "analysis": ("changeset",),
    "levelcheck": ("analysis",),
    "verifier": ("levelcheck",),
    "collect": ("verifier",),
    "render": ("collect",),
}

_NODES: tuple[str, ...] = (
    "changeset",
    "structure",
    "literals",
    "excerpt",
    "analysis",
    "levelcheck",
    "verifier",
    "collect",
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


def _parse_or_none(path: Path, parser):  # type: ignore[no-untyped-def]
    if not path.is_file():
        return None
    try:
        return parser(path.read_text(encoding="utf-8"))
    except ValueError:
        return None  # an unreadable gate flags nothing; collect still renders


def fix_targets(work_dir: Path) -> tuple[str, ...]:
    """Units owed a rewritten 설명 — 두 게이트가 지목한 것의 합집합.

    doc-verifier names units whose 설명 invented or omitted something; the
    levelcheck vocab gate names units whose 설명 used a banned word. Both are
    "this prose has to be written again", so both feed the one re-call rather
    than each asking for its own round.
    """

    paths = artifact_paths(work_dir)
    targets: list[str] = []
    report = _parse_or_none(paths["verifier"], parse_verifier)
    if report is not None:
        targets.extend(report.fix_targets())
    check = _parse_or_none(paths["levelcheck"], parse_levelcheck)
    if check is not None:
        targets.extend(row.unit_id for row in check.units if row.vocab_violation)
    return tuple(dict.fromkeys(targets))


def fix_round_due(work_dir: Path) -> tuple[str, ...]:
    """Targets for the single re-call, or () when it is not owed or spent."""

    paths = artifact_paths(work_dir)
    if paths["fix_marker"].exists() or not paths["verifier"].is_file():
        return ()
    return fix_targets(work_dir)


def fix_spec(
    work_dir: Path, *, wave: int, budget_usd: float, targets: tuple[str, ...]
) -> SpecBlock:
    """The analyzer re-call — SCOPE names units, never a file batch."""

    paths = artifact_paths(work_dir)
    return SpecBlock(
        wave=wave,
        agent="analyzer",
        read=(
            paths["changeset"],
            paths["structure"],
            paths["literals"],
            paths["excerpt"],
            paths["levelcheck"],
            paths["verifier"],
        ),
        budget_usd=budget_usd,
        return_path=paths["analysis_dir"],
        scope="units " + ",".join(targets),
    )


def close_fix_round(work_dir: Path) -> None:
    """Spend the round: mark it, archive round 1's gates, reopen 30 and 40.

    The marker goes down before anything else so a crash mid-round still
    costs the round — the design's one hard requirement here is that the
    loop ends, not that it always gets its retry. Archiving rather than
    deleting keeps round 1 readable next to the ledger; removing the live
    files is what puts levelcheck and verifier back to pending, which is how
    the DAG re-runs them over the rewritten 설명.
    """

    paths = artifact_paths(work_dir)
    paths["fix_marker"].write_text("1\n", encoding="utf-8")
    for live, archived in (
        ("verifier", "verifier_r1"),
        ("levelcheck", "levelcheck_r1"),
    ):
        if paths[live].is_file():
            paths[archived].write_text(
                paths[live].read_text(encoding="utf-8"), encoding="utf-8"
            )
            paths[live].unlink()


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
        if node in ("structure", "literals", "excerpt"):
            read = {
                "structure": (paths["changeset"],),
                "literals": (paths["changeset"], paths["structure"]),
                "excerpt": (
                    paths["changeset"],
                    paths["structure"],
                    paths["literals"],
                ),
            }[node]
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
                        read=(
                            paths["changeset"],
                            paths["structure"],
                            paths["literals"],
                            paths["excerpt"],
                        ),
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
                    # Not 30-levelcheck: the vocab gate is the tool's
                    # job, and showing what it decided here would only
                    # invite agreement.
                    read=(
                        paths["analysis_dir"],
                        paths["excerpt"],
                        paths["literals"],
                    ),
                    budget_usd=budget_usd,
                    return_path=paths[node],
                )
            )
    return specs
