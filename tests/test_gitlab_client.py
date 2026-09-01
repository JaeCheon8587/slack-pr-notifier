import asyncio

from app.gitlab_client import GitLabClient, _normalize_diff


class FakeResponse:
    def __init__(self, payload, status_code=200):  # type: ignore[no-untyped-def]
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self):  # type: ignore[no-untyped-def]
        return self._payload


class FakeClient:
    def __init__(self, pages):  # type: ignore[no-untyped-def]
        self._pages = iter(pages)
        self.params: list[dict[str, int]] = []

    async def get(self, _url, *, headers, params):  # type: ignore[no-untyped-def]
        assert headers["PRIVATE-TOKEN"] == "test-token"
        self.params.append(params)
        response = next(self._pages)
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


def test_list_mr_files_detects_configured_limit() -> None:
    gitlab = GitLabClient("https://gitlab.example.com", "test-token")
    client = FakeClient(
        [[
            {"new_path": "a.py", "diff": "+a"},
            {"new_path": "b.py", "diff": "+b"},
            {"new_path": "c.py", "diff": "+c"},
        ]]
    )

    files, truncated = asyncio.run(
        gitlab._list_mr_files(client, 42, 1, max_files=2)  # type: ignore[arg-type]
    )

    assert [entry["filename"] for entry in files] == ["a.py", "b.py"]
    assert truncated is True
    assert client.params == [{"per_page": 3, "page": 1}]


def test_list_mr_files_reports_complete_result() -> None:
    gitlab = GitLabClient("https://gitlab.example.com", "test-token")
    client = FakeClient([[{"new_path": "a.py", "diff": "+line"}]])

    files, truncated = asyncio.run(
        gitlab._list_mr_files(client, 42, 1, max_files=2)  # type: ignore[arg-type]
    )

    assert files[0]["additions"] == 1
    assert truncated is False


def test_normalize_diff_maps_gitlab_fields() -> None:
    result = _normalize_diff(
        {
            "old_path": "old.py",
            "new_path": "new.py",
            "renamed_file": True,
            "diff": "--- a/old.py\n+++ b/new.py\n-old\n+new",
        }
    )

    assert result == {
        "filename": "new.py",
        "previous_filename": "old.py",
        "status": "renamed",
        "additions": 1,
        "deletions": 1,
        "patch": "--- a/old.py\n+++ b/new.py\n-old\n+new",
    }


def test_list_mr_files_falls_back_to_legacy_changes_endpoint() -> None:
    gitlab = GitLabClient("https://gitlab.example.com", "test-token")
    client = FakeClient(
        [
            FakeResponse({}, status_code=500),
            FakeResponse(
                {
                    "changes": [
                        {"new_path": "legacy.py", "diff": "+legacy"},
                    ],
                    "overflow": False,
                }
            ),
        ]
    )
    files, truncated = asyncio.run(
        gitlab._list_mr_files(client, 42, 1, max_files=10)  # type: ignore[arg-type]
    )

    assert [entry["filename"] for entry in files] == ["legacy.py"]
    assert truncated is False
    assert client.params == [{"per_page": 11, "page": 1}, {}]
