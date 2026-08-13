"""Tests for `scm/github.py`'s `gh` CLI wrapper (issue #119).

`parse_repo_slug` is pure (no subprocess) -- tested directly against hand-written URLs.
`resolve_repo_slug` shells out to real `git` (via `gitutils.run_git`) against a real
temporary repo, matching this repo's real-subprocess convention (no mocked `git`).
`find_pull_request_for_branch`/`create_pull_request`/`update_pull_request` shell out to a
fake `gh` script (`tests/scm/fakes/gh_fake.py`) that never talks to a real GitHub host --
real subprocess spawn, real JSON parsing, fake binary, per this ticket's own testing
decision (never mock the `gh` call with a Python-level stub).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from code_review.pipeline.step import current_activity_reporter
from code_review.scm.github import (
    GhCommandError,
    PullRequest,
    _run_gh,
    create_pull_request,
    find_pull_request_for_branch,
    parse_repo_slug,
    resolve_repo_slug,
    update_pull_request,
)
from code_review.tui.activity import ActivityEvent, ActivityRelay

FAKE_GH = Path(__file__).parent / "fakes" / "gh_fake.py"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _run_git(["init", "-q"], path)


# --- parse_repo_slug (pure) ---------------------------------------------------------------


def test_parse_repo_slug_parses_the_ssh_remote_form() -> None:
    assert parse_repo_slug("git@github.com:khayweee/code-review.git") == "khayweee/code-review"


def test_parse_repo_slug_parses_the_https_remote_form() -> None:
    url = "https://github.com/khayweee/code-review.git"
    assert parse_repo_slug(url) == "khayweee/code-review"


def test_parse_repo_slug_parses_the_https_remote_form_without_a_dot_git_suffix() -> None:
    assert parse_repo_slug("https://github.com/khayweee/code-review") == "khayweee/code-review"


def test_parse_repo_slug_returns_none_for_an_unrecognized_url() -> None:
    assert parse_repo_slug("/local/path/to/origin") is None
    assert parse_repo_slug("") is None


# --- resolve_repo_slug (real git, no gh call) ----------------------------------------------


def test_resolve_repo_slug_parses_the_origin_remotes_url(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(["remote", "add", "origin", "git@github.com:khayweee/code-review.git"], repo)

    assert asyncio.run(resolve_repo_slug(repo)) == "khayweee/code-review"


def test_resolve_repo_slug_returns_none_when_origin_remote_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    assert asyncio.run(resolve_repo_slug(repo)) is None


def test_resolve_repo_slug_returns_none_when_the_remote_url_does_not_parse(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    _init_repo(origin)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(["remote", "add", "origin", str(origin)], repo)

    assert asyncio.run(resolve_repo_slug(repo)) is None


# --- find_pull_request_for_branch (fake gh) -------------------------------------------------


def test_find_pull_request_for_branch_returns_none_when_no_pr_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)

    result = asyncio.run(
        find_pull_request_for_branch(
            "feature/change", "khayweee/code-review", tmp_path, gh_executable=FAKE_GH
        )
    )

    assert result is None


def test_find_pull_request_for_branch_returns_the_existing_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = {
        "number": 42,
        "url": "https://github.com/khayweee/code-review/pull/42",
        "title": "chore: update pull request",
        "body": "old body",
    }
    monkeypatch.setenv("FAKE_GH_EXISTING_PR_JSON", json.dumps(existing))

    result = asyncio.run(
        find_pull_request_for_branch(
            "feature/change", "khayweee/code-review", tmp_path, gh_executable=FAKE_GH
        )
    )

    assert result == PullRequest(
        number=42,
        url="https://github.com/khayweee/code-review/pull/42",
        title="chore: update pull request",
        body="old body",
    )


def test_find_pull_request_for_branch_raises_on_a_genuine_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "authentication required")

    with pytest.raises(GhCommandError, match="authentication required"):
        asyncio.run(
            find_pull_request_for_branch(
                "feature/change", "khayweee/code-review", tmp_path, gh_executable=FAKE_GH
            )
        )


def test_find_pull_request_for_branch_passes_the_branch_and_repo_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    asyncio.run(
        find_pull_request_for_branch(
            "feature/change", "khayweee/code-review", tmp_path, gh_executable=FAKE_GH
        )
    )

    logged = json.loads(log_file.read_text().strip())
    args = logged["args"]
    assert args[:3] == ["pr", "view", "feature/change"]
    assert "--repo" in args and args[args.index("--repo") + 1] == "khayweee/code-review"


# --- create_pull_request (fake gh) ----------------------------------------------------------


def test_create_pull_request_returns_the_new_pr_parsed_from_the_printed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_NEW_PR_NUMBER", "7")

    result = asyncio.run(
        create_pull_request(
            repo_slug="khayweee/code-review",
            head="feature/change",
            base="main",
            title="chore: update pull request",
            body="## What Changed\n\nsomething",
            cwd=tmp_path,
            gh_executable=FAKE_GH,
        )
    )

    assert result == PullRequest(
        number=7,
        url="https://github.com/khayweee/code-review/pull/7",
        title="chore: update pull request",
        body="## What Changed\n\nsomething",
    )


def test_create_pull_request_pipes_the_body_via_stdin_with_explicit_head_base_and_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    asyncio.run(
        create_pull_request(
            repo_slug="khayweee/code-review",
            head="feature/change",
            base="main",
            title="chore: update pull request",
            body="## What Changed\n\nsomething",
            cwd=tmp_path,
            gh_executable=FAKE_GH,
        )
    )

    logged = json.loads(log_file.read_text().strip())
    args = logged["args"]
    assert args[:2] == ["pr", "create"]
    assert "--repo" in args and args[args.index("--repo") + 1] == "khayweee/code-review"
    assert "--head" in args and args[args.index("--head") + 1] == "feature/change"
    assert "--base" in args and args[args.index("--base") + 1] == "main"
    assert "--body-file" in args and args[args.index("--body-file") + 1] == "-"
    assert logged["stdin"] == "## What Changed\n\nsomething"


def test_create_pull_request_raises_on_a_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "could not create pull request")

    with pytest.raises(GhCommandError, match="could not create pull request"):
        asyncio.run(
            create_pull_request(
                repo_slug="khayweee/code-review",
                head="feature/change",
                base="main",
                title="t",
                body="b",
                cwd=tmp_path,
                gh_executable=FAKE_GH,
            )
        )


# --- update_pull_request (fake gh) ----------------------------------------------------------


def test_update_pull_request_edits_the_existing_pr_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    result = asyncio.run(
        update_pull_request(
            42,
            repo_slug="khayweee/code-review",
            title="chore: update pull request",
            body="## What Changed\n\nnew content",
            cwd=tmp_path,
            gh_executable=FAKE_GH,
        )
    )

    assert result == PullRequest(
        number=42,
        url="https://github.com/khayweee/code-review/pull/42",
        title="chore: update pull request",
        body="## What Changed\n\nnew content",
    )

    logged = json.loads(log_file.read_text().strip())
    args = logged["args"]
    assert args[:2] == ["pr", "edit"]
    assert args[2] == "42"
    assert "--repo" in args and args[args.index("--repo") + 1] == "khayweee/code-review"
    assert logged["stdin"] == "## What Changed\n\nnew content"


def test_update_pull_request_raises_on_a_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "could not edit pull request")

    with pytest.raises(GhCommandError, match="could not edit pull request"):
        asyncio.run(
            update_pull_request(
                42,
                repo_slug="khayweee/code-review",
                title="t",
                body="b",
                cwd=tmp_path,
                gh_executable=FAKE_GH,
            )
        )


# --- Activity reporting ---------------------------------------------------------------------


def test_run_gh_reports_a_started_and_finished_activity_when_a_reporter_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    relay = ActivityRelay()

    async def scenario() -> list[ActivityEvent]:
        token = current_activity_reporter.set(relay)
        try:
            await find_pull_request_for_branch(
                "feature/change", "khayweee/code-review", tmp_path, gh_executable=FAKE_GH
            )
        finally:
            current_activity_reporter.reset(token)
        return [await relay.next_event(), await relay.next_event()]

    started, finished = asyncio.run(scenario())

    assert started.status == "started"
    assert started.label == "gh pr view"
    assert finished.status == "finished"
    assert finished.activity_id == started.activity_id


def test_run_gh_reports_the_finished_activity_as_failed_on_a_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `test_gitutils.py`'s equivalent `run_git` proof: a nonzero-exit `gh` call
    (an ordinary, non-exceptional outcome for `_run_gh`, per its own docstring) still gets a
    matching started/finished pair, with the "finished" event's `error`
    (`ActivityHandle.fail(...)`) set to the raw, mechanical exit-code fact -- independent of
    whatever meaning a caller one layer up (e.g. `find_pull_request_for_branch`) gives a
    nonzero exit."""

    monkeypatch.setenv("FAKE_GH_FAIL", "boom")
    relay = ActivityRelay()

    async def scenario() -> list[ActivityEvent]:
        token = current_activity_reporter.set(relay)
        try:
            result = await _run_gh(["pr", "view"], tmp_path, gh_executable=FAKE_GH)
        finally:
            current_activity_reporter.reset(token)
        assert result.returncode != 0
        return [await relay.next_event(), await relay.next_event()]

    started, finished = asyncio.run(scenario())

    assert started.status == "started"
    assert finished.status == "finished"
    assert finished.activity_id == started.activity_id
    assert finished.error == "exit 1"
