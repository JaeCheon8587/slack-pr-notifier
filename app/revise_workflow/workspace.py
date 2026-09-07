"""Artifact locations + file-existence state derivation for one revise round.

Progress is derived from artifact existence alone (app/mrdoc/dispatch.py:87
`derive_state`): a round that dies at `gate` resumes at `gate` instead of
re-running the two LLM calls before it. Nothing here asks a node for status.

The `.revise/` prefix is load-bearing, not cosmetic. The MR's git clone lives
at `<workspace_root>/<project_id>/<mr_iid>` and `git_workspace.commit_all` is
`git add -A` fixed (app/git_workspace.py:266), so an artifact written inside
the clone gets committed and pushed onto the MR branch. `.revise/` puts the
work directory next to the clone instead of inside it.

`NODES` and `DEPS` are kept in sync by an import-time check: `runnable_nodes`
indexes `DEPS[node]` and `derive_state` indexes `artifact_paths(...)[node]`,
so a name present in only one of the three would fail with a bare KeyError at
the worst possible moment (mid-round) rather than at import.
"""

from __future__ import annotations

from pathlib import Path

#: Artifact key -> file name. The numeric prefixes are the DAG order; `ledger`
#: has none because it is history, not state (see the design's "상태의 출처는
#: 산출물 존재다").
ARTIFACT_FILES: dict[str, str] = {
    "intent": "00-intent.md",
    "anchor": "10-anchor.md",
    "impact": "20-impact.md",
    "edit": "30-edit.md",
    "gate": "35-gate.md",
    "verify": "40-verify.md",
    "summary": "50-summary.md",
    "ledger": "ledger.md",
}

#: The fixed DAG. It is a chain — every node consumes the one before it.
DEPS: dict[str, tuple[str, ...]] = {
    "intent": (),
    "anchor": ("intent",),
    "impact": ("anchor",),
    "edit": ("impact",),
    "gate": ("edit",),
    "verify": ("gate",),
    "summary": ("verify",),
}

#: DAG order, kept in sync with DEPS (checked below).
NODES: tuple[str, ...] = (
    "intent",
    "anchor",
    "impact",
    "edit",
    "gate",
    "verify",
    "summary",
)

if set(NODES) != set(DEPS):
    raise ValueError("NODES and DEPS disagree — every node needs a dependency tuple")
if not set(NODES) <= set(ARTIFACT_FILES):
    raise ValueError("every node needs an artifact file — derive_state indexes it")


def work_dir(
    workspace_root: Path, project_id: int | str, mr_iid: int | str, round_number: int
) -> Path:
    """`<workspace_root>/.revise/<project_id>/<mr_iid>/r<round>` — outside the clone.

    One directory per round, so a re-run of round 2 never sees round 1's
    artifacts and resume stays scoped to the round that actually died.
    """

    return workspace_root / ".revise" / str(project_id) / str(mr_iid) / f"r{round_number}"


def artifact_paths(directory: Path) -> dict[str, Path]:
    """Canonical artifact locations — the single source for state and writers."""

    return {key: directory / name for key, name in ARTIFACT_FILES.items()}


def derive_state(directory: Path) -> dict[str, str]:
    """{node: done|pending} from artifact existence alone — no other input."""

    paths = artifact_paths(directory)
    return {node: "done" if paths[node].exists() else "pending" for node in NODES}


def runnable_nodes(state: dict[str, str]) -> list[str]:
    """Pending nodes whose dependencies are all done, in DAG order."""

    return [
        node
        for node in NODES
        if state.get(node) == "pending"
        and all(state.get(dep) == "done" for dep in DEPS[node])
    ]


def append_ledger(directory: Path, lines: list[str]) -> None:
    """Append history lines — never rewritten, no timestamps.

    Timestamps would make two identical rounds produce different ledgers,
    which is exactly the diff-noise mrdoc's ledger avoids. Re-entry and
    resume both leave their earlier attempt in place.
    """

    ledger = artifact_paths(directory)["ledger"]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
