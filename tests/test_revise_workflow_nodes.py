"""Tests for app/revise_workflow/nodes.py — anchor / impact / gate / summary.

The LLM is absent by construction: these four nodes take trees and
dataclasses and return dataclasses, so the whole suite is literal in/out with
no runner, no git and no subprocess to stand in for. `changed_paths`,
`changed_lines` and `untracked` are handed to the gate as arguments — the
values git would have produced. Asserted: a term that exists anchors with a
±20-line window, a term that does not becomes MISSING with three near
misses, companion candidates always carry their evidence, and the gate
reverts out-of-whitelist files, reverts everything on a size overflow, and
catches an unreplaced value both as a literal set and as plain text.
"""

from __future__ import annotations

from app.mrdoc.literals import Literal
from app.revise_workflow.nodes import (
    build_anchor,
    build_gate,
    build_impact,
    build_summary,
    parse_replacement,
)
from app.revise_workflow.schema import (
    Evidence,
    Intent,
    Opinion,
    Required,
    Verdict,
    Verify,
)


def _required() -> Required:
    return Required(
        STATUS="OK", UNCOVERED="none", UNCERTAIN="none", CONFIDENCE="high — 대조"
    )


def _install(version: str = "3.2") -> str:
    return "\n".join(
        [
            "# 설치 안내",
            "머리말이다.",
            "",
            "## 1. 요구 사항",
            "메모리 8GB 이상",
            "",
            "## 2. 설치 절차",
            "아래 순서대로 진행한다.",
            f"요구 버전: {version}",
            "",
            "## 3. 문제 해결",
            "로그를 확인한다.",
            "",
        ]
    )


def _readme(version: str = "3.2") -> str:
    return "\n".join(
        [
            "# 개요",
            "설치는 [설치 절차](install.md#2-설치-절차) 를 본다.",
            f"요구 버전: {version}",
            "",
        ]
    )


def _tree(version: str = "3.2") -> dict[str, str]:
    return {"docs/install.md": _install(version), "docs/README.md": _readme(version)}


def _intent(
    *,
    수정방향: str = "3.2 → 4.0",
    search_terms: tuple[str, ...] = ("2. 설치 절차", "요구 버전: 3.2"),
    대상문서: tuple[str, ...] = ("docs/install.md",),
    연관문서: tuple[str, ...] = (),
    opinion_id: int = 41,
) -> Intent:
    return Intent(
        mr_iid=11,
        project_id="1009",
        round=1,
        base_sha="7c41d0e",
        opinions=(
            Opinion(
                opinion_id=opinion_id,
                op="modify",
                대상문서=대상문서,
                연관문서=연관문서,
                범위="local",
                대상주장="요구 버전 표기가 3.2 로 되어 있다",
                수정방향=수정방향,
                근거="의견 원문",
                통합=(),
                충돌해소="none",
                search_terms=search_terms,
            ),
        ),
        required=_required(),
    )


# --------------------------------------------------------------------------
# anchor
# --------------------------------------------------------------------------


def test_anchor_resolves_every_term_to_a_file_section_and_line() -> None:
    anchor = build_anchor(_intent(), _tree())
    entry = anchor.entries[0]
    assert entry.status == "FOUND"
    assert [(hit.term, hit.file, hit.line) for hit in entry.hits] == [
        ("2. 설치 절차", "docs/install.md", 7),
        ("요구 버전: 3.2", "docs/install.md", 9),
    ]
    assert entry.unit_id.startswith("u-")
    assert entry.hits[0].section_id.startswith("s-")
    assert anchor.resolved == 1 and anchor.missing == 0
    assert anchor.required.STATUS == "OK"


def test_anchor_window_is_the_unit_plus_minus_twenty_lines_clamped() -> None:
    entry = build_anchor(_intent(), _tree()).entries[0]
    assert entry.window is not None
    assert entry.window.file == "docs/install.md"
    assert entry.window.start == 1  # 7 - 20 clamped to the file start
    assert entry.window.end == 12  # 10 + 20 clamped to the file end


def test_anchor_files_are_the_whitelist_half() -> None:
    anchor = build_anchor(_intent(), _tree())
    assert anchor.files == ("docs/install.md",)


def test_zero_matches_is_missing_with_three_candidates() -> None:
    anchor = build_anchor(_intent(search_terms=("9. 배포 파이프라인",)), _tree())
    entry = anchor.entries[0]
    assert entry.status == "MISSING"
    assert entry.hits == ()
    assert entry.unit_id == "" and entry.window is None
    assert len(entry.candidates) == 3
    assert {candidate.file for candidate in entry.candidates} == {"docs/install.md"}
    assert anchor.required.UNCOVERED == "41"
    assert "PARTIAL" in anchor.required.STATUS


