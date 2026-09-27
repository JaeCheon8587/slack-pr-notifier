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

from .analysis import load_analyses_split
from .changeset import Changeset, parse_changeset
from .levelcheck import parse_levelcheck
from .structure import Structure, parse_structure
from .verifier import parse_verifier
from .workspace import artifact_paths, fix_rounds_used

EXIT_WAVE_RAN = 0
EXIT_COMPLETE = 4
EXIT_ABORT = 2

#: Total FIX re-calls a work directory may spend before the remaining
#: findings ride the report as verify.outstanding instead of a re-analysis.
FIX_ROUND_MAX = 2

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
    # Themes runs on the verifier's settled output — a fix round reopens
    # verifier, and themes has to wait for the prose's final word.
    "themes": ("verifier",),
    "collect": ("themes", "verifier"),
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
    "themes",
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


#: missing_prose reasons — the fix prompt prints them next to the target.
REASON_EXPLANATION_MISSING = "explanation_missing (설명)"
REASON_SUMMARY_MISSING = "summary_missing (FILE_SUMMARY)"
REASON_SUMMARY_UNKNOWN_REFS = "summary_unknown_refs (FILE_SUMMARY)"


def _missing_prose(
    paths: dict[str, Path],
    structure: Structure | None,
    changeset: Changeset | None,
) -> dict[str, str]:
    if structure is None or changeset is None:
        return {}
    analyses, _failed = load_analyses_split(paths["analysis_dir"])
    known = {unit.unit_id for unit in structure.changed}
    explained = {
        unit.unit_id
        for analysis in analyses
        for unit in analysis.units
        if unit.explanation.strip()
    }
    reasons: dict[str, str] = {}
    for unit in structure.changed:
        if unit.unit_id not in explained:
            reasons[unit.unit_id] = REASON_EXPLANATION_MISSING
    by_file = {analysis.file_id: analysis for analysis in analyses}
    for entry in changeset.files:
        analysis = by_file.get(entry.fid)
        if analysis is None or not analysis.summary.strip():
            reasons[entry.fid] = REASON_SUMMARY_MISSING
        elif any(ref not in known for ref in analysis.summary_refs):
            reasons[entry.fid] = REASON_SUMMARY_UNKNOWN_REFS
    return reasons


def missing_prose(work_dir: Path) -> dict[str, str]:
    """Prose the report would print as 생성 실패 — target id -> reason.

    The report's partial-failure rules, applied while a re-call can still
    help: a unit 05 produced that no 20-analysis block explains, and a file
    whose FILE_SUMMARY is empty or cites an id 05 never produced (collect
    drops such a summary whole). Every target is an id from the tool's own
    list, so asking for it again guesses nothing — the orphan block that left
    the gap is still dropped, never re-mapped onto a real unit. A 20-analysis
    file that does not parse lands here too: none of its units is explained,
    which is how a broken file gets rewritten whole.
    """

    paths = artifact_paths(work_dir)
    return _missing_prose(
        paths,
        _parse_or_none(paths["structure"], parse_structure),
        _parse_or_none(paths["changeset"], parse_changeset),
    )


def _addressable(
    targets: tuple[str, ...],
    structure: Structure | None,
    changeset: Changeset | None,
) -> list[str]:
    """Verifier picks that name a real unit or file — f- prefix stripped.

    A FIX aimed at an id 05 never produced (the analyzer's typo, copied into
    the audit) has no block to rewrite. Passed on, it made the fix mission
    refuse and the whole run abort over one unaddressable FIX; dropped here,
    it still counts as 지적 잔존 because collect reads the FIX blocks itself.
    """

    if structure is None or changeset is None:
        return list(targets)  # nothing to check against — pass through
    units = {unit.unit_id for unit in structure.changed}
    files = {entry.fid for entry in changeset.files}
    kept: list[str] = []
    for target in targets:
        if target in units or target in files:
            kept.append(target)
        elif target.startswith("f-") and target[2:] in files:
            # doc-verifier's template once showed file ids that way.
            kept.append(target[2:])
    return kept


def fix_targets(work_dir: Path) -> tuple[str, ...]:
    """Units and files owed rewritten prose — 세 게이트가 지목한 것의 합집합.

    doc-verifier names units whose 설명 invented or omitted something (its
    fidelity verdicts count even where the FIX copy is missing); the
    levelcheck vocab gate names units whose 설명 used a banned word; and
    missing_prose names what never arrived at all. All three are "this prose
    has to be written (again)", so all feed the one re-call rather than each
    asking for its own round — the cap stays FIX_ROUND_MAX either way.
    """

    paths = artifact_paths(work_dir)
    structure = _parse_or_none(paths["structure"], parse_structure)
    changeset = _parse_or_none(paths["changeset"], parse_changeset)
    targets: list[str] = []
    report = _parse_or_none(paths["verifier"], parse_verifier)
    if report is not None:
        targets.extend(_addressable(report.flagged_targets(), structure, changeset))
    check = _parse_or_none(paths["levelcheck"], parse_levelcheck)
    if check is not None:
        targets.extend(row.unit_id for row in check.units if row.vocab_violation)
    targets.extend(_missing_prose(paths, structure, changeset))
    return tuple(dict.fromkeys(targets))


