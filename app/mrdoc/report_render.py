"""report.html renderer — verdict/ref gate, then one self-contained page.

The design's 4 render steps live here: (1) 60-report's verdict must equal
50-collect's — a mismatch aborts, (2) every block's refs must exist in
50-collect, otherwise the block is dropped, (3) body content comes from
50-collect (the reporter only writes sentences), (4) one HTML file with
inline CSS and zero external dependencies. Hand-rolled HTML with escaping —
Jinja2 would be the pipeline's first template dependency for a page this
small.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .collect import Collect
from .frontmatter import parse_frontmatter

_BLOCK_HEADER = re.compile(r"^## ([A-Z][A-Z0-9_]*)(?: ([^\s]+))?")
_FENCE = chr(96) * 3


@dataclass(frozen=True)
class ReportBlock:
    """One 60-report.md section — refs are the traceability chain's last link."""

    kind: str  # HEADLINE | VERDICT_REASON | MUST_READ | FILE_DIGEST
    ident: str
    refs: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class ReportData:
    """60-report.md parsed — sentences only, numbers stay in 50-collect."""

    verdict: str
    status: str
    sources: str
    unsourced: str
    conflicts: str
    uncovered: str
    confidence: str
    blocks: tuple[ReportBlock, ...]


def parse_reportdata(text: str) -> ReportData:
    """Parse 60-report.md (ValueError on malformed frontmatter)."""

    meta = parse_frontmatter(text)
    blocks: list[ReportBlock] = []
    current: ReportBlock | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = _BLOCK_HEADER.match(line)
        if match:
            if current is not None:
                blocks.append(_with_body(current, body))
            current = ReportBlock(match.group(1), match.group(2) or "", (), "")
            body = []
            continue
        if current is None:
            continue
        if line.startswith(_FENCE):
            continue
        if line.startswith("refs:"):
            raw = line.partition(":")[2].strip()
            items = [
                item.strip()
                for item in raw.removeprefix("[").removesuffix("]").split(",")
                if item.strip()
            ]
            current = ReportBlock(
                current.kind, current.ident, tuple(items), current.body
            )
            continue
        body.append(line)
    if current is not None:
        blocks.append(_with_body(current, body))
    return ReportData(
        verdict=str(meta.get("verdict", "")),
        status=str(meta.get("STATUS", "")),
        sources=str(meta.get("SOURCES", "")),
        unsourced=str(meta.get("UNSOURCED", "")),
        conflicts=str(meta.get("CONFLICTS", "")),
        uncovered=str(meta.get("UNCOVERED", "")),
        confidence=str(meta.get("CONFIDENCE", "")),
        blocks=tuple(blocks),
    )


def _with_body(block: ReportBlock, lines: list[str]) -> ReportBlock:
    body = "\n".join(lines).strip()
    return ReportBlock(block.kind, block.ident, block.refs, body)


def _block(data: ReportData, kind: str, ident: str = "") -> ReportBlock | None:
    for block in data.blocks:
        if block.kind == kind and (not ident or block.ident == ident):
            return block
    return None


def _keep(block: ReportBlock | None, valid: frozenset[str]) -> ReportBlock | None:
    """Step 2 — drop any block whose refs point outside 50-collect."""

    if block is None:
        return None
    if all(ref in valid for ref in block.refs):
        return block
    return None


_VERDICT_CLASS = {"BLOCK": "block", "REVIEW": "review", "PASS": "pass"}

