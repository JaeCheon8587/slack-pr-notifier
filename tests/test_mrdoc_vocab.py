"""Tests for app/mrdoc/vocab.py — the banned-term list and its one probe.

The point of the module is that the prompt and the gate read the same
constant, so the tests that matter are: the probe finds what the list says,
and the class label 의미(값) is not itself a violation.
"""

from __future__ import annotations

from app.mrdoc import vocab


def test_every_banned_term_is_found_in_its_own_sentence() -> None:
    for term in vocab.BANNED_TERMS:
        assert term in vocab.find_violations(f"설명 문장에 {term} 가 들어 있다")


def test_nested_term_reports_once_longest_wins() -> None:
    """'적절' sits inside '부적절' — the covered occurrence must not count."""

    assert vocab.find_violations("부적절한 표현") == ["부적절"]


def test_standalone_occurrence_still_reports_next_to_a_longer_one() -> None:
    assert vocab.find_violations("적절하지만 부적절한 곳도 있다") == ["적절", "부적절"]


def test_violations_come_back_deduped_in_list_order() -> None:
    text = "상향된 값이라 개선되었고, 개선의 배경은 릴리스 주기 때문이다."
    assert vocab.find_violations(text) == ["때문", "배경", "개선", "상향"]


def test_clean_explanation_has_no_violations() -> None:
    text = "검증 기준이 3.12.4 에서 3.12.7 로 바뀌고, 최소 지원이 3.8+ 에서 3.10+ 로 바뀌었다."
    assert vocab.find_violations(text) == []


def test_class_label_is_not_a_violation() -> None:
    """'의미하다' is banned; the property label 의미(값) is not."""

    assert vocab.find_violations("이 유닛의 성질은 의미(값)이다") == []
    assert vocab.find_violations("이 변경이 의미하는 바는") == ["의미하"]


def test_empty_text_is_not_a_violation() -> None:
    assert vocab.find_violations("") == []


def test_prompt_line_lists_every_term() -> None:
    line = vocab.prompt_line()
    for term in vocab.BANNED_TERMS:
        assert term in line