def test_a_file_outside_the_toc_is_missing_not_matched_elsewhere() -> None:
    """`요구 버전: 3.2` exists in README, but the opinion named a ghost file."""

    anchor = build_anchor(_intent(대상문서=("docs/ghost.md",)), _tree())
    entry = anchor.entries[0]
    assert entry.status == "MISSING"
    assert len(entry.candidates) == 3


def test_normalized_match_is_used_only_when_exact_finds_nothing() -> None:
    anchor = build_anchor(_intent(search_terms=("요구   버전:   3.2",)), _tree())
    entry = anchor.entries[0]
    assert entry.status == "FOUND"
    assert entry.hits[0].line == 7  # falls back to the section's heading line


# --------------------------------------------------------------------------
# impact
# --------------------------------------------------------------------------


def test_impact_reverse_searches_literals_and_inbound_links() -> None:
    tree = _tree()
    intent = _intent()
    impact = build_impact(intent, build_anchor(intent, tree), tree)
    entry = impact.entries[0]
    assert entry.anchor is not None
    assert ("kv", "요구 버전", "3.2") in [
        (lit.kind, lit.key, lit.value) for lit in entry.literals
    ]
    assert [(link.file, link.line, link.target) for link in entry.links_in] == [
        ("docs/README.md", 2, "install.md#2-설치-절차")
    ]
    vias = {(candidate.via, candidate.file) for candidate in entry.candidates}
    assert ("literal", "docs/README.md") in vias
    assert ("link", "docs/README.md") in vias
    assert impact.files == ("docs/README.md",)


def test_every_candidate_carries_its_evidence() -> None:
    tree = _tree()
    intent = _intent()
    impact = build_impact(intent, build_anchor(intent, tree), tree)
    for candidate in impact.entries[0].candidates:
        assert candidate.via in ("literal", "link")
        assert candidate.match
        assert candidate.unit_id.startswith("u-")


def test_the_anchor_unit_is_never_its_own_companion() -> None:
    tree = _tree()
    intent = _intent()
    anchor = build_anchor(intent, tree)
    impact = build_impact(intent, anchor, tree)
    own = anchor.entries[0].unit_id
    assert all(candidate.unit_id != own for candidate in impact.entries[0].candidates)


def test_a_missing_anchor_gets_no_impact_entry() -> None:
    tree = _tree()
    intent = _intent(search_terms=("9. 배포 파이프라인",))
    anchor = build_anchor(intent, tree)
    assert build_impact(intent, anchor, tree).entries == ()


def test_a_file_level_link_without_a_fragment_is_not_a_section_link() -> None:
    tree = {
        "docs/install.md": _install(),
        "docs/README.md": "# 개요\n[설치](install.md) 를 본다.\n",
    }
    intent = _intent()
    impact = build_impact(intent, build_anchor(intent, tree), tree)
    assert impact.entries[0].links_in == ()


def test_related_docs_without_evidence_are_reported_not_promoted() -> None:
    """3a's 연관문서 guess is checked against the same mechanical evidence.

    README carries real literal+link evidence, so it is a candidate — not an
    unverified related. spec.md exists but shares nothing, and ghost.md does
    not exist at all: both land in `related_unverified`, suspicion reported
    rather than silently dropped.
    """

    tree = _tree()
    tree["docs/spec.md"] = "# 사양\n관련 없는 내용이다.\n"
    intent = _intent(연관문서=("docs/README.md", "docs/spec.md", "docs/ghost.md"))
    impact = build_impact(intent, build_anchor(intent, tree), tree)
    entry = impact.entries[0]
    assert {candidate.file for candidate in entry.candidates} == {"docs/README.md"}
    assert entry.related_unverified == ("docs/ghost.md", "docs/spec.md")


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------


def _gate(changed_paths, **kwargs):
    base, head = _tree("3.2"), _tree("4.0")
    intent = kwargs.pop("intent", _intent())
    anchor = build_anchor(intent, head)
    impact = build_impact(intent, anchor, head)
    return build_gate(
        intent,
        anchor,
        impact,
        changed_paths,
        kwargs.pop("base_tree", base),
        kwargs.pop("head_tree", head),
        max_changed_files=kwargs.pop("max_changed_files", 10),
        max_changed_lines=kwargs.pop("max_changed_lines", 400),
        changed_lines=kwargs.pop("changed_lines", 2),
        untracked=kwargs.pop("untracked", []),
    )


