"""Tests for `PRStep` (issue #119).

Real-git-repo convention throughout (no mocked `git` subprocess), matching
`tests/steps/test_rebase.py`. `gh` itself is never mocked either -- every scenario points
`PRStep.gh_executable` at `tests/scm/fakes/gh_fake.py`, a real subprocess that never talks
to an actual GitHub host (see that file's own docstring and `tests/scm/test_github.py`,
which already covers `scm/github.py`'s own request-shaping/error-handling in isolation).
This file is about `PRStep.run`'s own orchestration: the skip check, body assembly from
`ctx.intent`/`ctx.step_outcomes`, the create-vs-update branch, and the resulting
`PullRequestOutcome` (`url`/`number`/`created`) each branch reports back in `StepOutcome.
payload`.

`PRStep`'s "What Changed" diff needs a genuinely fetched `origin/<default_branch>` ref (see
`steps/pr.py`'s module docstring), so every scenario past the skip check builds on
`tests/steps/conftest.py`'s shared `origin_and_checkout` fixture (real local `origin`, real
`git fetch`) rather than a bare `git remote add` pointed at an unreachable URL. Its remote
is then swapped to a GitHub-shaped URL via `git remote set-url` purely so
`resolve_repo_slug` can parse an `owner/repo` slug for the fake-`gh` calls -- already
-fetched remote-tracking refs survive a `set-url` (`git` never revalidates/deletes them),
and `PRStep` itself never fetches again, so this is safe and never touches the network.

`PRStep` makes no agent call (see its module docstring), so `_SpyAgent` below -- mirroring
`tests/steps/test_rebase.py`'s own `_SpyAgent` -- fails loudly if it's ever invoked.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_review.agent import RunOpts
from code_review.agent.base import OutputT, Result
from code_review.pipeline.step import StepContext, StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.pr import PRStep, PullRequestOutcome
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestArtifact, TestSufficiencyOutput
from tests.steps.conftest import commit_file

FAKE_GH = Path(__file__).parent.parent / "scm" / "fakes" / "gh_fake.py"
_GITHUB_SHAPED_ORIGIN_URL = "git@github.com:khayweee/code-review.git"

_STAND_IN_INTENT = Intent(
    summary="add retry logic with exponential backoff", source="explicit", score=1.0
)


@dataclass
class _SpyAgent:
    """Records whether `run` was ever invoked and fails loudly if it was -- `PRStep` must
    never call through the agent it is given (no agent/LLM call in this ticket)."""

    run_called: bool = False

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
        self.run_called = True
        raise AssertionError("PRStep must not call Agent.run")

    async def close(self) -> None:
        pass


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _run_git(["init", "-q"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)


def _repo_on_default_branch_only(tmp_path: Path, default_branch: str = "main") -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(["checkout", "-q", "-b", default_branch], repo)
    commit_file(repo, "a.txt", "line-a\n", "initial")
    return repo


def _repo_on_feature_branch(origin_and_checkout: tuple[Path, Path]) -> Path:
    """`checkout` (real fetch from a real local `origin`, per this module's own docstring)
    with its own "feature" branch one commit ahead of the genuinely fetched `origin/main`.
    The remote is then pointed at a GitHub-shaped URL for `resolve_repo_slug` -- see this
    module's own docstring for why that's safe here.
    """

    _origin, checkout = origin_and_checkout
    commit_file(checkout, "b.txt", "line-b\n", "add b")
    _run_git(["remote", "set-url", "origin", _GITHUB_SHAPED_ORIGIN_URL], checkout)
    return checkout


def _ctx(repo: Path, branch: str, agent: _SpyAgent, **overrides: object) -> StepContext:
    # branch must be "the branch under review" PRStep would actually be reviewing -- it
    # reads ctx.branch directly now (WorktreeStep's worktree is checked out detached, so
    # there's no ctx.cwd HEAD to re-derive it from; see pr.py's own module docstring).
    defaults: dict[str, object] = dict(
        cwd=repo, branch=branch, agent=agent, diff="", intent=_STAND_IN_INTENT
    )
    defaults.update(overrides)
    return StepContext(**defaults)  # type: ignore[arg-type]


def _read_gh_log(log_file: Path) -> list[dict[str, object]]:
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines() if line]


def _run(step: PRStep, ctx: StepContext) -> StepOutcome:
    return asyncio.run(step.run(ctx))


# --- Skip on the default branch -----------------------------------------------------------


def test_pr_step_skips_cleanly_with_no_gh_call_when_already_on_the_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on_default_branch_only(tmp_path)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    outcome = _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "main", agent))

    assert agent.run_called is False
    assert outcome == StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    assert _read_gh_log(log_file) == []


def test_pr_step_default_branch_is_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on_default_branch_only(tmp_path, default_branch="trunk")
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    outcome = _run(
        PRStep(default_branch="trunk", gh_executable=FAKE_GH), _ctx(repo, "trunk", agent)
    )

    assert outcome.needs_approval is False
    assert outcome.payload == []
    assert _read_gh_log(log_file) == []


# --- Create vs. update ----------------------------------------------------------------------


def test_pr_step_creates_a_new_pr_when_none_exists_for_the_branch(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    outcome = _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "feature", agent))

    assert agent.run_called is False
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == PullRequestOutcome(
        url="https://github.com/khayweee/code-review/pull/1", number=1, created=True
    )

    calls = _read_gh_log(log_file)
    subcommands = [tuple(c["args"][:2]) for c in calls]
    assert ("pr", "view") in subcommands
    assert ("pr", "create") in subcommands
    assert ("pr", "edit") not in subcommands

    create_call = next(c for c in calls if tuple(c["args"][:2]) == ("pr", "create"))
    args = create_call["args"]
    assert args[args.index("--repo") + 1] == "khayweee/code-review"
    assert args[args.index("--head") + 1] == "feature"
    assert args[args.index("--base") + 1] == "main"
    assert args[args.index("--title") + 1] == "chore: update pull request"

    body = create_call["stdin"]
    assert "## What Changed" in body
    assert "b.txt" in body
    assert "## Intent" in body
    assert _STAND_IN_INTENT.summary in body
    assert "## Risk Assessment" not in body
    assert "## Testing" not in body


def test_pr_step_updates_the_existing_pr_when_one_already_exists_for_the_branch(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on_feature_branch(origin_and_checkout)
    existing = {
        "number": 9,
        "url": "https://github.com/khayweee/code-review/pull/9",
        "title": "chore: update pull request",
        "body": "stale",
    }
    monkeypatch.setenv("FAKE_GH_EXISTING_PR_JSON", json.dumps(existing))
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    outcome = _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "feature", agent))

    assert agent.run_called is False
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == PullRequestOutcome(
        url="https://github.com/khayweee/code-review/pull/9", number=9, created=False
    )

    calls = _read_gh_log(log_file)
    subcommands = [tuple(c["args"][:2]) for c in calls]
    assert ("pr", "edit") in subcommands
    assert ("pr", "create") not in subcommands

    edit_call = next(c for c in calls if tuple(c["args"][:2]) == ("pr", "edit"))
    assert edit_call["args"][2] == "9"


# --- "What Changed" diffs against origin/<default_branch>, never a stale local ref ---------


def test_pr_step_diffs_against_the_fetched_origin_default_branch_not_a_stale_local_one(
    origin_and_checkout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression pin: "What Changed" must diff against the fetched `origin/<default_branch>`
    ref, never the literal local `<default_branch>` ref, which can be arbitrarily stale --
    mirrors `steps/rebase.py`'s own `git rebase origin/<default_branch>`, never the local
    ref, for the identical reason (`RebaseStep` runs earlier in the same pipeline and only
    ever updates the remote-tracking ref via `git fetch`).

    Setup: local "main" is created once, tracking `origin/main`'s *original* tip, and never
    updated again. `origin` then advances with a commit unrelated to the real feature change
    (`c.txt`); the checkout fetches that advance (`origin/main`'s remote-tracking ref
    updates), but local "main" stays stale. "feature" is reset onto the *fresh*
    `origin/main` and gains its own real commit (`b.txt`). Diffing against the stale local
    "main" would incorrectly include `c.txt` (origin's own advance, not the real feature
    delta) alongside `b.txt`; diffing against `origin/main` (the fix) includes only `b.txt`.
    """

    origin, checkout = origin_and_checkout
    # Local "main", about to go stale: tracks origin/main's tip *before* origin advances.
    _run_git(["branch", "main", "origin/main"], checkout)

    commit_file(origin, "c.txt", "from origin\n", "origin advances")
    _run_git(["fetch", "-q", "origin"], checkout)  # origin/main advances; local main does not

    # feature is reset onto the fresh origin/main (a fast-forward from its own prior tip,
    # which was origin/main's pre-advance commit) and gains its own real change.
    _run_git(["checkout", "-q", "-B", "feature", "origin/main"], checkout)
    commit_file(checkout, "b.txt", "line-b\n", "add b")
    _run_git(["remote", "set-url", "origin", _GITHUB_SHAPED_ORIGIN_URL], checkout)

    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = checkout.parent / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    _run(PRStep(gh_executable=FAKE_GH), _ctx(checkout, "feature", agent))

    create_call = next(
        c for c in _read_gh_log(log_file) if tuple(c["args"][:2]) == ("pr", "create")
    )
    body = create_call["stdin"]
    assert "b.txt" in body
    assert "c.txt" not in body


# --- Body assembly from ctx.step_outcomes ----------------------------------------------------


def test_pr_step_body_includes_risk_and_testing_sections_from_prior_step_outcomes(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    review_outcome = StepOutcome(
        needs_approval=False,
        auto_fixable=False,
        payload=ReviewOutput(findings=[], risk_level="medium", risk_rationale="touches auth"),
    )
    test_sufficiency_outcome = StepOutcome(
        needs_approval=False,
        auto_fixable=False,
        payload=TestSufficiencyOutput(
            findings=[],
            tested=["retry backoff on transient failure"],
            testing_summary="covered by unit tests",
            artifacts=[
                TestArtifact(
                    kind="existing-test",
                    description="test_retries_on_failure",
                    location="tests/test_retry.py:10",
                )
            ],
        ),
    )
    ctx = _ctx(
        repo,
        "feature",
        agent,
        step_outcomes={
            "ReviewStep": review_outcome,
            "TestSufficiencyStep": test_sufficiency_outcome,
        },
    )

    _run(PRStep(gh_executable=FAKE_GH), ctx)

    calls = _read_gh_log(log_file)
    create_call = next(c for c in calls if tuple(c["args"][:2]) == ("pr", "create"))
    body = create_call["stdin"]

    assert "## Risk Assessment" in body
    assert "medium" in body
    assert "touches auth" in body
    assert "## Testing" in body
    assert "covered by unit tests" in body
    assert "retry backoff on transient failure" in body


def test_pr_step_omits_risk_and_testing_sections_when_step_outcomes_is_empty(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PRStep` must still work when driven directly against a `StepContext` built without
    going through the executor at all (`step_outcomes` defaults to `{}`)."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    outcome = _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "feature", agent))

    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert isinstance(outcome.payload, PullRequestOutcome)
    create_call = next(
        c for c in _read_gh_log(log_file) if tuple(c["args"][:2]) == ("pr", "create")
    )
    body = create_call["stdin"]
    assert "## Risk Assessment" not in body
    assert "## Testing" not in body


def test_pr_step_omits_risk_section_when_the_step_outcomes_entry_has_the_wrong_payload_type(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `step_outcomes["ReviewStep"]` entry whose payload isn't a `ReviewOutput` (e.g. a
    hand-built `StepContext` in a test with a mismatched entry) is treated the same as
    "absent" -- omitted, never rendered with a placeholder."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    agent = _SpyAgent()

    mismatched = StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    ctx = _ctx(repo, "feature", agent, step_outcomes={"ReviewStep": mismatched})

    _run(PRStep(gh_executable=FAKE_GH), ctx)

    create_call = next(
        c for c in _read_gh_log(log_file) if tuple(c["args"][:2]) == ("pr", "create")
    )
    assert "## Risk Assessment" not in create_call["stdin"]


# --- Failure modes --------------------------------------------------------------------------


def test_pr_step_raises_when_the_origin_remote_is_missing(tmp_path: Path) -> None:
    repo = _repo_on_default_branch_only(tmp_path)
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    commit_file(repo, "b.txt", "line-b\n", "add b")
    agent = _SpyAgent()

    with pytest.raises(RuntimeError, match="origin remote"):
        _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "feature", agent))