_CSS = (
    "body{font-family:'Segoe UI','Malgun Gothic',sans-serif;"
    "margin:0;background:#f4f5f7;color:#1a1d21}"
    ".wrap{max-width:900px;margin:0 auto;padding:24px 20px 60px}"
    ".banner{padding:18px 22px;border-radius:10px;color:#fff;font-size:20px;font-weight:600}"
    ".banner.block{background:#b3261e}.banner.review{background:#b25e09}.banner.pass{background:#1e7e34}"
    ".reason{margin:10px 0 26px;color:#424a52;font-size:14px}"
    "h2{font-size:17px;border-bottom:2px solid #d3d7de;padding-bottom:6px;margin:34px 0 14px}"
    ".unit{background:#fff;border:1px solid #dfe3e8;"
    "border-radius:8px;padding:14px 16px;margin:10px 0}"
    ".unit.must{border-color:#b25e09;box-shadow:0 0 0 1px #b25e09}"
    ".unit h3{margin:0 0 8px;font-size:15px}"
    ".ba p{margin:4px 0;font-size:14px}"
    ".facts{font-size:13px;color:#4a5560;margin:6px 0 2px}.facts b{font-weight:600}"
    "table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}"
    "th,td{border:1px solid #dfe3e8;padding:7px 10px;text-align:left;vertical-align:top}"
    "th{background:#eef1f4}.muted{color:#6b7480;font-size:13px;margin:6px 0}"
    ".finding{background:#fff;border:1px solid #e3c07a;border-left:4px solid #b25e09;"
    "border-radius:6px;padding:12px 14px;margin:10px 0;font-size:14px}"
    ".finding.blocker{border-color:#e07a74;border-left-color:#b3261e}"
    ".finding.minor{border-color:#dfe3e8;border-left-color:#8a939e}"
    ".badge{display:inline-block;font-size:11px;font-weight:700;color:#fff;border-radius:4px;"
    "padding:2px 7px;margin-right:8px;vertical-align:1px}"
    ".badge.blocker{background:#b3261e}.badge.major{background:#b25e09}.badge.minor{background:#6b7480}"
    ".ev{font-size:12.5px;color:#4a5560;margin:8px 0 4px}.ev code{background:#eef1f4;"
    "padding:1px 5px;border-radius:3px}.rec{margin:6px 0 0}.meta{font-size:13px;"
    "background:#fff;border:1px dashed #c9cfd6;border-radius:8px;padding:12px 16px}"
    ".meta p{margin:4px 0}a{color:#0b57d0}"
)


