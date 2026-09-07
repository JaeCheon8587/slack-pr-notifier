"""revise_workflow — the staged revise loop's deterministic core.

The revise loop is today one headless CLI call that is told to edit the docs
and report `{applied, unapplied, summary}` (app/ai_runner.py). Nothing checks
that the thing a human pointed at exists, that its companions were updated,
or that "I applied it" is true. docs/revise-workflow.html splits that into a
fixed 9-node DAG where judgment goes to three separate LLM calls and every
search, existence check and comparison is a deterministic Python node.

This package holds the deterministic half:

- `workspace` — artifact paths under `<workspace_root>/.revise/...` plus
  file-existence state derivation (resume from the node that died).
- `schema`    — the seven artifacts' dataclasses, parsers and renderers. The
  renderers also produce the empty templates the prompts carry, so a prompt
  and its parser physically cannot drift.
- `nodes`     — the four tool nodes: anchor / impact / gate / summary-extract.

Everything here is a pure function: no subprocess, no git, no network, no
Settings, no module state. The gate never calls git — it takes
`changed_paths` as an argument. `runner.py` and `prompts.py` (the LLM half)
sit on top and are the only place a process gets spawned.
"""
