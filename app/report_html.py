"""Standalone HTML review-report renderer (report.html).

Renders the structured AI review (app.ai_reviewer.MRReview) plus the MR
context already fetched for the AI prompt -- changed-file list, per-file
diffs, full contents -- into one self-contained HTML document.

Deliberately deterministic Python templating rather than asking the model to
emit HTML: it is testable, costs no extra AI call, and every interpolated
value is HTML-escaped (diff text is untrusted input). The rendered file is
archived under settings.report_html_dir and uploaded to the Slack
notification's thread (SlackClient.upload_report_file).
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any


_CSS = """\
:root{--bg:#f4f3ef;--panel:#ffffff;--ink:#1c1e26;--ink-2:#4b5163;--ink-3:#8b91a3;
--line:#e7e3da;--line-strong:#cabfa9;--primary:#3530a8;--ok:#1a7a45;--work:#b8431a;
--mono:Consolas,'Cascadia Mono',ui-monospace,monospace;
--sans:-apple-system,'Segoe UI','Malgun Gothic',system-ui,sans-serif}
@media (prefers-color-scheme: dark){:root{--bg:#101218;--panel:#1d212c;--ink:#e8eaf1;
--ink-2:#aeb4c4;--ink-3:#7a8194;--line:#2b3140;--line-strong:#424b5f;--primary:#8b86f5;
--ok:#5fd08a;--work:#e8865a}}
*{box-sizing:border-box}
body{margin:0;padding:40px 20px 80px;background:var(--bg);color:var(--ink);
font:15px/1.65 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto}
header{margin-bottom:32px}
.kicker{font-family:var(--mono);font-size:11px;letter-spacing:.22em;text-transform:uppercase;
color:var(--primary);margin-bottom:10px}
h1{margin:0 0 14px;font-size:clamp(22px,4vw,30px);line-height:1.2;font-weight:800}
h1 a{color:inherit;text-decoration:none;border-bottom:2px solid var(--primary)}
.meta{display:flex;flex-wrap:wrap;gap:8px}
.meta span{font-family:var(--mono);font-size:11.5px;color:var(--ink-2);background:var(--panel);
border:1px solid var(--line);border-radius:8px;padding:5px 11px}
.meta b{color:var(--ink)}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:20px 22px;margin-bottom:16px}
h2{margin:0 0 10px;font-size:16px;font-weight:700}
h2 .n{font-family:var(--mono);color:var(--primary);margin-right:8px}
p.summary{margin:4px 0;font-size:15.5px}
ul{margin:8px 0;padding-left:20px}
li{margin:5px 0;color:var(--ink-2)}
li::marker{color:var(--ink-3)}
.empty{color:var(--ink-3);font-size:13.5px;margin:4px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;
color:var(--ink-3);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line-strong)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
td.add{color:var(--ok);font-family:var(--mono);white-space:nowrap}
td.del{color:var(--work);font-family:var(--mono);white-space:nowrap}
td.mono, .mono{font-family:var(--mono);font-size:.92em}
details{border:1px dashed var(--line-strong);border-radius:9px;padding:8px 12px;margin:8px 0}
summary{cursor:pointer;font-family:var(--mono);font-size:12.5px;color:var(--ink-2)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:12px 14px;
overflow-x:auto;font:12px/1.55 var(--mono)}
pre.diff span.add{color:var(--ok);display:block}
pre.diff span.del{color:var(--work);display:block}
pre.diff span.hunk{color:var(--primary);display:block}
.notice{font-size:12.5px;color:var(--ink-3);font-family:var(--mono);margin:8px 0}
footer{margin-top:32px;padding-top:14px;border-top:1px solid var(--line);
font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:.04em}
"""


def render_review_report(
    mr: dict[str, Any], review: Any, context: dict[str, Any] | None
) -> str:
    """Render the AI review + MR context into a standalone HTML string."""

    files: list[dict[str, Any]] = (context or {}).get("files") or []
    contents: dict[str, str] = (context or {}).get("contents") or {}
    files_truncated = bool((context or {}).get("files_truncated"))

    parts = [
        '<!DOCTYPE html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
        f"<title>MR Review Report · {_e(mr.get('title') or '')}</title>\n",
        f"<style>{_CSS}</style>\n</head>\n<body>\n<div class=\"wrap\">\n",
    ]

    parts.append(_render_header(mr))
    parts.append(_render_section("01", "AI 요약", _render_summary(review)))
    parts.append(_render_section("02", "주요 변경점", _render_list(review.key_changes)))
    parts.append(
        _render_section("03", "살펴볼 지점", _render_list(review.points_to_watch))
    )
    parts.append(_render_section("04", "변경 파일", _render_file_table(files, files_truncated)))
    parts.append(_render_section("05", "파일별 diff", _render_diffs(files)))
    if contents:
        parts.append(_render_section("06", "변경 파일 전체 내용", _render_contents(contents)))

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    parts.append(
        f"<footer>MR REVIEW REPORT · {_e(str(mr.get('sha') or ''))} · GENERATED {generated}</footer>\n"
    )
    parts.append("</div>\n</body>\n</html>\n")
    return "".join(parts)


def _e(value: Any) -> str:
    """HTML-escape any value (diff text is untrusted)."""

    return html.escape(str(value), quote=True)


def _render_header(mr: dict[str, Any]) -> str:
    iid = mr.get("iid")
    title = mr.get("title") or ""
    if mr.get("url"):
        heading = f'<h1><a href="{_e(mr['url'])}">!{_e(iid)} {_e(title)}</a></h1>'
    else:
        heading = f"<h1>!{_e(iid)} {_e(title)}</h1>"
    head_ref = mr.get("head_ref") or "?"
    base_ref = mr.get("base_ref") or "?"
    return (
        f'<header>\n<div class="kicker">MR Review Report</div>\n{heading}\n'
        f'<div class="meta">'
        f"<span>저장소 <b>{_e(mr.get('repository') or mr.get('project_id') or '?')}</b></span>"
        f"<span>작성자 <b>{_e(mr.get('author') or '?')}</b></span>"
        f"<span>브랜치 <b>{_e(head_ref)} → {_e(base_ref)}</b></span>"
        f"<span>칌밋 <b>{_e(mr.get('sha') or '?')}</b></span>"
        f"</div>\n</header>\n"
    )


def _render_section(num: str, title: str, body: str) -> str:
    return (
        f'<section>\n<h2><span class="n">{num}</span>{_e(title)}</h2>\n{body}</section>\n'
    )


def _render_summary(review: Any) -> str:
    summary = str(review.summary or "").strip()
    if not summary:
        return '<p class="empty">요약이 없습니다.</p>'
    return f'<p class="summary">{_e(summary)}</p>'


def _render_list(items: Any, *, empty: str = "항목이 없습니다.") -> str:
    values = list(items or [])
    if not values:
        return f'<p class="empty">{_e(empty)}</p>'
    lines = "\n".join(f"<li>{_e(item)}</li>" for item in values)
    return f"<ul>\n{lines}\n</ul>"


def _render_file_table(files: list[dict[str, Any]], files_truncated: bool) -> str:
    if not files:
        return '<p class="empty">변경 파일 정보가 없습니다.</p>'
    rows = []
    for entry in files:
        rows.append(
            "<tr>"
            f"<td class=\"mono\">{_e(entry.get('filename') or '?')}</td>"
            f"<td>{_e(entry.get('status') or '?')}</td>"
            f'<td class="add">+{entry.get('additions', 0)}</td>'
            f'<td class="del">-{entry.get('deletions', 0)}</td>'
            "</tr>"
        )
    notice = (
        '<p class="notice">Ⓡ 파일 조회 상한 초과 — 일부 변경 파일만 표시됩니다.</p>'
        if files_truncated
        else ""
    )
    return (
        "<table>\n<tr><th>파일</th><th>상태</th><th>추가</th><th>삭제</th></tr>\n"
        + "\n".join(rows)
        + "\n</table>\n"
        + notice
    )


def _render_diffs(files: list[dict[str, Any]]) -> str:
    if not files:
        return '<p class="empty">표시할 diff가 없습니다.</p>'
    blocks = []
    for entry in files:
        name = entry.get("filename") or "?"
        status = entry.get("status")
        patch = entry.get("patch")
        header = (
            '<h2 style="font-size:13.5px"><span class="n">·</span>'
            f'<span class="mono">{_e(name)}</span> ({_e(status)}, '
            f"+{entry.get('additions', 0)} -{entry.get('deletions', 0)})</h2>"
        )
        if patch:
            body = f'<pre class="diff">{_render_diff(str(patch))}</pre>'
        else:
            body = '<p class="empty">diff 없음 — 대용량/바이너리 파일</p>'
        blocks.append(f"{header}\n{body}")
    return "\n".join(blocks)


def _render_diff(patch: str) -> str:
    """Escape a unified diff, then color +/-/@@ lines via spans."""

    lines = []
    for line in patch.splitlines():
        esc = html.escape(line)
        if line.startswith("+"):
            lines.append(f'<span class="add">{esc}</span>')
        elif line.startswith("-"):
            lines.append(f'<span class="del">{esc}</span>')
        elif line.startswith("@@"):
            lines.append(f'<span class="hunk">{esc}</span>')
        else:
            lines.append(esc)
    return "\n".join(lines)


def _render_contents(contents: dict[str, str]) -> str:
    blocks = []
    for name, text in contents.items():
        blocks.append(
            f"<details>\n<summary>{_e(name)}</summary>\n"
            f"<pre>{html.escape(str(text))}</pre>\n</details>"
        )
    return "\n".join(blocks)

