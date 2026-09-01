"""Content-hash ids — sha8, never sequential (design: "sha8 id, 순번 금지").

Sequential ids (file 1, file 2...) break artifact caching between rounds; a
content-derived hash keeps `20-analysis/<file_id>.md` stable for the same
path across pushes, which is what makes the single-retry loop and quote
cross-checking reproducible.
"""

from __future__ import annotations

import hashlib


def sha8(text: str) -> str:
    """First 8 hex chars of sha256 — the design's collision-tolerance choice."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def slugify(name: str) -> str:
    """File-name slug: non-alphanumerics collapsed to single dashes.

    `auth.md` → `auth-md`, `docusaurus.config.js` → `docusaurus-config-js`.
    """

    import re

    return re.sub(r"[^0-9A-Za-z]+", "-", name).strip("-").lower()


def file_id(path: str) -> str:
    """`<slug>-<sha8(path)>` — basename alone collides across folders."""

    basename = path.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{slugify(basename)}-{sha8(path)}"


def section_id(tree_path: str, ordinal: int, heading_path: str) -> str:
    """`s-<sha8>` over (file, ordinal, heading path).

    The ordinal disambiguates repeated heading paths inside one file (the
    design's "### 예시 twice" case) so the id stays unique per physical
    section, and identical headings at different positions never collide.
    """

    return f"s-{sha8(f'{tree_path}|{ordinal}|{heading_path}')}"


def unit_id(section_id: str) -> str:
    """`u-<sha8>` for one change unit — 1:1 with its (canonical) section."""

    return f"u-{sha8(f'unit|{section_id}')}"
