"""mrdoc rail -- MR-open event wired to the doc-review pipeline.

app/ingest.handle_mr_open's non-blocking follow-up: once the normal Slack
notification is posted, an MR whose changes are md-dominant
(mrdoc_doc_ratio_threshold, default 0.8) gets the full pipeline run against
it in a daemon thread: the deterministic tool chain in-process plus the
three LLM satellites (analyzer/verifier/reporter) as headless claude calls.
Trees are fetched through the GitLab API -- this host has no clone of the
company GitLab -- materialized to base/ and snapshot/ so satellites can
Read the sources, artifacts land under .mrdoc-ws/.work/<iid>-<sha8>, and a
summary plus report.html are replied into the original notification's
thread.

Everything here is fire-and-forget: the thread must never block the event
handler (tree fetches alone can take minutes), and every failure is logged
inside the thread, never propagated to the MR rail.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings, secret_value
from app.gitlab_client import GitLabClient
from app.slack_client import SlackClient

from . import satellites
from .frontmatter import parse_frontmatter
from .orchestrator import PipelineInputs, run_to_completion
from .workspace import artifact_paths, work_dir

logger = logging.getLogger("uvicorn.error")

# Per-MR artifact root -- distinct from the smoke driver's .smoke-ws and the
# revise executor's workspace_root so a pipeline bug can never touch either.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / ".mrdoc-ws"


def md_ratio(files: list[dict[str, Any]] | None) -> float | None:
    """Share of changed files that are .md/.mdx -- None when there are none."""

    if not files:
        return None
    md = sum(
        1
        for entry in files
        if str(entry.get("filename") or "").lower().endswith((".md", ".mdx"))
    )
    return md / len(files)


def passes_gate(settings: Settings, files: list[dict[str, Any]] | None) -> bool:
    """True when mrdoc is enabled and the MR is md-dominant."""

    if not settings.mrdoc_enabled:
        return False
    ratio = md_ratio(files)
    return ratio is not None and ratio >= settings.mrdoc_doc_ratio_threshold


def start_mrdoc_review(
    settings: Settings,
    mr: dict[str, Any],
    posted: dict[str, Any] | None,
    *,
    context: dict[str, Any] | None = None,
) -> threading.Thread | None:
    """Launch the pipeline thread for an opened MR -- None when gated out.

    When ``context`` (ingest's fetch_mr_context result) is available the md
    gate runs synchronously here so a code-only MR never pays for a thread;
    without it the gate re-runs inside the thread once files are fetched.
    """

    if not settings.mrdoc_enabled:
        return None
    if not settings.mrdoc_satellite_enabled:
        logger.info(
            "mrdoc rail: !%s skipped -- mrdoc_satellite_enabled is false",
            mr.get("iid"),
        )
        return None
    if context is not None and not passes_gate(settings, context.get("files")):
        logger.info(
            "mrdoc rail: !%s skipped -- md ratio below threshold", mr.get("iid")
        )
        return None
    thread = threading.Thread(
        target=_run_thread,
        args=(settings, mr, posted, context),
        name="mrdoc-" + str(mr.get("iid")),
        daemon=True,
    )
    thread.start()
    logger.info("mrdoc rail: thread started for !%s", mr.get("iid"))
    return thread


def _run_thread(
    settings: Settings,
    mr: dict[str, Any],
    posted: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> None:
    try:
        asyncio.run(_run_review(settings, mr, posted, context))
    except Exception:  # noqa: BLE001 -- the rail must never crash the app
        logger.exception("mrdoc rail: pipeline crashed for !%s", mr.get("iid"))


async def _run_review(
    settings: Settings,
    mr: dict[str, Any],
    posted: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> None:
    mr_iid = mr.get("iid")
    token = secret_value(settings.gitlab_token)
    if not token:
        logger.warning("mrdoc rail: gitlab_token not configured -- !%s skipped", mr_iid)
        return
    client = GitLabClient(
        settings.gitlab_url, token, verify_ssl=settings.gitlab_verify_ssl
    )
    project_id = mr.get("project_id")

    detail = await client.get_merge_request(project_id, mr_iid)
    refs = detail.get("diff_refs") or {}
    base_sha = refs.get("base_sha") or ""
    head_sha = refs.get("head_sha") or ""
    start_sha = refs.get("start_sha") or base_sha
    if not (base_sha and head_sha):
        logger.warning("mrdoc rail: !%s has no diff_refs -- skipped", mr_iid)
        return
    if str(detail.get("changes_count") or "0") == "0":
        logger.info("mrdoc rail: !%s has no changes -- skipped", mr_iid)
        return

    files = context.get("files") if isinstance(context, dict) else None
    if not isinstance(files, list):
        context = await client.fetch_mr_context(
            project_id, mr_iid, head_sha, max_files=settings.mrdoc_max_files
        )
        files = context["files"]
    if not passes_gate(settings, files):
        logger.info("mrdoc rail: !%s md ratio below threshold -- skipped", mr_iid)
        return

    api_root = settings.gitlab_url.rstrip("/")
    api_url = api_root if api_root.endswith("/api/v4") else api_root + "/api/v4"
    headers = {"PRIVATE-TOKEN": token}
    verify = settings.gitlab_verify_ssl
    base_tree = await _fetch_md_tree(api_url, headers, verify, project_id, base_sha)
    head_tree = await _fetch_md_tree(api_url, headers, verify, project_id, head_sha)
    logger.info(
        "mrdoc rail: md trees fetched for !%s -- base=%d head=%d",
        mr_iid,
        len(base_tree),
        len(head_tree),
    )

    directory = work_dir(_WORKSPACE_ROOT, mr_iid, head_sha)
    _materialize_trees(directory, base_tree, head_tree)
    inputs = PipelineInputs(
        mr_iid=mr_iid,
        project_id=project_id,
        base_sha=base_sha,
        head_sha=head_sha,
        start_sha=start_sha,
        raw_files=files,
        base_tree=base_tree,
        head_tree=head_tree,
        diff_url=str(detail.get("web_url") or ""),
        skipped=(
            bool(context.get("files_truncated"))
            if isinstance(context, dict)
            else False
        ),
    )
    code = run_to_completion(
        inputs,
        directory,
        satellites.satellite_executor(settings, directory),
        fanout=settings.mrdoc_fanout,
        budget_usd=settings.mrdoc_satellite_budget_usd,
    )
    logger.info("mrdoc rail: pipeline exit=%s for !%s dir=%s", code, mr_iid, directory)
    await _post_summary(settings, posted, mr_iid, directory, code)


_SUMMARY_KEYS = {
    "changeset": ("counts", ("files", "md", "non_md", "skipped")),
    "literals": ("totals", ("removed", "added", "changed")),
}


def _summarize(directory: Path, exit_code: int, mr_iid: Any) -> str:
    """One Slack-sized summary block from the artifacts' frontmatter."""

    lines = [
        "*mrdoc 문서 리뷰 -- MR !" + str(mr_iid) + "* (exit=" + str(exit_code) + ")"
    ]
    paths = artifact_paths(directory)
    for name, (key, wanted) in _SUMMARY_KEYS.items():
        artifact = paths[name]
        if not artifact.exists():
            continue
        try:
            meta = parse_frontmatter(artifact.read_text(encoding="utf-8"))
        except ValueError:
            continue
        block = meta.get(key)
        if isinstance(block, dict) and block:
            rendered = ", ".join(
                field + "=" + str(block[field])
                for field in wanted
                if field in block
            )
            lines.append(key + ": " + rendered)
    lines.append("산출물: " + str(directory))
    return "\n".join(lines)


def _materialize_trees(
    directory: Path, base_tree: dict[str, str], head_tree: dict[str, str]
) -> None:
    """Write base/ and snapshot/ so the satellites can Read source files.

    Idempotent per push directory — the fetch already paid for the blobs,
    and a satellite re-run (same head sha) must not rewrite them mid-read.
    """

    for name, tree in (("base", base_tree), ("snapshot", head_tree)):
        root = directory / name
        if root.exists():
            continue
        for rel, text in tree.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


async def _post_summary(
    settings: Settings,
    posted: dict[str, Any] | None,
    mr_iid: Any,
    directory: Path,
    exit_code: int,
) -> None:
    text = _summarize(directory, exit_code, mr_iid)
    channel = posted.get("channel") if isinstance(posted, dict) else None
    thread_ts = posted.get("ts") if isinstance(posted, dict) else None
    if not (settings.slack_bot_token and channel and thread_ts):
        logger.info(
            "mrdoc rail: no Slack target for !%s -- summary follows\n%s", mr_iid, text
        )
        return
    try:
        client = SlackClient(secret_value(settings.slack_bot_token))
        await client.call(
            "chat.postMessage",
            {"channel": channel, "thread_ts": thread_ts, "text": text},
        )
    except Exception:
        logger.exception("mrdoc rail: summary post failed for !%s", mr_iid)
        return
    html = _uploadable_report(directory)
    if html is None:
        return
    try:
        await client.upload_report_file(
            str(channel),
            str(thread_ts),
            "mrdoc-report.html",
            html,
            initial_comment="📄 mrdoc 리포트 (report.html)",
        )
    except Exception:
        logger.exception("mrdoc rail: report upload failed for !%s", mr_iid)


def _uploadable_report(directory: Path) -> str | None:
    """report.html content when worth uploading -- None when missing or stub."""

    report = artifact_paths(directory)["render"]
    if not report.exists():
        return None
    html = report.read_text(encoding="utf-8")
    if "RAIL-STUB placeholder" in html:
        logger.info("mrdoc rail: report is stub output -- upload skipped")
        return None
    return html


async def _fetch_md_tree(
    api_url: str, headers: dict[str, str], verify: bool, project_id: Any, ref: str
) -> dict[str, str]:
    """Every .md/.mdx blob at ``ref`` as {posix_path: text} -- GitLab API walk.

    The MR lives on the company GitLab while this host has no clone of it,
    so the tree comes from repository/tree (paginated) plus one raw-file
    request per md blob. A 404 between listing and fetch is skipped -- it
    can only be a race with a force push, and the wave loop aborts loudly
    on a structurally broken tree anyway.
    """

    project = quote(str(project_id), safe="")
    paths: list[str] = []
    contents: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30, verify=verify) as client:
        page = 1
        while True:
            response = await client.get(
                api_url + "/projects/" + project + "/repository/tree",
                headers=headers,
                params={"ref": ref, "recursive": "true", "per_page": 100, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            paths.extend(
                item["path"]
                for item in batch
                if item.get("type") == "blob"
                and str(item.get("path", "")).lower().endswith((".md", ".mdx"))
            )
            if len(batch) < 100:
                break
            page += 1
        for path in paths:
            response = await client.get(
                api_url
                + "/projects/"
                + project
                + "/repository/files/"
                + quote(path, safe="")
                + "/raw",
                headers=headers,
                params={"ref": ref},
            )
            if response.status_code == 404:
                continue
            response.raise_for_status()
            contents[path] = response.text
    return contents
