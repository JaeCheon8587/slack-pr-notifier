"""mrdoc — deterministic document-MR review pipeline (docs/mrdoc-pipeline.html).

Phase 1 (this package's first cut) implements the deterministic tool chain:
changeset / structure / literals, the artifact-derived dispatch loop, and the
Python orchestrator that replaces the design's sonnet Agent-tool loop (see
docs/mrdoc-migration-plan.md §3-A1 — the loop is pure mechanics: exit 0 → run
wave, exit 4 → done, exit 2 → abort, no judgment anywhere).

Phase 2 adds the rest of the DAG: levelcheck/collect/render run as
deterministic in-process tool nodes, and the three LLM satellites
(doc-analyzer / doc-verifier / doc-reporter) execute as headless claude CLI
missions (satellites.py) under the opus-orchestrator delegation contract —
the orchestrator judges, satellites only read and write.
"""
