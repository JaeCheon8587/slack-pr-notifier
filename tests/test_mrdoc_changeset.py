"""Tests for app/mrdoc/changeset.py — 00-changeset node."""

from __future__ import annotations

import pytest

from app.mrdoc.changeset import (
    build_changeset,
    parse_changeset,
    parse_patch,
    render_changeset,
)


def test_parse_patch_extracts_paired_extents() -> None:
    patch = "@@ -135,10 +135,12 @@ ctx\n-old\n+new\n@@ -200,3 +202,3 @@\n-x\n+y\n"
    old, new = parse_patch(patch)
    assert old == [(135, 144), (200, 202)]
    assert new == [(135, 146), (202, 204)]


def test_parse_patch_count_zero_collapses_to_anchor() -> None:
    old, new = parse_patch("@@ -135,0 +136,4 @@\n+a\n+b\n")
    assert old == [(135, 135)]
    assert new == [(136, 139)]


def _raw_files() -> list[dict]:
    return [
        {
            "filename": "docs/a|b.md",
            "previous_filename": None,
            "status": "modified",
            "patch": "@@ -1,2 +1,2 @@\n-a\n+b\n",
        },
        {
            "filename": "src/main.py",
            "previous_filename": "src/old.py",
            "status": "renamed",
            "patch": "@@ -1,1 +1,1 @@\n-a\n+b\n",
        },
    ]


def test_build_splits_md_and_non_md() -> None:
    changeset = build_changeset(
        mr_iid=7,
        project_id="grp/proj",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=_raw_files(),
    )
    assert [e.path for e in changeset.files] == ["docs/a|b.md"]
    assert [e.path for e in changeset.non_md] == ["src/main.py"]
    assert changeset.non_md[0].old_path == "src/old.py"


def test_render_parse_roundtrip_with_escaped_pipes() -> None:
    changeset = build_changeset(
        mr_iid=7,
        project_id="grp/proj",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=_raw_files(),
        skipped=True,
    )
    text = render_changeset(changeset)
    assert "## FILES" in text and "## NON_MD" in text and "## SKIPPED" in text
    assert parse_changeset(text) == changeset


def test_parse_rejects_file_id_mismatch() -> None:
    changeset = build_changeset(
        mr_iid=7,
        project_id="p",
        base_sha="b",
        head_sha="h",
        start_sha="s",
        raw_files=_raw_files(),
    )
    text = render_changeset(changeset).replace(changeset.files[0].fid, "tampered")
    with pytest.raises(ValueError):
        parse_changeset(text)