def test_gate_passes_a_clean_substitution() -> None:
    gate = _gate(["docs/install.md", "docs/README.md"])
    assert gate.kind == "ok"
    assert gate.reverted == () and gate.revert_all is False
    assert gate.failed_opinion_ids == ()
    assert gate.paths.result == "pass"
    check = gate.literals[0]
    assert check.sets is not None
    assert (check.sets.a_in_base, check.sets.a_not_in_head, check.sets.b_in_head) == (
        True,
        True,
        True,
    )
    assert check.text.a_in_head_text is False and check.text.b_in_head_text is True
    assert check.result == "pass"
    assert gate.required.STATUS == "OK"


def test_a_file_outside_the_whitelist_lands_in_reverted() -> None:
    gate = _gate(["docs/install.md", "docs/other.md"])
    assert gate.paths.outside == ("docs/other.md",)
    assert gate.reverted == ("docs/other.md",)
    assert gate.paths.result == "fail"
    assert gate.kind == "ok"  # one file back, not the whole round
    assert "허용 밖" in gate.required.STATUS


def test_a_non_markdown_file_lands_in_reverted() -> None:
    gate = _gate(["docs/install.md", "app/main.py"])
    assert gate.paths.non_md == ("app/main.py",)
    assert "app/main.py" in gate.reverted


def test_mdx_counts_as_a_document() -> None:
    head = {**_tree("4.0"), "docs/guide.mdx": "# 안내\n요구 버전: 4.0\n"}
    base = {**_tree("3.2"), "docs/guide.mdx": "# 안내\n요구 버전: 3.2\n"}
    gate = _gate(
        ["docs/guide.mdx"], base_tree=base, head_tree=head
    )
    assert gate.paths.non_md == ()


def test_size_overflow_reverts_everything_and_fails_the_round() -> None:
    gate = _gate(["docs/install.md", "docs/README.md"], max_changed_files=1)
    assert gate.size.result == "fail"
    assert gate.kind == "failed"
    assert gate.revert_all is True
    assert gate.reverted == ("docs/README.md", "docs/install.md")
    assert gate.required.STATUS.startswith("FAILED")

    by_lines = _gate(["docs/install.md"], max_changed_lines=1, changed_lines=2)
    assert by_lines.revert_all is True and by_lines.kind == "failed"


def test_an_unreplaced_literal_becomes_a_failed_opinion_id() -> None:
    """head == base: 3b claimed an edit that never happened."""

    gate = _gate(["docs/install.md"], head_tree=_tree("3.2"))
    check = gate.literals[0]
    assert check.sets is not None
    assert check.sets.a_not_in_head is False and check.sets.b_in_head is False
    assert check.text.a_in_head_text is True
    assert check.result == "fail"
    assert gate.failed_opinion_ids == (41,)
    assert "구현 누락" in gate.required.STATUS


def test_a_plain_text_swap_has_no_sets_and_is_judged_by_text() -> None:
    base = {"docs/install.md": "# 안내\n\n## 2. 설치 절차\n먼저 구성 요소를 확인한다.\n"}
    head = {"docs/install.md": "# 안내\n\n## 2. 설치 절차\n먼저 컴포넌트를 확인한다.\n"}
    intent = _intent(수정방향="구성 요소 → 컴포넌트", search_terms=("2. 설치 절차",))
    anchor = build_anchor(intent, head)
    gate = build_gate(
        intent,
        anchor,
        build_impact(intent, anchor, head),
        ["docs/install.md"],
        base,
        head,
        max_changed_files=10,
        max_changed_lines=400,
        changed_lines=1,
        untracked=[],
    )
    check = gate.literals[0]
    assert check.sets is None
    assert check.text.a_in_head_text is False and check.text.b_in_head_text is True
    assert check.result == "pass"
    assert "평문 치환" in gate.required.UNCERTAIN


def test_a_plain_text_swap_that_did_not_happen_still_fails() -> None:
    body = "# 안내\n\n## 2. 설치 절차\n먼저 구성 요소를 확인한다.\n"
    intent = _intent(수정방향="구성 요소 → 컴포넌트", search_terms=("2. 설치 절차",))
    anchor = build_anchor(intent, {"docs/install.md": body})
    gate = build_gate(
        intent,
        anchor,
        build_impact(intent, anchor, {"docs/install.md": body}),
        ["docs/install.md"],
        {"docs/install.md": body},
        {"docs/install.md": body},
        max_changed_files=10,
        max_changed_lines=400,
        changed_lines=0,
        untracked=[],
    )
    assert gate.literals[0].sets is None
    assert gate.literals[0].result == "fail"
    assert gate.failed_opinion_ids == (41,)


