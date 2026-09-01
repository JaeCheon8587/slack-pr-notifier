import json

from app.ai_reviewer import MRReview, _build_prompt, _parse_review, _run_claude
from app.config import Settings


def test_parse_review_prefers_structured_output() -> None:
    raw = json.dumps(
        {
            "subtype": "success",
            "is_error": False,
            "structured_output": {
                "summary": "요약",
                "key_changes": ["변경"],
                "points_to_watch": ["확인"],
            },
            "result": "ignored",
        },
        ensure_ascii=False,
    )

    review = _parse_review(raw)

    assert review == MRReview(
        summary="요약",
        key_changes=["변경"],
        points_to_watch=["확인"],
    )


def test_parse_review_falls_back_to_result_json() -> None:
    raw = json.dumps(
        {
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(
                {
                    "summary": "요약",
                    "key_changes": [],
                    "points_to_watch": [],
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )

    assert _parse_review(raw) is not None


def test_build_prompt_respects_budget_and_reports_omissions() -> None:
    files = [
        {
            "filename": f"src/file_{index}.py",
            "status": "modified",
            "additions": 10,
            "deletions": 2,
            "patch": "+" + ("x" * 800),
        }
        for index in range(8)
    ]
    context = {
        "files": files,
        "contents": {entry["filename"]: "y" * 1000 for entry in files},
        "files_truncated": True,
    }
    prompt = _build_prompt(
        {
            "repository": "owner/repo",
            "iid": 1,
            "title": "Large change",
            "author": "author",
            "head_ref": "feature",
            "base_ref": "main",
        },
        context,
        max_chars=5000,
    )

    assert len(prompt) <= 5000
    assert "파일 조회 상한을 초과" in prompt
    assert "입력 예산으로 diff 생략" in prompt
    assert prompt.endswith("}")


def test_run_claude_disables_tools_and_persistence(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = '{"subtype":"success","is_error":false}'
        stderr = ""

    def run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr("app.ai_reviewer.tempfile.mkdtemp", lambda **_: str(tmp_path))
    monkeypatch.setattr("app.ai_reviewer.shutil.rmtree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.ai_reviewer.subprocess.run", run)
    settings = Settings(
        _env_file=None,
        claude_bin="claude",
        ai_effort="high",
        ai_max_budget_usd=0.5,
    )

    assert _run_claude("prompt", settings) is not None
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--safe-mode" in cmd
    assert "--disable-slash-commands" in cmd
    assert "--no-session-persistence" in cmd
    tools_index = cmd.index("--tools")
    assert cmd[tools_index + 1] == ""
    assert "--json-schema" in cmd
