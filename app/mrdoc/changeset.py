"""00-changeset node — raw MR diffs into the deterministic file table.

The design forbids diff bodies inside artifacts ("diff 본문 인라인 금지"):
agents must read code from the checked-out trees, not from patched fragments
that drift on rebase. What survives here is per-file identity plus per-hunk
line extents on both sides ('135-160,168-171'), comma-joined in hunk order so
the i-th old extent corresponds to the i-th new extent — that 1:1 hunk
correspondence is what 05-structure uses for pairing candidates (rule ②).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .frontmatter import parse_frontmatter, render_frontmatter
from .ids import file_id
from .tables import parse_table, render_table

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_RANGE = re.compile(r"^(\d+)(?:-(\d+))?$")


@dataclass(frozen=True)
class FileEntry:
    """One changed file with per-hunk line extents on both sides."""

    path: str
    status: str  # added | removed | renamed | modified
    old_path: str | None
    new_ranges: tuple[tuple[int, int], ...]  # head-side, hunk order
    old_ranges: tuple[tuple[int, int], ...]  # base-side, hunk order

    @property
    def fid(self) -> str:
        return file_id(self.path)


@dataclass(frozen=True)
class Changeset:
    """Everything 00-changeset.md carries — the pipeline's shared header."""

    mr_iid: int
    project_id: str
    base_sha: str
    head_sha: str
    start_sha: str
    files: tuple[FileEntry, ...]  # *.md / *.mdx only
    non_md: tuple[FileEntry, ...]
    skipped: bool  # true when the MR exceeded max_files (overflow cut)


def _extents(start: int, count: int) -> tuple[int, int]:
    """Hunk extent on one side — count 0 collapses onto the anchor line."""

    return (start, start + count - 1) if count > 0 else (start, start)


def parse_patch(patch: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Walk '@@' headers, returning (old_extents, new_extents) in hunk order.

    Extents are the header's own '-a,b +c,d' span (context included): header
    data is trusted as-is, so no body walk is needed and counts always agree
    with what GitLab served.
    """

    old: list[tuple[int, int]] = []
    new: list[tuple[int, int]] = []
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if not match:
            continue
        o_start, o_count, n_start, n_count = match.groups()
        old.append(_extents(int(o_start), int(o_count) if o_count is not None else 1))
        new.append(_extents(int(n_start), int(n_count) if n_count is not None else 1))
    return old, new


def _render_ranges(ranges: tuple[tuple[int, int], ...]) -> str:
    return ",".join(f"{a}-{b}" if a != b else f"{a}" for a, b in ranges)


def _parse_ranges(text: str) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        match = _RANGE.match(part)
        if not match:
            raise ValueError(f"bad line range: {part!r}")
        start = int(match.group(1))
        result.append((start, int(match.group(2) or start)))
    return tuple(result)


def _is_md(path: str) -> bool:
    lowered = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return lowered.endswith((".md", ".mdx"))


def build_changeset(
    *,
    mr_iid: int,
    project_id: int | str,
    base_sha: str,
    head_sha: str,
    start_sha: str,
    raw_files: list[dict[str, Any]],
    skipped: bool = False,
) -> Changeset:
    """Split normalized GitLab diffs into md/non-md file entries.

    Non-md entries carry identity only (no hunk extents): 05-structure pairs
    sections exclusively inside the md tree, so NON_MD rows exist for agent
    awareness — "this MR also touched code" — not for pairing.
    """

    md: list[FileEntry] = []
    non_md: list[FileEntry] = []
    for raw in raw_files:
        path = raw["filename"]
        patch = raw.get("patch") or ""
        old_extents, new_extents = parse_patch(patch) if patch else ([], [])
        if _is_md(path):
            md.append(
                FileEntry(
                    path=path,
                    status=raw.get("status") or "modified",
                    old_path=raw.get("previous_filename"),
                    new_ranges=tuple(new_extents),
                    old_ranges=tuple(old_extents),
                )
            )
        else:
            non_md.append(
                FileEntry(
                    path=path,
                    status=raw.get("status") or "modified",
                    old_path=raw.get("previous_filename"),
                    new_ranges=(),
                    old_ranges=(),
                )
            )
    return Changeset(
        mr_iid=mr_iid,
        project_id=str(project_id),
        base_sha=base_sha,
        head_sha=head_sha,
        start_sha=start_sha,
        files=tuple(md),
        non_md=tuple(non_md),
        skipped=skipped,
    )


def _file_row(entry: FileEntry) -> list[str]:
    return [
        entry.fid,
        entry.path,
        entry.status,
        entry.old_path or "",
        _render_ranges(entry.new_ranges),
        _render_ranges(entry.old_ranges),
    ]


_FILE_HEADER = ["file_id", "path", "status", "old_path", "new_lines", "old_lines"]


def render_changeset(changeset: Changeset) -> str:
    """Render 00-changeset.md — frontmatter + FILES + NON_MD + SKIPPED."""

    frontmatter = render_frontmatter(
        {
            "mr_iid": changeset.mr_iid,
            "project_id": changeset.project_id,
            "diff_refs": {
                "base_sha": changeset.base_sha,
                "head_sha": changeset.head_sha,
                "start_sha": changeset.start_sha,
            },
            "counts": {
                "files": len(changeset.files) + len(changeset.non_md),
                "md": len(changeset.files),
                "non_md": len(changeset.non_md),
                "skipped": changeset.skipped,
            },
        }
    )
    parts = [
        frontmatter,
        "",
        "## FILES",
        "",
        render_table(_FILE_HEADER, [_file_row(entry) for entry in changeset.files]),
        "",
        "## NON_MD",
        "",
        render_table(
            ["path", "status", "old_path"],
            [[entry.path, entry.status, entry.old_path or ""] for entry in changeset.non_md],
        ),
        "",
        "## SKIPPED",
        "",
        render_table(["path"], [["(none)"]] if not changeset.skipped else []),
    ]
    return "\n".join(parts) + "\n"


def _entry_from_row(row: list[str]) -> FileEntry:
    fid, path, status, old_path, new_lines, old_lines = row
    entry = FileEntry(
        path=path,
        status=status,
        old_path=old_path or None,
        new_ranges=_parse_ranges(new_lines),
        old_ranges=_parse_ranges(old_lines),
    )
    if entry.fid != fid:
        raise ValueError(f"file_id mismatch for {path}: {fid} != {entry.fid}")
    return entry


def parse_changeset(text: str) -> Changeset:
    """Parse 00-changeset.md back — round-trip contract for resume/tests."""

    meta = parse_frontmatter(text)
    diff_refs = meta.get("diff_refs") or {}
    counts = meta.get("counts") or {}
    files = tuple(_entry_from_row(row) for row in parse_table(text, "FILES"))
    non_md = tuple(
        FileEntry(
            path=row[0], status=row[1], old_path=row[2] or None, new_ranges=(), old_ranges=()
        )
        for row in parse_table(text, "NON_MD")
    )
    skipped_rows = parse_table(text, "SKIPPED")
    skipped = counts.get("skipped") is True or bool(
        skipped_rows and skipped_rows[0][0] != "(none)"
    )
    if len(files) + len(non_md) != counts.get("files"):
        raise ValueError("frontmatter file count does not match tables")
    return Changeset(
        mr_iid=int(meta["mr_iid"]),
        project_id=str(meta["project_id"]),
        base_sha=str(diff_refs.get("base_sha", "")),
        head_sha=str(diff_refs.get("head_sha", "")),
        start_sha=str(diff_refs.get("start_sha", "")),
        files=files,
        non_md=non_md,
        skipped=skipped,
    )
