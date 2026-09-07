"""Tests for app/revise_workflow/workspace.py — paths, state, ledger.

Nothing is faked: the module is pure path arithmetic plus one append, so
every case runs against a real `tmp_path`. What is asserted is the three
things a round depends on — the work directory sits outside the MR's git
clone (`git add -A` would otherwise commit artifacts onto the branch), state
comes from artifact existence alone so a dead round resumes mid-DAG, and the
ledger only ever grows and carries no timestamps.
"""

from __future__ import annotations

import pytest

from app.revise_workflow.workspace import (
    ARTIFACT_FILES,
    DEPS,
    NODES,
    append_ledger,
    artifact_paths,
    derive_state,
    runnable_nodes,
    work_dir,
)


def _dir(tmp_path):
    return work_dir(tmp_path, 1009, 11, 1)


def test_work_dir_is_round_scoped_and_outside_the_clone(tmp_path) -> None:
    first = _dir(tmp_path)
    assert first == tmp_path / ".revise" / "1009" / "11" / "r1"
    assert first != work_dir(tmp_path, 1009, 11, 2)
    clone = tmp_path / "1009" / "11"
    assert clone not in first.parents
    assert ".revise" in first.parts


def test_artifact_names_are_the_design_file_names(tmp_path) -> None:
    paths = artifact_paths(_dir(tmp_path))
    assert {key: path.name for key, path in paths.items()} == {
        "intent": "00-intent.md",
        "anchor": "10-anchor.md",
        "impact": "20-impact.md",
        "edit": "30-edit.md",
        "gate": "35-gate.md",
        "verify": "40-verify.md",
        "summary": "50-summary.md",
        "ledger": "ledger.md",
    }


def test_nodes_deps_and_artifacts_agree() -> None:
    assert set(NODES) == set(DEPS)
    assert set(NODES) <= set(ARTIFACT_FILES)
    assert "ledger" not in NODES  # history, not state


def test_state_is_derived_from_artifact_existence_only(tmp_path) -> None:
    directory = _dir(tmp_path)
    directory.mkdir(parents=True)
    assert derive_state(directory) == dict.fromkeys(NODES, "pending")
    assert runnable_nodes(derive_state(directory)) == ["intent"]

    artifact_paths(directory)["intent"].write_text("x", encoding="utf-8")
    state = derive_state(directory)
    assert state["intent"] == "done"
    assert runnable_nodes(state) == ["anchor"]


def test_resume_skips_completed_nodes(tmp_path) -> None:
    directory = _dir(tmp_path)
    directory.mkdir(parents=True)
    paths = artifact_paths(directory)
    for key in ("intent", "anchor", "impact", "edit"):
        paths[key].write_text("x", encoding="utf-8")
    assert runnable_nodes(derive_state(directory)) == ["gate"]


def test_runnability_is_direct_deps_only(tmp_path) -> None:
    """Same rule as app/mrdoc/dispatch.py: only a node's own deps are checked.

    A hole in the chain therefore makes both the hole and the node after it
    runnable — the loop walks NODES in order and closes the hole first.
    """

    directory = _dir(tmp_path)
    directory.mkdir(parents=True)
    paths = artifact_paths(directory)
    paths["intent"].write_text("x", encoding="utf-8")
    paths["impact"].write_text("x", encoding="utf-8")  # anchor never ran
    runnable = runnable_nodes(derive_state(directory))
    assert runnable == ["anchor", "edit"]
    assert runnable[0] == "anchor"


def test_a_node_without_a_dependency_tuple_is_a_keyerror() -> None:
    """The failure the import-time guard exists to prevent, shown directly."""

    with pytest.raises(KeyError):
        DEPS["nonsense"]


def test_unknown_node_names_in_state_are_ignored() -> None:
    state = dict.fromkeys(NODES, "pending")
    state["nonsense"] = "done"
    assert runnable_nodes(state) == ["intent"]


def test_ledger_appends_and_carries_no_timestamp(tmp_path) -> None:
    directory = _dir(tmp_path)
    append_ledger(directory, ["11  r1  tool  anchor    -> resolved=1 missing=0"])
    append_ledger(directory, ["11  r1  tool  impact    -> candidates=2"])
    text = artifact_paths(directory)["ledger"].read_text(encoding="utf-8")
    assert text == (
        "11  r1  tool  anchor    -> resolved=1 missing=0\n"
        "11  r1  tool  impact    -> candidates=2\n"
    )
