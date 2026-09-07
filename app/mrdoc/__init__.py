"""mrdoc — deterministic document-MR review pipeline (docs/mrdoc-pipeline.html).

Phase 1 (this package's first cut) implements the deterministic tool chain:
changeset / structure / literals, the artifact-derived dispatch loop, and the
Python orchestrator that replaces the design's sonnet Agent-tool loop (see
docs/mrdoc-migration-plan.md §3-A1 — the loop is pure mechanics: exit 0 → run
wave, exit 4 → done, exit 2 → abort, no judgment anywhere).

Phase 2 adds the rest of the DAG: excerpt/levelcheck/collect/render run as
deterministic in-process tool nodes, and the two LLM satellites
(doc-analyzer / doc-verifier) execute as headless codex CLI missions
(satellites.py) under the opus-orchestrator delegation contract — the
orchestrator judges, satellites only read and write.

v2 removed the pipeline's judgement layer entirely: nine nodes, no ruling of
any kind, and the only natural language is the per-unit 설명, the per-file
FILE_SUMMARY and the auditor's one-line why. The render node writes both
report.html and slack-summary.txt, so the overview a reader sees in Slack is
the same text the page's section 1 prints.
"""
