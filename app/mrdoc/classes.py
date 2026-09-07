"""성질 라벨 ↔ 내부 코드 — the one place the two vocabularies meet.

Artifacts and the report speak the design's labels (구조 · 의미(맥락) ·
의미(값) · 표현 · 미분류); the pipeline carries the short codes L1/L2/L3 that
predate them, and L0 for "no claim, no literal evidence". Keeping both is
cheap; keeping the translation in two places is not, so it lives here alone.

The matrix rolls 의미(맥락) and 의미(값) up into one 의미 column — the report's
axis has three properties, not four.
"""

from __future__ import annotations

STRUCTURE = "구조"
MEANING = "의미"
MEANING_CONTEXT = "의미(맥락)"
MEANING_VALUE = "의미(값)"
EXPRESSION = "표현"
UNCLASSIFIED = "미분류"

#: internal code -> the label artifacts and the report print
LABELS: dict[str, str] = {
    "L1": MEANING_CONTEXT,
    "L2": MEANING_VALUE,
    "L3": EXPRESSION,
    "L0": UNCLASSIFIED,
}
_CODES: dict[str, str] = {value: key for key, value in LABELS.items()}

#: internal code -> matrix/count axis (both 의미 flavours share one column)
_AXIS: dict[str, str] = {
    "L1": MEANING,
    "L2": MEANING,
    "L3": EXPRESSION,
    "L0": UNCLASSIFIED,
}

#: The order counts are rendered in — fixed so two runs diff cleanly.
AXIS_ORDER: tuple[str, ...] = (STRUCTURE, MEANING, EXPRESSION, UNCLASSIFIED)

#: The analyzer's coarse claim (20-analysis UNIT.class) -> internal code.
#: It may only claim 의미 or 표현; 의미(값) and 구조 are tool-determined.
CLAIM_CODES: dict[str, str] = {MEANING: "L1", EXPRESSION: "L3"}
_CLAIM_LABELS: dict[str, str] = {"L1": MEANING, "L2": MEANING, "L3": EXPRESSION}


def label(code: str) -> str:
    """Internal code -> printed label ('L2' -> '의미(값)')."""

    return LABELS.get(code, code)


def code(text: str) -> str:
    """Printed label -> internal code ('의미(값)' -> 'L2'); passthrough else."""

    return _CODES.get(text, text)


def axis(code: str) -> str:
    """Internal code -> matrix column ('L1' -> '의미')."""

    return _AXIS.get(code, UNCLASSIFIED)


def claim_label(code: str) -> str:
    """Internal code -> the analyzer's coarse claim word, '' when unclaimed."""

    return _CLAIM_LABELS.get(code, "")
