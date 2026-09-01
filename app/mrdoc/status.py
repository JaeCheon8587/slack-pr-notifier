"""Human-readable pipeline status — one line per node for ledger/console."""

from __future__ import annotations

from pathlib import Path

from .dispatch import DEPS, derive_state


def format_status(work_dir: Path) -> str:
    """'node: state (waiting on ...)' per DAG row."""

    state = derive_state(work_dir)
    lines = []
    for node in DEPS:
        missing = [dep for dep in DEPS[node] if state.get(dep) != "done"]
        suffix = f" (waiting on {', '.join(missing)})" if missing else ""
        lines.append(f"{node}: {state.get(node, 'pending')}{suffix}")
    return "\n".join(lines)