def fix_round_due(work_dir: Path) -> tuple[str, ...]:
    """Targets for the next re-call, or () when none is left to spend."""

    paths = artifact_paths(work_dir)
    if fix_rounds_used(work_dir) >= FIX_ROUND_MAX or not paths["verifier"].is_file():
        return ()
    return fix_targets(work_dir)


def round_gate(work_dir: Path, gate: str) -> Path:
    """The verifier/levelcheck file the open re-call answers to.

    close_fix_round moves the live gates aside before the satellite runs, so
    by then the findings live in this round's archive. The live file only
    wins while it still exists — i.e. before the round has been opened.
    """

    paths = artifact_paths(work_dir)
    if paths[gate].is_file():
        return paths[gate]
    return paths.get(f"{gate}_r{fix_rounds_used(work_dir)}", paths[gate])


def fix_spec(
    work_dir: Path, *, wave: int, budget_usd: float, targets: tuple[str, ...]
) -> SpecBlock:
    """The analyzer re-call — SCOPE names targets, never a file batch.

    READ names the gates' archive for the round about to open, not the live
    files: close_fix_round removes those before the satellite starts, and a
    READ path that no longer exists sends the satellite in blind.
    """

    paths = artifact_paths(work_dir)
    round_no = fix_rounds_used(work_dir) + 1
    return SpecBlock(
        wave=wave,
        agent="analyzer",
        read=(
            paths["changeset"],
            paths["structure"],
            paths["literals"],
            paths["excerpt"],
            paths.get(f"levelcheck_r{round_no}", paths["levelcheck"]),
            paths.get(f"verifier_r{round_no}", paths["verifier"]),
        ),
        budget_usd=budget_usd,
        return_path=paths["analysis_dir"],
        scope="units " + ",".join(targets),
    )


def close_fix_round(work_dir: Path) -> None:
    """Spend a round: bump the marker, archive this round's gates, reopen 30/40.

    The marker goes down before anything else so a crash mid-round still
    costs the round — the design's one hard requirement here is that the
    loop ends, not that it always gets its retry. Archiving rather than
    deleting keeps every round readable next to the ledger; removing the live
    files is what puts levelcheck and verifier back to pending, which is how
    the DAG re-runs them over the rewritten 설명.
    """

    paths = artifact_paths(work_dir)
    round_no = fix_rounds_used(work_dir) + 1
    paths["fix_marker"].write_text(f"{round_no}\n", encoding="utf-8")
    for live, archived in (
        ("verifier", f"verifier_r{round_no}"),
        ("levelcheck", f"levelcheck_r{round_no}"),
    ):
        if paths[live].is_file():
            paths[archived].write_text(
                paths[live].read_text(encoding="utf-8"), encoding="utf-8"
            )
            paths[live].unlink()


def snapshot_analysis(work_dir: Path) -> dict[str, bytes]:
    """Every 20-analysis file as bytes — what revert_fix_round puts back."""

    directory = artifact_paths(work_dir)["analysis_dir"]
    if not directory.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def revert_fix_round(work_dir: Path, snapshot: dict[str, bytes]) -> None:
    """Undo a failed re-call: 20-analysis as it was, this round's gates live again.

    The satellite writes straight into 20-analysis, so by the time its result
    is rejected the files on disk may already be the broken rewrite. Putting
    the snapshot back leaves the report exactly as good as it was before the
    round — 설명 생성 실패 where prose was missing, 지적 잔존 where a FIX
    went unanswered — instead of no report at all. The marker is not rolled
    back: a failure still spends the round, so the loop stays finite.
    Reinstating the archived gates rather than re-running them is sound
    because the prose they audited is byte-identical again.
    """

    paths = artifact_paths(work_dir)
    directory = paths["analysis_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.is_file() and path.name not in snapshot:
            path.unlink()
    for name, data in snapshot.items():
        (directory / name).write_bytes(data)
    round_no = fix_rounds_used(work_dir)
    for live in ("verifier", "levelcheck"):
        archived = paths.get(f"{live}_r{round_no}")
        if archived is not None and archived.is_file():
            paths[live].write_bytes(archived.read_bytes())


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
        elif node == "themes":
            if fix_round_due(work_dir):
                # A re-call is still owed: grouping now would describe prose
                # the round is about to replace. Only reachable after a failed
                # round was reverted (or a crash between verifier and round).
                continue
            specs.append(
                SpecBlock(
                    wave=wave,
                    agent="themes",
                    read=(
                        paths["analysis_dir"],
                        paths["structure"],
                        paths["literals"],
                    ),
                    budget_usd=budget_usd,
                    return_path=paths["themes"],
                )
            )
    return specs
