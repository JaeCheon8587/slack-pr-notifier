from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx


class GitLabClient:
    """Minimal GitLab client for reading MR changes and recording decisions."""

    def __init__(self, base_url: str, token: str, *, verify_ssl: bool = True) -> None:
        root = base_url.rstrip("/")
        self._api_url = root if root.endswith("/api/v4") else f"{root}/api/v4"
        self._headers = {"PRIVATE-TOKEN": token}
        self._verify_ssl = verify_ssl

    async def list_merge_requests(
        self,
        project_id: int | str,
        *,
        state: str = "opened",
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """List a project's merge requests filtered by ``state`` (default "opened").

        Used by the outbound poller (app.gitlab_poller) to detect MR open/push/
        external-close events without an inbound webhook (P.2 폴링 수집 경로) — a
        single page (100) is far beyond any realistic number of simultaneously
        open MRs for one project, matching list_mr_commits's per-page rationale
        below.
        """

        async with httpx.AsyncClient(timeout=15, verify=self._verify_ssl) as client:
            response = await client.get(
                f"{self._api_url}/projects/{quote(str(project_id), safe='')}/merge_requests",
                headers=self._headers,
                params={"state": state, "per_page": per_page},
            )
        response.raise_for_status()
        payload = response.json()
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    async def get_merge_request(
        self,
        project_id: int | str,
        merge_request_iid: int,
    ) -> dict[str, Any]:
        """Fetch the current MR resource (head sha, state, merge_error).

        Used both for the pre-merge SHA freshness check (§② step 2 — the
        button's signed-token SHA must match the current head, not a
        possibly-stale DB value) and for the post-merge poll (§② step 4-5).
        """

        async with httpx.AsyncClient(timeout=10, verify=self._verify_ssl) as client:
            response = await client.get(
                self._mr_url(project_id, merge_request_iid, ""),
                headers=self._headers,
            )
        response.raise_for_status()
        return response.json()

    async def merge_merge_request(
        self,
        project_id: int | str,
        merge_request_iid: int,
        commit_sha: str,
    ) -> dict[str, Any]:
        """Merge the MR, binding the request to ``commit_sha``.

        GitLab rejects the merge server-side if the MR's current head no
        longer matches ``sha`` (a commit landed after the Slack approval was
        computed) — this is the merge-time half of the SHA-freshness guard;
        the client-side half is the ``get_merge_request`` pre-check in
        ``app.slack_actions``. No separate GitLab "approve" call is made —
        v4.1 removed the approve step; the merge call itself is the only
        GitLab-side action, per docs/mr-review-pipeline.html §① v4.1 note.
        """

        async with httpx.AsyncClient(timeout=10, verify=self._verify_ssl) as client:
            response = await client.put(
                self._mr_url(project_id, merge_request_iid, "merge"),
                headers=self._headers,
                json={"sha": commit_sha},
            )
        response.raise_for_status()
        return response.json()

    async def create_merge_request_note(
        self,
        project_id: int | str,
        merge_request_iid: int,
        body: str,
    ) -> dict[str, Any]:
        """Add a general note to an MR."""

        async with httpx.AsyncClient(timeout=10, verify=self._verify_ssl) as client:
            response = await client.post(
                self._mr_url(project_id, merge_request_iid, "notes"),
                headers=self._headers,
                json={"body": body},
            )
        response.raise_for_status()
        return response.json()

    async def list_mr_commits(
        self,
        project_id: int | str,
        merge_request_iid: int,
    ) -> list[dict[str, Any]]:
        """List the MR's commits (newest first, per GitLab's default ordering).

        Used by the opinion rail's guard (b) (docs/mr-review-pipeline.html
        §S4①): "사람 커밋 → GitLab commits API 1회 조회" — a single call, no
        pagination beyond the first page (100 commits is far beyond any
        realistic single-MR commit count for this guard's purpose).
        """

        async with httpx.AsyncClient(timeout=10, verify=self._verify_ssl) as client:
            response = await client.get(
                self._mr_url(project_id, merge_request_iid, "commits"),
                headers=self._headers,
                params={"per_page": 100},
            )
        response.raise_for_status()
        commits = response.json()
        return [item for item in commits if isinstance(item, dict)] if isinstance(commits, list) else []

    async def fetch_mr_context(
        self,
        project_id: int | str,
        merge_request_iid: int,
        commit_sha: str,
        *,
        max_files: int = 50,
        max_file_bytes: int = 60_000,
    ) -> dict[str, Any]:
        """Return normalized MR diffs and changed-file contents for AI review."""

        async with httpx.AsyncClient(timeout=30, verify=self._verify_ssl) as client:
            files, files_truncated = await self._list_mr_files(
                client, project_id, merge_request_iid, max_files
            )
            contents: dict[str, str] = {}
            for entry in files:
                filename = entry.get("filename")
                if entry.get("status") == "removed" or not isinstance(filename, str):
                    continue
                try:
                    text = await self._fetch_raw_content(
                        client, project_id, filename, commit_sha, max_file_bytes
                    )
                except httpx.HTTPError:
                    continue
                if text is not None:
                    contents[filename] = text
        return {
            "files": files,
            "contents": contents,
            "files_truncated": files_truncated,
        }

    async def _list_mr_files(
        self,
        client: httpx.AsyncClient,
        project_id: int | str,
        merge_request_iid: int,
        max_files: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        if max_files < 1:
            return [], False

        diffs: list[dict[str, Any]] = []
        page = 1
        detection_limit = max_files + 1
        while len(diffs) < detection_limit:
            per_page = min(100, detection_limit - len(diffs))
            response = await client.get(
                self._mr_url(project_id, merge_request_iid, "diffs"),
                headers=self._headers,
                params={"per_page": per_page, "page": page},
            )
            if page == 1 and response.status_code in {404, 500, 502, 503}:
                return await self._list_legacy_mr_files(
                    client, project_id, merge_request_iid, max_files
                )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            diffs.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < per_page:
                break
            page += 1

        files = [_normalize_diff(item) for item in diffs[:max_files]]
        return files, len(diffs) > max_files

    async def _list_legacy_mr_files(
        self,
        client: httpx.AsyncClient,
        project_id: int | str,
        merge_request_iid: int,
        max_files: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Use GitLab's legacy changes endpoint when /diffs is unavailable or broken."""

        response = await client.get(
            self._mr_url(project_id, merge_request_iid, "changes"),
            headers=self._headers,
            params={},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return [], False
        changes = payload.get("changes")
        if not isinstance(changes, list):
            return [], bool(payload.get("overflow"))
        diffs = [item for item in changes if isinstance(item, dict)]
        files = [_normalize_diff(item) for item in diffs[:max_files]]
        truncated = bool(payload.get("overflow")) or len(diffs) > max_files
        return files, truncated

    async def _fetch_raw_content(
        self,
        client: httpx.AsyncClient,
        project_id: int | str,
        path: str,
        ref: str,
        max_file_bytes: int,
    ) -> str | None:
        project = quote(str(project_id), safe="")
        file_path = quote(path, safe="")
        response = await client.get(
            f"{self._api_url}/projects/{project}/repository/files/{file_path}/raw",
            headers=self._headers,
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        raw = response.content
        if b"\x00" in raw[:1024]:
            return None
        if len(raw) > max_file_bytes:
            return raw[:max_file_bytes].decode("utf-8", errors="replace") + "\n… (내용 일부 생략)"
        return raw.decode("utf-8", errors="replace")

    def _mr_url(self, project_id: int | str, iid: int, suffix: str = "") -> str:
        project = quote(str(project_id), safe="")
        base = f"{self._api_url}/projects/{project}/merge_requests/{iid}"
        return f"{base}/{suffix}" if suffix else base


def _normalize_diff(diff: dict[str, Any]) -> dict[str, Any]:
    patch = diff.get("diff") if isinstance(diff.get("diff"), str) else None
    additions = 0
    deletions = 0
    if patch:
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

    if diff.get("deleted_file"):
        status = "removed"
    elif diff.get("new_file"):
        status = "added"
    elif diff.get("renamed_file"):
        status = "renamed"
    else:
        status = "modified"

    return {
        "filename": diff.get("new_path") or diff.get("old_path"),
        "previous_filename": diff.get("old_path"),
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "patch": patch,
    }


def build_review_body(
    *,
    approved: bool,
    slack_user_id: str,
    commit_sha: str,
    reason: str | None = None,
) -> str:
    """Build the Markdown note used for Slack-originated MR decisions."""

    result = "✅ 승인" if approved else "❌ 변경 요청"
    lines = [
        "## Slack MR Review",
        "",
        f"- 결과: {result}",
        f"- Slack 리뷰어: `{slack_user_id}`",
        f"- 검토 시각: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- 대상 커밋: `{commit_sha}`",
    ]
    if reason:
        lines.extend(["", "### 변경 요청 사유", "", reason])
    return "\n".join(lines)
