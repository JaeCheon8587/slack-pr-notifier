"""Per-push immutable work directories — '.work/<mr_iid>-<head_sha8>'.

Every push gets its own directory keyed by the head sha8: artifacts are
never reused across pushes, which is what makes resume safe (a stale
artifact can only be stale within one push) and lets two pushes run
concurrently without clobbering each other. Inside sit 'base/' and
'snapshot/' — full tree checkouts of the diff base and head — plus the
numbered artifacts the nodes write.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from .ids import sha8

Checkout = Callable[[str, Path], None]


def work_dir(workspace_root: Path, mr_iid: int | str, head_sha: str) -> Path:
    """'.work/<mr_iid>-<head_sha8>' — one directory per push."""

    return workspace_root / ".work" / f"{mr_iid}-{sha8(head_sha)}"


def git_archive_checkout(repo: Path) -> Checkout:
    """Default checkout: 'git archive <sha>' extracted into the destination."""

    def checkout(revision: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "archive", revision],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        extract = subprocess.run(
            ["tar", "-x", "-C", str(dest)],
            input=archive.stdout,
            capture_output=True,
            check=True,
        )
        if extract.stderr:
            raise RuntimeError(extract.stderr.decode("utf-8", "replace"))

    return checkout


def ensure_trees(
    directory: Path, base_sha: str, head_sha: str, checkout: Checkout
) -> tuple[Path, Path]:
    """Materialize base/ and snapshot/ — idempotent per push directory."""

    base = directory / "base"
    snapshot = directory / "snapshot"
    if not base.exists():
        checkout(base_sha, base)
    if not snapshot.exists():
        checkout(head_sha, snapshot)
    return base, snapshot


def read_md_tree(root: Path) -> dict[str, str]:
    """Every .md/.mdx under root as {posix_relpath: text} — tree input."""

    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".md", ".mdx"):
            continue
        tree[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return tree


def artifact_paths(directory: Path) -> dict[str, Path]:
    """Canonical artifact locations — single source for dispatch/status."""

    return {
        "changeset": directory / "00-changeset.md",
        "structure": directory / "05-structure.md",
        "literals": directory / "06-literals.md",
        "excerpt": directory / "07-excerpts.md",
        "levelcheck": directory / "30-levelcheck.md",
        "verifier": directory / "40-verifier.md",
        # Themes satellite — one session per MR, after the verifier's final
        # word, so grouping sees the prose the report will actually print.
        "themes": directory / "45-themes.md",
        "collect": directory / "50-collect.md",
        "render": directory / "report.html",
        # Written by the render node beside report.html — section 1 verbatim,
        # which is all Slack ever gets to see of a document.
        "slack_summary": directory / "slack-summary.txt",
        "analysis_dir": directory / "20-analysis",
        "ledger": directory / "ledger.md",
        # Its content counts the FIX re-calls already spent (1, 2, ...).
        # Reaching the dispatcher's cap is what makes the retry loop finite
        # without asking anybody's opinion.
        "fix_marker": directory / ".fix-round",
        "verifier_r1": directory / "40-verifier.r1.md",
        "verifier_r2": directory / "40-verifier.r2.md",
        "levelcheck_r1": directory / "30-levelcheck.r1.md",
        "levelcheck_r2": directory / "30-levelcheck.r2.md",
    }


def fix_rounds_used(directory: Path) -> int:
    """How many FIX re-calls this work directory has already spent.

    A missing marker means zero; a corrupt marker still counts as one
    spent round — the loop stays finite even when the counter is unreadable.
    """

    marker = artifact_paths(directory)["fix_marker"]
    if not marker.is_file():
        return 0
    try:
        return max(int(marker.read_text(encoding="utf-8").strip()), 1)
    except ValueError:
        return 1