def test_untracked_residue_is_recorded() -> None:
    gate = _gate(["docs/install.md"], untracked=["docs/scratch.md", ".revise/tmp"])
    assert gate.residue.untracked == (".revise/tmp", "docs/scratch.md")
    assert gate.residue.result == "fail"
    assert gate.kind == "ok"
    assert "untracked" in gate.required.STATUS


def test_windows_paths_are_normalized_to_posix() -> None:
    gate = _gate(["docs\\install.md"], untracked=["docs\\scratch.md"])
    assert gate.paths.changed == ("docs/install.md",)
    assert gate.residue.untracked == ("docs/scratch.md",)


def test_an_opinion_without_an_arrow_gets_no_literal_check() -> None:
    gate = _gate(["docs/install.md"], intent=_intent(수정방향="문장을 더 친절하게 다듬어라"))
    assert gate.literals == ()
    assert gate.failed_opinion_ids == ()


def test_parse_replacement_accepts_the_arrow_forms_and_strips_quotes() -> None:
    assert parse_replacement("3.2 → 4.0") == ("3.2", "4.0")
    assert parse_replacement('"3.2" -> "4.0"') == ("3.2", "4.0")
    assert parse_replacement("「3.2」 ⇒ 「4.0」") == ("3.2", "4.0")
    assert parse_replacement("표현을 다듬어라") is None
    assert parse_replacement("a → b → c") is None


# --------------------------------------------------------------------------
# summary-extract
# --------------------------------------------------------------------------


def _verify(verdict: str = "applied", 판정문: str = "그대로 반영되었다.") -> Verify:
    return Verify(
        mr_iid=11,
        round=1,
        verdicts=(
            Verdict(
                opinion_id=41,
                verdict=verdict,
                evidence=(
                    Evidence("docs/install.md", 9, "요구 버전: 3.2", "요구 버전: 4.0"),
                ),
                판정문=판정문,
            ),
        ),
        summary="요구 버전 표기를 3.2 에서 4.0 으로 바꿨다.",
        required=_required(),
    )


_LABEL = "docs/install.md § 2. 설치 절차"


def test_key_changes_are_machine_generated_from_the_literal_difference() -> None:
    summary = build_summary(
        _verify(),
        _gate(["docs/install.md", "docs/README.md"]),
        {_LABEL: [Literal("kv", "요구 버전", "3.2")]},
        {_LABEL: [Literal("kv", "요구 버전", "4.0")]},
    )
    assert summary.key_changes == (f"{_LABEL} — 요구 버전: 3.2 → 4.0",)
    assert summary.summary == "요구 버전 표기를 3.2 에서 4.0 으로 바꿨다."
    assert summary.applied == (41,)
    assert summary.points_to_watch == ()
    assert summary.commit_paths == ("docs/README.md", "docs/install.md")
    assert summary.required.STATUS == "OK"


def test_removed_and_added_literals_get_their_own_bullets() -> None:
    summary = build_summary(
        _verify(),
        _gate(["docs/install.md"]),
        {_LABEL: [Literal("kv", "폐기 항목", "예")]},
        {_LABEL: [Literal("kv", "신규 항목", "예")]},
    )
    assert summary.key_changes == (
        f"{_LABEL} — 신규 항목: 예 추가",
        f"{_LABEL} — 폐기 항목: 예 삭제",
    )


def test_points_to_watch_comes_from_partial_and_unapplied_verdicts() -> None:
    summary = build_summary(
        _verify("unapplied", "diff 가 비어 있다."),
        _gate(["docs/install.md"]),
        {},
        {},
    )
    assert summary.unapplied == (41,)
    assert summary.points_to_watch == ("[unapplied] 의견 41 — diff 가 비어 있다.",)
    assert summary.required.UNCOVERED == "41"
    assert "PARTIAL" in summary.required.STATUS


def test_reverted_files_are_dropped_from_commit_paths_and_flagged() -> None:
    gate = _gate(["docs/install.md", "docs/other.md"])
    summary = build_summary(_verify(), gate, {}, {})
    assert summary.commit_paths == ("docs/install.md",)
    assert any("허용 밖" in line for line in summary.points_to_watch)


def test_a_failed_round_commits_nothing() -> None:
    gate = _gate(["docs/install.md", "docs/README.md"], max_changed_files=1)
    summary = build_summary(_verify(), gate, {}, {})
    assert summary.commit_paths == ()
    assert any("규모 상한 초과" in line for line in summary.points_to_watch)
