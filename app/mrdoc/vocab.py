"""어휘 게이트 — the banned-term list the prompt and the gate both read.

The design allows a 설명 to say 무엇이 / 어떻게 and nothing else. Three kinds
of sentence are out: 왜 (motive, background), 그래서 (impact, meaning), and
평가 (good, bad, risky, improved). The measured run produced all three —
"상향된 표현", "절 간 기준이 일치하게 되었다" — because the prompt asked for
prose and nothing checked what came back.

This module is deliberately one constant plus one function: doc-analyzer's
system prompt inserts BANNED_TERMS verbatim and mrdoc levelcheck greps for
the same strings. Written twice, the two lists drift and the gate quietly
stops matching what the prompt forbids.
"""

from __future__ import annotations

#: Substring probes, in report order. Morphological variants are covered only
#: as far as a plain substring reaches ('때문에'/'때문이다' both hit '때문');
#: '의미하' rather than '의미' so the class label 의미(값) is not a violation.
BANNED_TERMS: tuple[str, ...] = (
    # 금지 1 — 왜 (동기 · 배경 추론)
    "왜",
    "때문",
    "동기",
    "배경",
    # 금지 2 — 그래서 (영향 · 의미)
    "영향",
    "의미하",
    # 금지 3 — 평가
    "좋다",
    "나쁘다",
    "적절",
    "부적절",
    "위험",
    "개선",
    "정렬",
    "상향",
    "하향",
)


def _spans(text: str, term: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    start = text.find(term)
    while start != -1:
        found.append((start, start + len(term)))
        start = text.find(term, start + 1)
    return found


def find_violations(text: str) -> list[str]:
    """Banned terms present in the text, in BANNED_TERMS order, deduped.

    Longest match wins: '적절' sits inside '부적절', so a lone '부적절한 표현'
    is one violation, not two. A '적절' that also stands on its own somewhere
    else in the same text still reports — only the covered occurrences drop.
    """

    if not text:
        return []
    hits = {term: _spans(text, term) for term in BANNED_TERMS if term in text}
    result: list[str] = []
    for term, spans in hits.items():
        longer = [
            span
            for other, other_spans in hits.items()
            if len(other) > len(term)
            for span in other_spans
        ]
        if any(
            not any(outer[0] <= start and end <= outer[1] for outer in longer)
            for start, end in spans
        ):
            result.append(term)
    return result


def prompt_line() -> str:
    """The list as the analyzer prompt shows it — one source, one wording."""

    return " · ".join(BANNED_TERMS)