def render_report_html(
    collect: Collect,
    report_text: str,
    *,
    mr_iid: int,
    diff_url: str = "",
) -> str:
    """Gate + render. Raises ValueError when verdicts disagree (render step 1)."""

    data = parse_reportdata(report_text)
    if data.verdict != collect.verdict:
        raise ValueError(
            f"verdict mismatch: 60-report {data.verdict} != 50-collect {collect.verdict}"
        )
    valid = collect.valid_ids()
    headline = _keep(_block(data, "HEADLINE"), valid)
    verdict_reason = _keep(_block(data, "VERDICT_REASON"), valid)

    esc = html.escape
    verdict_class = _VERDICT_CLASS.get(collect.verdict, "review")
    out: list[str] = [
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>MR !{mr_iid} 문서 리뷰</title>",
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">",
        f"<div class=\"banner {verdict_class}\">"
        f"{esc(collect.verdict)} — MR !{mr_iid}</div>",
    ]
    if headline and headline.body:
        out.append(f"<div class=\"reason\">{esc(headline.body)}</div>")
    if verdict_reason and verdict_reason.body:
        out.append(f"<div class=\"reason\">{esc(verdict_reason.body)}</div>")
    elif collect.reason:
        out.append(f"<div class=\"reason\">{esc(collect.reason)}</div>")
    if diff_url:
        href = esc(diff_url, quote=True)
        out.append(f"<p class=\"muted\"><a href=\"{href}\">MR diff 보기</a></p>")

    must = set(collect.must_read)
    units = collect.units
    out.append("<h2>이번 MR이 바꾼 것</h2>")

    l1 = [u for u in units if u.level == "L1"]
    for unit in l1:
        cls = "unit must" if unit.unit_id in must else "unit"
        out.append(f"<div class=\"{cls}\"><h3>{esc(unit.section)}</h3><div class=\"ba\">")
        if unit.before:
            out.append(f"<p><b>이전</b> {esc(unit.before)}</p>")
        if unit.after:
            out.append(f"<p><b>이후</b> {esc(unit.after)}</p>")
        out.append("</div>")
        if unit.removed:
            out.append(
                "<p class=\"facts\"><b>없어진 사실</b> "
                + " · ".join(esc(v) for v in unit.removed)
                + "</p>"
            )
        if unit.added:
            out.append(
                "<p class=\"facts\"><b>생긴 사실</b> "
                + " · ".join(esc(v) for v in unit.added)
                + "</p>"
            )
        for change in unit.changed:
            out.append(
                f"<p class=\"facts\"><b>값 변경</b> {esc(change.key)}: "
                f"{esc(change.from_value)} → {esc(change.to_value)}</p>"
            )
        out.append("</div>")
    if not l1:
        out.append("<p class=\"muted\">맥락 변경(L1) 절이 없다</p>")

    l2 = [u for u in units if u.level == "L2"]
    out.append("<table><tr><th>값 변경 (L2)</th><th>키</th><th>이전</th><th>이후</th></tr>")
    rows = 0
    for unit in l2:
        star = " ★" if unit.unit_id in must else ""
        if not unit.changed:
            out.append(
                f"<tr><td>{esc(unit.section)}{star}</td><td>—</td>"
                f"<td>{esc(', '.join(unit.removed))}</td><td>{esc(', '.join(unit.added))}</td></tr>"
            )
            rows += 1
        for change in unit.changed:
            out.append(
                f"<tr><td>{esc(unit.section)}{star}</td><td>{esc(change.key)}</td>"
                f"<td>{esc(change.from_value)}</td><td>{esc(change.to_value)}</td></tr>"
            )
            rows += 1
    out.append("</table>")
    if not rows:
        out.append("<p class=\"muted\">값 변경(L2) 없음</p>")

    l3 = sum(1 for u in units if u.level == "L3")
    out.append(f"<p class=\"muted\">표현 변경(L3) {l3}건 — 문장·어순만 다듬은 절 (리뷰 제외)</p>")
    files = collect.files
    out.append(
        "<p class=\"muted\">이동 "
        + str(files.get("moved_sections", 0))
        + " · 신규 파일 "
        + str(files.get("added", 0))
        + " · 삭제 파일 "
        + str(files.get("deleted", 0))
        + "</p>"
    )

    out.append(f"<h2>주의할 점 {len(collect.findings)}건</h2>")
    if not collect.findings:
        out.append("<p class=\"muted\">없음</p>")
    for finding in collect.findings:
        cls = "finding " + finding.severity.lower()
        out.append(f"<div class=\"{cls}\">")
        out.append(
            f"<span class=\"badge {finding.severity.lower()}\">{esc(finding.severity)}</span>"
            f"<b>{esc(finding.category)}</b> · unit {esc(finding.unit_id)}"
        )
        if finding.claim:
            out.append(f"<p>{esc(finding.claim)}</p>")
        for item in finding.evidence:
            out.append(
                f"<p class=\"ev\">{esc(item.rev)} · {esc(item.file)}:{item.line} "
                f"<code>\"{esc(item.quote)}\"</code></p>"
            )
        if finding.recommendation:
            out.append(f"<p class=\"rec\"><b>권고</b> {esc(finding.recommendation)}</p>")
        out.append("</div>")

    out.append("<h2>분석 신뢰도</h2><div class=\"meta\">")
    gate = collect.gate
    out.append(
        "<p>파싱 파일 "
        + str(gate.get("files_parsed", 0))
        + " · finding 유입 "
        + str(gate.get("findings_in", 0))
        + " → 통과 "
        + str(gate.get("findings_out", 0))
        + "</p>"
    )
    out.append(
        "<p>역참조 탈락 "
        + str(gate.get("dropped_bad_check_ref", 0))
        + " · quote 검증 탈락 "
        + str(gate.get("dropped_quote_mismatch", 0))
        + " · 중복 병합 "
        + str(gate.get("merged_dup", 0))
        + "</p>"
    )
    verify = collect.verify
    out.append(
        "<p>검증 "
        + str(verify.get("rounds", 0))
        + "회 "
        + esc(str(verify.get("verdict", "")))
        + " · 지적 잔존 "
        + str(verify.get("outstanding", 0))
        + " · 레벨 승격 "
        + str(collect.levels.get("promoted", 0))
        + " · 분석 실패 파일 "
        + str(len(collect.failed_files))
        + "</p>"
    )
    dist = collect.confidence_dist
    out.append(
        "<p>CONFIDENCE high "
        + str(dist.get("high", 0))
        + " / medium "
        + str(dist.get("medium", 0))
        + " / low "
        + str(dist.get("low", 0))
        + "</p>"
    )
    if collect.uncertain:
        out.append("<p>UNCERTAIN " + " · ".join(esc(item) for item in collect.uncertain) + "</p>")
    if collect.failed_files:
        out.append(
            "<p>failed_files " + " · ".join(esc(item) for item in collect.failed_files) + "</p>"
        )
    out.append("</div></div></body></html>")
    return "\n".join(out) + "\n"
