"""mrdoc — deterministic document-MR review pipeline (docs/mrdoc-pipeline.html).

Phase 1 (this package's first cut) implements the deterministic tool chain:
changeset / structure / literals, the artifact-derived dispatch loop, and the
Python orchestrator that replaces the design's sonnet Agent-tool loop (see
docs/mrdoc-migration-plan.md §3-A1 — the loop is pure mechanics: exit 0 → run
wave, exit 4 → done, exit 2 → abort, no judgment anywhere).

LLM satellite nodes (doc-analyzer / doc-verifier / doc-reporter) and the
collect / levelcheck / render tools arrive in later phases; the dispatch table
already knows their artifacts so the loop shape is final from day one.
"""

