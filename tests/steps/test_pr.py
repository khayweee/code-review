"""Tests for `PRStep`.

Real-git-repo convention throughout (no mocked `git` subprocess), matching
`tests/steps/test_rebase.py`. `gh` itself is never mocked either -- every scenario points
`PRStep.gh_executable` at `tests/scm/fakes/gh_fake.py`, a real subprocess that never talks
to an actual GitHub host (see that file's own docstring and `tests/scm/test_github.py`,
which already covers `scm/github.py`'s own request-shaping/error-handling in isolation).
Nor is the agent: scenarios that exercise `PRStep`'s one drafting call use the real
`ClaudeCLI` backend pointed at a fake CLI script under `tests/pipeline/fakes/`, exactly as
`tests/steps/test_review.py` does. This file is about `PRStep.run`'s own orchestration: the
skip check, the drafted-vs-fallback title/"What Changed" branch, body assembly from
`ctx.intent`/`ctx.step_outcomes`, the create-vs-update branch, and the resulting
`PullRequestOutcome` (`url`/`number`/`created`) each branch reports back in `StepOutcome.
payload`.

`PRStep` both pushes to `origin` and diffs against a genuinely fetched
`origin/<default_branch>` ref (see `steps/pr.py`'s module docstring), so every scenario past
the skip check builds on `tests/steps/conftest.py`'s shared `origin_and_checkout` fixture
(real local `origin`, real `git fetch`) rather than a bare `git remote add` pointed at an
unreachable URL, and asserts on the real `origin` repo's refs afterwards rather than on a
recorded command string. `_wire_origin_for_slug_and_push` explains how one remote serves
both a GitHub-shaped slug and a real local push target without ever touching the network.

Scenarios that are about something other than drafting (create vs. update, the
origin-vs-stale-local-ref diff pin, the Risk/Testing sections) run against
`AGENT_FAILURE_FAKE_CLI`, so they exercise the deterministic fallback and keep asserting on
a body that doesn't depend on what an agent said.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_review.agent import Agent, ClaudeCLI, RunOpts, Usage
from code_review.agent.base import OutputT, Result
from code_review.pipeline.step import StepContext, StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.pr import (
    _FALLBACK_TITLE,
    _MAX_BODY_LENGTH,
    _TRUNCATION_MARKER,
    Demonstration,
    PRStep,
    PullRequestOutcome,
    _cleaned_what_changed_bullets,
    _evidence_section,
    _fit_body_to_github_limit,
    _name_status_bullet,
    _observed_testing_material,
    _sanitized_title,
)
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestArtifact, TestSufficiencyOutput
from tests.steps.conftest import commit_file

FAKE_GH = Path(__file__).parent.parent / "scm" / "fakes" / "gh_fake.py"
_GITHUB_SHAPED_ORIGIN_URL = "git@github.com:khayweee/code-review.git"

_FAKES = Path(__file__).parent.parent / "pipeline" / "fakes"
DRAFT_FAKE_CLI = _FAKES / "pr_draft_clean.py"
DRAFT_NEEDING_SANITIZING_FAKE_CLI = _FAKES / "pr_draft_needing_sanitizing.py"
DRAFT_WITH_DEMONSTRATIONS_FAKE_CLI = _FAKES / "pr_draft_with_demonstrations.py"
GROUNDING_PROBE_FAKE_CLI = _FAKES / "pr_draft_grounding_probe.py"
# Reused directly from `tests/agent/` rather than copied into `pipeline/fakes/` -- it is a
# generic "start, then exit non-zero" double with no `PRDraft`-specific behavior, and what
# this file needs from it is only that the real backend turns it into a real `AgentError`
# (`ProcessExitError`), which is exactly what drives `PRStep`'s deterministic fallback.
AGENT_FAILURE_FAKE_CLI = Path(__file__).parent.parent / "agent" / "fakes" / "nonzero_exit.py"

_STAND_IN_INTENT = Intent(
    summary="add retry logic with exponential backoff", source="explicit", score=1.0
)


@dataclass
class _SpyAgent:
    """Records whether `run` was ever invoked and fails loudly if it was -- used only by the
    skip-on-the-default-branch scenarios, where `PRStep` must return before it ever reaches
    its one drafting call."""

    run_called: bool = False

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
        self.run_called = True
        raise AssertionError("PRStep must not call Agent.run before the skip check")

    async def close(self) -> None:
        pass


@pytest.fixture
def agent() -> Iterator[Agent]:
    """The real Milestone 1 backend; which fake CLI it shells out to is chosen per test via
    `PRStep.executable`."""

    backend = ClaudeCLI()
    yield backend
    asyncio.run(backend.close())


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


def _wire_origin_for_slug_and_push(checkout: Path, origin: Path) -> None:
    """Point `origin`'s fetch URL at a GitHub-shaped address while keeping pushes aimed at
    the real local `origin` repo, so one remote satisfies both things `PRStep` asks of it.

    Both halves are ordinary git behavior, not a stub: `resolve_repo_slug` reads `git remote
    get-url origin`, which reports the configured fetch URL and so yields a parsable
    `owner/repo` slug for the fake-`gh` calls, while `git push origin` prefers
    `remote.origin.pushurl` and therefore lands in a real repository these tests can inspect
    afterwards. No network access on either path. Already-fetched remote-tracking refs
    survive a `set-url` (`git` never revalidates or deletes them) and `PRStep` never fetches
    again, so the fixture's `origin/main` stays valid too.
    """

    _run_git(["remote", "set-url", "origin", _GITHUB_SHAPED_ORIGIN_URL], checkout)
    _run_git(["remote", "set-url", "--push", "origin", str(origin)], checkout)


def _ref_sha(repo: Path, ref: str) -> str | None:
    """`ref`'s commit SHA in `repo`, or `None` if it doesn't exist there."""

    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip() or None


def _tracked_files(repo: Path, ref: str) -> list[str]:
    return _run_git(["ls-tree", "--name-only", "-r", ref], repo).stdout.split()


def _repo_on_feature_branch(origin_and_checkout: tuple[Path, Path]) -> Path:
    """`checkout` (real fetch from a real local `origin`, per this module's own docstring)
    with its own "feature" branch one commit ahead of the genuinely fetched `origin/main`,
    and its remote wired up per `_wire_origin_for_slug_and_push`.
    """

    origin, checkout = origin_and_checkout
    commit_file(checkout, "b.txt", "line-b\n", "add b")
    _wire_origin_for_slug_and_push(checkout, origin)
    return checkout


def _ctx(repo: Path, branch: str, agent: Agent, **overrides: object) -> StepContext:
    # branch must be "the branch under review" PRStep would actually be reviewing -- it
    # reads ctx.branch directly now (WorktreeStep's worktree is checked out detached, so
    # there's no ctx.cwd HEAD to re-derive it from; see pr.py's own module docstring).
    defaults: dict[str, object] = dict(
        cwd=repo, branch=branch, agent=agent, diff="", intent=_STAND_IN_INTENT
    )
    defaults.update(overrides)
    return StepContext(**defaults)  # type: ignore[arg-type]


def _reviewed_and_tested_step_outcomes() -> dict[str, StepOutcome]:
    """`step_outcomes` as the executor would have left them by the time `PRStep` runs, so
    the assembled body carries its Risk Assessment and Testing sections."""

    return {
        "ReviewStep": StepOutcome(
            needs_approval=False,
            auto_fixable=False,
            payload=ReviewOutput(findings=[], risk_level="medium", risk_rationale="touches auth"),
        ),
        "TestSufficiencyStep": StepOutcome(
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
        ),
    }


def _read_gh_log(log_file: Path) -> list[dict[str, object]]:
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines() if line]


def _created_pr_call(log_file: Path) -> dict[str, object]:
    return next(c for c in _read_gh_log(log_file) if tuple(c["args"][:2]) == ("pr", "create"))


def _created_pr_title(log_file: Path) -> str:
    args = _created_pr_call(log_file)["args"]
    assert isinstance(args, list)
    title = args[args.index("--title") + 1]
    assert isinstance(title, str)
    return title


def _created_pr_body(log_file: Path) -> str:
    body = _created_pr_call(log_file)["stdin"]
    assert isinstance(body, str)
    return body


def _run(step: PRStep, ctx: StepContext) -> StepOutcome:
    return asyncio.run(step.run(ctx))


# --- Skip on the default branch -----------------------------------------------------------


def test_pr_step_skips_cleanly_with_no_gh_call_when_already_on_the_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on_default_branch_only(tmp_path)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    spy = _SpyAgent()

    outcome = _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "main", spy))

    assert spy.run_called is False
    assert outcome == StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    assert _read_gh_log(log_file) == []


def test_pr_step_default_branch_is_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on_default_branch_only(tmp_path, default_branch="trunk")
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    spy = _SpyAgent()

    outcome = _run(PRStep(default_branch="trunk", gh_executable=FAKE_GH), _ctx(repo, "trunk", spy))

    assert spy.run_called is False
    assert outcome.needs_approval is False
    assert outcome.payload == []
    assert _read_gh_log(log_file) == []


# --- Agent-drafted title and "What Changed" --------------------------------------------------


def test_pr_step_body_is_the_agents_draft_with_the_generated_sections_appended(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """The whole assembled artifact, end to end: a conventional-commit title from the one
    drafting call, its bullets under "What Changed", and the deterministically assembled
    Intent/Risk/Testing sections after them."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    ctx = _ctx(repo, "feature", agent, step_outcomes=_reviewed_and_tested_step_outcomes())

    _run(PRStep(gh_executable=FAKE_GH, executable=DRAFT_FAKE_CLI), ctx)

    assert _created_pr_title(log_file) == (
        "feat(agent): retry transient backend failures with exponential backoff"
    )
    assert _created_pr_body(log_file) == (
        "## What Changed\n"
        "\n"
        "- Transient backend failures are now retried instead of surfacing immediately.\n"
        "- Backoff grows exponentially between attempts, with a bounded attempt count.\n"
        "- A permanently failing call still raises the original error once retries run out.\n"
        "\n"
        "## Intent\n"
        "\n"
        "add retry logic with exponential backoff\n"
        "\n"
        "## Risk Assessment\n"
        "\n"
        "**Risk level:** medium\n"
        "\n"
        "touches auth\n"
        "\n"
        "## Testing\n"
        "\n"
        "covered by unit tests\n"
        "\n"
        "- retry backoff on transient failure"
    )


def test_pr_step_outcome_carries_the_drafting_calls_usage(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(tmp_path / "gh.log"))

    outcome = _run(
        PRStep(gh_executable=FAKE_GH, executable=DRAFT_FAKE_CLI), _ctx(repo, "feature", agent)
    )

    assert outcome.usage == Usage(input_tokens=900, output_tokens=120, total_cost_usd=0.0175)


def test_pr_step_sanitizes_the_drafted_title_and_bullets_before_they_reach_the_pr(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """The drafting prompt asks for a one-line, heading-free title and bare bullets, but
    prompt wording is not an invariant: a draft that breaks every one of those rules must
    still produce a usable title and a "What Changed" that doesn't duplicate a generated
    section heading."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    ctx = _ctx(repo, "feature", agent, step_outcomes=_reviewed_and_tested_step_outcomes())

    _run(PRStep(gh_executable=FAKE_GH, executable=DRAFT_NEEDING_SANITIZING_FAKE_CLI), ctx)

    title = _created_pr_title(log_file)
    assert title.startswith("feat(agent): retry transient backend failures and also x")
    assert len(title) == 256  # GitHub's own PR-title limit
    assert "\n" not in title
    assert not title.startswith("#")

    body = _created_pr_body(log_file)
    assert body.startswith(
        "## What Changed\n"
        "\n"
        "- Transient backend failures are now retried.\n"
        "- Backoff grows exponentially between attempts.\n"
        "\n"
        "## Intent\n"
    )
    # The echoed "## Testing" bullet was dropped, so the real generated section is the only
    # one in the body.
    assert body.count("## Testing") == 1


# --- Deterministic fallback when the drafting call fails -------------------------------------


def test_pr_step_falls_back_to_the_deterministic_title_and_body_when_the_agent_call_fails(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """`AGENT_FAILURE_FAKE_CLI` exits non-zero, which the real backend raises as a
    `ProcessExitError` (an `AgentError`). `PRStep` answers that with the static title and a
    "What Changed" derived from `git diff --name-status` -- and still appends
    Intent/Risk/Testing, exactly as the drafted path does."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    ctx = _ctx(repo, "feature", agent, step_outcomes=_reviewed_and_tested_step_outcomes())

    outcome = _run(PRStep(gh_executable=FAKE_GH, executable=AGENT_FAILURE_FAKE_CLI), ctx)

    assert isinstance(outcome.payload, PullRequestOutcome)
    assert outcome.usage is None
    assert _created_pr_title(log_file) == _FALLBACK_TITLE
    assert _created_pr_body(log_file) == (
        "## What Changed\n"
        "\n"
        "- added `b.txt`\n"
        "\n"
        "## Intent\n"
        "\n"
        "add retry logic with exponential backoff\n"
        "\n"
        "## Risk Assessment\n"
        "\n"
        "**Risk level:** medium\n"
        "\n"
        "touches auth\n"
        "\n"
        "## Testing\n"
        "\n"
        "covered by unit tests\n"
        "\n"
        "- retry backoff on transient failure"
    )


# --- Pushing the branch to origin ------------------------------------------------------------


def _detached_worktree_advanced_past_the_branch_ref(checkout: Path, tmp_path: Path) -> Path:
    """A throwaway worktree checked out **detached** at "feature"'s tip and then advanced by
    its own commit -- the shape `WorktreeStep` plus `RebaseStep` leave behind, where the
    worktree's `HEAD` is history that does not belong to the branch ref, and where pushing
    `HEAD` instead of the branch ref would publish commits the branch never had.
    """

    worktree = tmp_path / "worktree"
    _run_git(["worktree", "add", "-q", "--detach", str(worktree), "refs/heads/feature"], checkout)
    commit_file(worktree, "worktree-only.txt", "rewritten\n", "commit only the worktree has")
    return worktree


def _publish_a_diverged_feature_branch_on_origin(origin: Path, tmp_path: Path) -> str:
    """Another contributor's clone pushes its own "feature" commit to `origin` first, so the
    local "feature" is no longer a fast-forward of the remote one. Returns `origin`'s
    resulting "feature" SHA.
    """

    other = tmp_path / "other-clone"
    _run_git(["clone", "-q", str(origin), str(other)], tmp_path)
    _run_git(["config", "user.email", "other@example.com"], other)
    _run_git(["config", "user.name", "Other"], other)
    _run_git(["checkout", "-q", "-b", "feature", "origin/main"], other)
    commit_file(other, "z.txt", "from another contributor\n", "diverging commit")
    _run_git(["push", "-q", "origin", "feature"], other)
    published = _ref_sha(origin, "feature")
    assert published is not None
    return published


def test_pr_step_publishes_a_branch_that_exists_only_locally_then_opens_its_pr(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """The ordinary case the tool was broken on: a branch you just created locally. `gh pr
    create --head` needs a remote head, so the step publishes the branch first."""

    origin, _checkout = origin_and_checkout
    repo = _repo_on_feature_branch(origin_and_checkout)
    assert _ref_sha(origin, "feature") is None, "precondition: the branch is local-only"
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    outcome = _run(
        PRStep(gh_executable=FAKE_GH, executable=DRAFT_FAKE_CLI), _ctx(repo, "feature", agent)
    )

    assert _ref_sha(origin, "feature") == _ref_sha(repo, "refs/heads/feature")
    assert "b.txt" in _tracked_files(origin, "feature")
    assert outcome.payload == PullRequestOutcome(
        url="https://github.com/khayweee/code-review/pull/1", number=1, created=True
    )


def test_pr_step_updates_the_existing_pr_when_there_is_nothing_new_to_push(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """Re-running on an already-published branch: `git push` answers "Everything
    up-to-date" and exits 0, so the step carries on to find and update the existing PR."""

    origin, _checkout = origin_and_checkout
    repo = _repo_on_feature_branch(origin_and_checkout)
    _run_git(["push", "-q", "origin", "refs/heads/feature:refs/heads/feature"], repo)
    already_published = _ref_sha(origin, "feature")

    existing = {
        "number": 9,
        "url": "https://github.com/khayweee/code-review/pull/9",
        "title": "chore: update pull request",
        "body": "stale",
    }
    monkeypatch.setenv("FAKE_GH_EXISTING_PR_JSON", json.dumps(existing))
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    outcome = _run(
        PRStep(gh_executable=FAKE_GH, executable=DRAFT_FAKE_CLI), _ctx(repo, "feature", agent)
    )

    assert _ref_sha(origin, "feature") == already_published
    assert outcome.payload == PullRequestOutcome(
        url="https://github.com/khayweee/code-review/pull/9", number=9, created=False
    )
    assert ("pr", "edit") in [tuple(c["args"][:2]) for c in _read_gh_log(log_file)]


def test_pr_step_pushes_the_branch_ref_never_the_worktrees_detached_head(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """The ticket's real risk. `PRStep` runs inside a worktree whose `HEAD` is detached and
    rebased, so `HEAD` and `refs/heads/<branch>` are genuinely different commits here --
    `HEAD` carries a file the branch ref has never seen. Pushing `HEAD` would publish that
    rewritten history as the branch; only the branch ref may be published."""

    origin, checkout = origin_and_checkout
    _repo_on_feature_branch(origin_and_checkout)
    worktree = _detached_worktree_advanced_past_the_branch_ref(checkout, tmp_path)

    branch_ref_sha = _ref_sha(checkout, "refs/heads/feature")
    worktree_head_sha = _ref_sha(worktree, "HEAD")
    assert worktree_head_sha != branch_ref_sha, "precondition: HEAD and the branch diverged"

    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(tmp_path / "gh.log"))

    _run(PRStep(gh_executable=FAKE_GH, executable=DRAFT_FAKE_CLI), _ctx(worktree, "feature", agent))

    assert _ref_sha(origin, "feature") == branch_ref_sha
    assert _ref_sha(origin, "feature") != worktree_head_sha
    assert "worktree-only.txt" not in _tracked_files(origin, "feature")


def test_pr_step_fails_naming_the_branch_when_origin_rejects_a_non_fast_forward_push(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diverged remote branch is the user's to reconcile: the step must refuse, say so,
    and leave `origin` exactly as it found it -- never force the push through.

    Driven with `_SpyAgent`, which fails loudly if invoked: the push runs before the drafting
    call precisely so an unpushable branch costs no LLM call.
    """

    origin, _checkout = origin_and_checkout
    repo = _repo_on_feature_branch(origin_and_checkout)
    published = _publish_a_diverged_feature_branch_on_origin(origin, tmp_path)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    spy = _SpyAgent()

    with pytest.raises(RuntimeError) as raised:
        _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "feature", spy))

    message = str(raised.value)
    assert "feature" in message
    assert "diverged" in message
    assert "force" in message  # the refusal is deliberate, not a bug

    assert _ref_sha(origin, "feature") == published
    assert spy.run_called is False
    assert _read_gh_log(log_file) == []


def test_pr_step_refuses_to_force_past_a_remote_branch_a_lease_push_would_have_clobbered(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins "never `--force`/`--force-with-lease`" behaviorally rather than by inspecting the
    argument vector. The branch is published, then rewritten locally (`git commit --amend`),
    which leaves `refs/remotes/origin/feature` accurate -- exactly the state in which
    `--force-with-lease` stops protecting anything and would overwrite `origin` with the
    rewritten history. A plain push is rejected instead, and `origin` keeps its commit.
    """

    origin, _checkout = origin_and_checkout
    repo = _repo_on_feature_branch(origin_and_checkout)
    _run_git(["push", "-q", "origin", "refs/heads/feature:refs/heads/feature"], repo)
    published = _ref_sha(origin, "feature")
    _run_git(["commit", "-q", "--amend", "-m", "add b (rewritten)"], repo)
    assert _ref_sha(repo, "refs/heads/feature") != published, "precondition: history rewritten"

    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    spy = _SpyAgent()

    with pytest.raises(RuntimeError, match="diverged"):
        _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "feature", spy))

    assert _ref_sha(origin, "feature") == published
    assert spy.run_called is False
    assert _read_gh_log(log_file) == []


def test_pr_step_on_the_default_branch_pushes_nothing_and_calls_neither_agent_nor_gh(
    origin_and_checkout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip check comes before the push, so a local default branch that is *ahead* of
    `origin` is still left alone. A regression surfaces either as a moved `origin/main` or
    as the push error `origin` would raise for its own checked-out branch."""

    origin, checkout = origin_and_checkout
    _run_git(["checkout", "-q", "-b", "main", "origin/main"], checkout)
    commit_file(checkout, "local-only.txt", "never pushed\n", "local main advances")
    _wire_origin_for_slug_and_push(checkout, origin)
    origin_main_before = _ref_sha(origin, "main")
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    spy = _SpyAgent()

    outcome = _run(PRStep(gh_executable=FAKE_GH), _ctx(checkout, "main", spy))

    assert outcome == StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    assert _ref_sha(origin, "main") == origin_main_before
    assert spy.run_called is False
    assert _read_gh_log(log_file) == []


# --- Create vs. update ----------------------------------------------------------------------


def test_pr_step_creates_a_new_pr_when_none_exists_for_the_branch(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    outcome = _run(
        PRStep(gh_executable=FAKE_GH, executable=AGENT_FAILURE_FAKE_CLI),
        _ctx(repo, "feature", agent),
    )

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

    args = _created_pr_call(log_file)["args"]
    assert isinstance(args, list)
    assert args[args.index("--repo") + 1] == "khayweee/code-review"
    assert args[args.index("--head") + 1] == "feature"
    assert args[args.index("--base") + 1] == "main"
    assert args[args.index("--title") + 1] == _FALLBACK_TITLE

    body = _created_pr_body(log_file)
    assert "## What Changed" in body
    assert "b.txt" in body
    assert "## Intent" in body
    assert _STAND_IN_INTENT.summary in body
    assert "## Risk Assessment" not in body
    assert "## Testing" not in body


def test_pr_step_updates_the_existing_pr_when_one_already_exists_for_the_branch(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
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

    outcome = _run(
        PRStep(gh_executable=FAKE_GH, executable=DRAFT_FAKE_CLI), _ctx(repo, "feature", agent)
    )

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
    args = edit_call["args"]
    assert isinstance(args, list)
    assert args[2] == "9"
    assert args[args.index("--title") + 1] == (
        "feat(agent): retry transient backend failures with exponential backoff"
    )


# --- "What Changed" diffs against origin/<default_branch>, never a stale local ref ---------


def test_pr_step_diffs_against_the_fetched_origin_default_branch_not_a_stale_local_one(
    origin_and_checkout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, agent: Agent
) -> None:
    """Regression pin: the fallback "What Changed" must diff against the fetched
    `origin/<default_branch>` ref, never the literal local `<default_branch>` ref, which can
    be arbitrarily stale -- mirrors `steps/rebase.py`'s own `git rebase
    origin/<default_branch>`, never the local ref, for the identical reason (`RebaseStep`
    runs earlier in the same pipeline and only ever updates the remote-tracking ref via `git
    fetch`).

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
    _wire_origin_for_slug_and_push(checkout, origin)

    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = checkout.parent / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    _run(
        PRStep(gh_executable=FAKE_GH, executable=AGENT_FAILURE_FAKE_CLI),
        _ctx(checkout, "feature", agent),
    )

    body = _created_pr_body(log_file)
    assert "b.txt" in body
    assert "c.txt" not in body


# --- Body assembly from ctx.step_outcomes ----------------------------------------------------


def test_pr_step_body_includes_risk_and_testing_sections_from_prior_step_outcomes(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    ctx = _ctx(repo, "feature", agent, step_outcomes=_reviewed_and_tested_step_outcomes())

    _run(PRStep(gh_executable=FAKE_GH, executable=AGENT_FAILURE_FAKE_CLI), ctx)

    body = _created_pr_body(log_file)
    assert "## Risk Assessment" in body
    assert "medium" in body
    assert "touches auth" in body
    assert "## Testing" in body
    assert "covered by unit tests" in body
    assert "retry backoff on transient failure" in body


def test_pr_step_omits_risk_and_testing_sections_when_step_outcomes_is_empty(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """`PRStep` must still work when driven directly against a `StepContext` built without
    going through the executor at all (`step_outcomes` defaults to `{}`)."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    outcome = _run(
        PRStep(gh_executable=FAKE_GH, executable=AGENT_FAILURE_FAKE_CLI),
        _ctx(repo, "feature", agent),
    )

    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert isinstance(outcome.payload, PullRequestOutcome)
    body = _created_pr_body(log_file)
    assert "## Risk Assessment" not in body
    assert "## Testing" not in body


def test_pr_step_omits_risk_section_when_the_step_outcomes_entry_has_the_wrong_payload_type(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """A `step_outcomes["ReviewStep"]` entry whose payload isn't a `ReviewOutput` (e.g. a
    hand-built `StepContext` in a test with a mismatched entry) is treated the same as
    "absent" -- omitted, never rendered with a placeholder."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    mismatched = StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    ctx = _ctx(repo, "feature", agent, step_outcomes={"ReviewStep": mismatched})

    _run(PRStep(gh_executable=FAKE_GH, executable=AGENT_FAILURE_FAKE_CLI), ctx)

    assert "## Risk Assessment" not in _created_pr_body(log_file)


# --- Failure modes --------------------------------------------------------------------------


def test_pr_step_raises_when_the_origin_remote_is_missing(tmp_path: Path) -> None:
    """The slug lookup happens before the drafting call, so a `_SpyAgent` that refuses to
    run still proves this raises rather than being swallowed by the fallback."""

    repo = _repo_on_default_branch_only(tmp_path)
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    commit_file(repo, "b.txt", "line-b\n", "add b")

    with pytest.raises(RuntimeError, match="origin remote"):
        _run(PRStep(gh_executable=FAKE_GH), _ctx(repo, "feature", _SpyAgent()))


# --- Evidence section ------------------------------------------------------------------------


def _behavior(label: str, **fields: str | None) -> Demonstration:
    return Demonstration(kind="behavior", label=label, **fields)


def _api(label: str, **fields: str | None) -> Demonstration:
    return Demonstration(kind="api", label=label, **fields)


def test_pr_step_body_carries_the_drafted_evidence_section_between_risk_and_testing(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """The whole artifact with evidence in it: two behavioral demonstrations share one
    table, the API one gets its own fenced exchange, and the section sits next to Testing --
    the same claim made concrete."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    ctx = _ctx(repo, "feature", agent, step_outcomes=_reviewed_and_tested_step_outcomes())

    _run(PRStep(gh_executable=FAKE_GH, executable=DRAFT_WITH_DEMONSTRATIONS_FAKE_CLI), ctx)

    assert _created_pr_body(log_file) == (
        "## What Changed\n"
        "\n"
        "- Failed uploads now retry with exponential backoff instead of giving up.\n"
        "- A retry ceiling stops the loop after five attempts and raises.\n"
        "\n"
        "## Intent\n"
        "\n"
        "add retry logic with exponential backoff\n"
        "\n"
        "## Risk Assessment\n"
        "\n"
        "**Risk level:** medium\n"
        "\n"
        "touches auth\n"
        "\n"
        "## Evidence\n"
        "\n"
        "| Behavior | Given | Was | Now |\n"
        "| --- | --- | --- | --- |\n"
        "| Backoff delay | `attempt=3` | `1s` | `4s` |\n"
        "| Retry ceiling | `attempt=6` | retries forever | raises after 5 |\n"
        "\n"
        "**POST /api/retry rate-limits after 3 attempts**\n"
        "\n"
        "```http\n"
        'POST /api/retry {"id": 1}\n'
        "\n"
        "429 Too Many Requests\n"
        '{"retry_after": 30}\n'
        "```\n"
        "\n"
        "## Testing\n"
        "\n"
        "covered by unit tests\n"
        "\n"
        "- retry backoff on transient failure"
    )


def test_evidence_api_demonstration_without_a_prior_response_is_an_unlabeled_exchange() -> None:
    """One response needs no disambiguation, so it goes in bare -- the shape verified
    against GitHub's own renderer."""

    assert _evidence_section(
        [_api("GET /health reports the build sha", given="GET /health", now='200 {"sha": "abc"}')]
    ) == (
        "## Evidence\n"
        "\n"
        "**GET /health reports the build sha**\n"
        "\n"
        "```http\n"
        "GET /health\n"
        "\n"
        '200 {"sha": "abc"}\n'
        "```"
    )


def test_evidence_api_demonstration_with_a_prior_response_labels_both() -> None:
    """Two responses in one block are ambiguous unlabeled -- a reader cannot tell which is
    the change."""

    assert _evidence_section(
        [
            _api(
                "GET /health now reports the build sha",
                given="GET /health",
                was="200 {}",
                now='200 {"sha": "abc"}',
            )
        ]
    ) == (
        "## Evidence\n"
        "\n"
        "**GET /health now reports the build sha**\n"
        "\n"
        "```http\n"
        "GET /health\n"
        "\n"
        "Was:\n"
        "200 {}\n"
        "\n"
        "Now:\n"
        '200 {"sha": "abc"}\n'
        "```"
    )


def test_evidence_groups_every_behavioral_demonstration_into_one_table() -> None:
    """A one-row table each is visually heavy and defeats the point of the section."""

    section = _evidence_section(
        [
            _behavior("Backoff delay", given="`attempt=3`", was="`1s`", now="`4s`"),
            _behavior(
                "Retry ceiling", given="`attempt=6`", was="retries forever", now="raises after 5"
            ),
        ]
    )

    assert section == (
        "## Evidence\n"
        "\n"
        "| Behavior | Given | Was | Now |\n"
        "| --- | --- | --- | --- |\n"
        "| Backoff delay | `attempt=3` | `1s` | `4s` |\n"
        "| Retry ceiling | `attempt=6` | retries forever | raises after 5 |"
    )
    assert section.count("| Behavior |") == 1  # one table, not one per demonstration


def test_evidence_table_drops_the_was_column_when_no_row_has_a_prior_value() -> None:
    assert _evidence_section(
        [
            _behavior("Empty queue", given="no jobs", now="returns immediately"),
            _behavior("Full queue", given="16 jobs", now="processes all 16"),
        ]
    ) == (
        "## Evidence\n"
        "\n"
        "| Behavior | Given | Now |\n"
        "| --- | --- | --- |\n"
        "| Empty queue | no jobs | returns immediately |\n"
        "| Full queue | 16 jobs | processes all 16 |"
    )


def test_evidence_table_keeps_the_was_column_when_any_single_row_has_one() -> None:
    section = _evidence_section(
        [
            _behavior("Empty queue", given="no jobs", now="returns immediately"),
            _behavior("Full queue", given="16 jobs", was="dropped jobs", now="processes all 16"),
        ]
    )

    assert "| Behavior | Given | Was | Now |" in section
    assert "| Empty queue | no jobs | - | returns immediately |" in section


def test_evidence_table_fills_a_valueless_cell_rather_than_leaving_it_blank() -> None:
    """The `Was` column is included whenever any row has a prior value, so rows without one
    would render as a blank gap -- which reads as a broken table, not as "no prior state"."""

    section = _evidence_section(
        [
            _behavior("Empty queue", given="no jobs", now="returns immediately"),
            _behavior("Full queue", given="16 jobs", was="dropped jobs", now="processes all 16"),
        ]
    )

    assert "|  |" not in section
    assert "| - |" in section


def test_evidence_escapes_a_pipe_so_it_stays_inside_its_own_table_cell() -> None:
    """An unescaped `|` opens a new column and shifts every later cell one to the left."""

    section = _evidence_section([_behavior("Shell pipeline", given="`a | b`", now="`a || b`")])

    assert "| Shell pipeline | `a \\| b` | `a \\|\\| b` |" in section
    assert len(section.splitlines()[-1].split(" | ")) == 3  # still three cells


def test_evidence_flattens_a_newline_in_a_cell_so_it_cannot_end_the_row_early() -> None:
    section = _evidence_section([_behavior("Multi line", given="first\nsecond", now="ok")])

    assert "| Multi line | first second | ok |" in section


def test_evidence_contains_a_backtick_fence_inside_an_api_payload() -> None:
    """A ``` inside the payload would close the block early; a longer fence contains it
    without mutating what the reviewer reads (CommonMark's own mechanism)."""

    section = _evidence_section(
        [_api("Renders a fence", given="POST /render", now="```suspicious\nnested\n```")]
    )

    assert "````http\n" in section
    assert section.endswith("\n````")
    assert "```suspicious" in section  # payload preserved verbatim, not rewritten


def test_evidence_drops_demonstrations_that_cannot_render() -> None:
    """An empty fence or an empty table row reads worse than a missing demonstration."""

    section = _evidence_section(
        [
            _behavior("", given="no label", now="dropped"),
            _behavior("Nothing to show"),
            _api("Prior result only", was="404"),
            _behavior("Survives", given="`n=1`", now="`n=2`"),
        ]
    )

    assert section == (
        "## Evidence\n"
        "\n"
        "| Behavior | Given | Now |\n"
        "| --- | --- | --- |\n"
        "| Survives | `n=1` | `n=2` |"
    )


def test_evidence_section_is_omitted_entirely_when_nothing_is_renderable() -> None:
    assert _evidence_section([_behavior("Nothing to show"), _api("", given="x")]) is None
    assert _evidence_section([]) is None


def test_pr_body_omits_the_evidence_heading_when_the_draft_has_no_demonstrations(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """`DRAFT_FAKE_CLI` returns no demonstrations at all, so the body must carry no empty
    Evidence heading."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))

    _run(PRStep(gh_executable=FAKE_GH, executable=DRAFT_FAKE_CLI), _ctx(repo, "feature", agent))

    assert "## Evidence" not in _created_pr_body(log_file)


# --- Grounding the drafting prompt ------------------------------------------------------------


def test_observed_testing_material_is_none_under_the_same_absent_case_contract() -> None:
    """No `TestSufficiencyStep` outcome means no grounding material, exactly as a missing
    outcome means no Testing section."""

    assert _observed_testing_material(_ctx(Path("."), "feature", _SpyAgent())) is None


def test_observed_testing_material_carries_tested_behaviors_and_artifacts() -> None:
    ctx = _ctx(
        Path("."),
        "feature",
        _SpyAgent(),
        step_outcomes=_reviewed_and_tested_step_outcomes(),
    )

    assert _observed_testing_material(ctx) == (
        "- verified behavior: retry backoff on transient failure\n"
        "- existing-test: test_retries_on_failure (tests/test_retry.py:10)"
    )


def test_pr_step_feeds_the_test_sufficiency_observations_into_the_drafting_prompt(
    origin_and_checkout: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> None:
    """End to end: `TestSufficiencyOutput` -> `ctx.step_outcomes` -> the drafting prompt.
    `GROUNDING_PROBE_FAKE_CLI` reports back what it found in the prompt as bullets, so this
    is asserted on the produced body rather than on prompt wording."""

    repo = _repo_on_feature_branch(origin_and_checkout)
    monkeypatch.delenv("FAKE_GH_EXISTING_PR_JSON", raising=False)
    log_file = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG_FILE", str(log_file))
    ctx = _ctx(repo, "feature", agent, step_outcomes=_reviewed_and_tested_step_outcomes())

    _run(PRStep(gh_executable=FAKE_GH, executable=GROUNDING_PROBE_FAKE_CLI), ctx)

    body = _created_pr_body(log_file)
    assert "- saw grounding instruction" in body
    assert "- saw tested behavior" in body
    assert "- saw test artifact" in body


# --- PR body length guard ----------------------------------------------------------------------

_NEVER_SHED = {
    "what_changed": "## What Changed\n\n- retries failed uploads",
    "intent": "## Intent\n\nadd retry logic",
    "risk": "## Risk Assessment\n\n**Risk level:** low\n\nno issues",
}


def _testing(
    summary: str = "covered by unit tests", tested: list[str] | None = None
) -> TestSufficiencyOutput:
    return TestSufficiencyOutput(
        findings=[], tested=tested or ["retry backoff"], testing_summary=summary, artifacts=[]
    )


def _assert_never_shed_survives(body: str) -> None:
    for section in _NEVER_SHED.values():
        assert section in body


def test_body_guard_keeps_the_full_body_when_it_already_fits() -> None:
    body = _fit_body_to_github_limit(
        **_NEVER_SHED,
        demonstrations=[_behavior("Backoff delay", given="`attempt=3`", now="`4s`")],
        testing=_testing(),
    )

    assert "| Backoff delay | `attempt=3` | `4s` |" in body
    assert "- retry backoff" in body
    _assert_never_shed_survives(body)
    assert len(body) <= _MAX_BODY_LENGTH


def test_body_guard_degrades_demonstrations_to_label_only_bullets_first() -> None:
    """Oversize comes from the demonstration payloads, so dropping just those payloads is
    enough -- the claims themselves, and the Testing list, both survive."""

    body = _fit_body_to_github_limit(
        **_NEVER_SHED,
        demonstrations=[_behavior("Backoff delay", given="x" * 70000, now="`4s`")],
        testing=_testing(),
    )

    assert "## Evidence\n\n- Backoff delay" in body
    assert "x" * 100 not in body
    assert "- retry backoff" in body  # the Testing list is a later shedding step
    _assert_never_shed_survives(body)
    assert len(body) <= _MAX_BODY_LENGTH


def test_body_guard_drops_the_evidence_section_when_even_the_labels_are_too_long() -> None:
    body = _fit_body_to_github_limit(
        **_NEVER_SHED,
        demonstrations=[_behavior("L" * 70000, given="`attempt=3`", now="`4s`")],
        testing=_testing(),
    )

    assert "## Evidence" not in body
    assert "- retry backoff" in body
    _assert_never_shed_survives(body)
    assert len(body) <= _MAX_BODY_LENGTH


def test_body_guard_drops_the_tested_list_but_keeps_the_testing_summary() -> None:
    body = _fit_body_to_github_limit(
        **_NEVER_SHED,
        demonstrations=[],
        testing=_testing(tested=["T" * 70000, "retry backoff"]),
    )

    assert "## Testing\n\ncovered by unit tests" in body
    assert "- retry backoff" not in body
    assert "T" * 100 not in body
    _assert_never_shed_survives(body)
    assert len(body) <= _MAX_BODY_LENGTH


def test_body_guard_truncates_at_a_line_boundary_once_nothing_sheddable_is_left() -> None:
    """The last resort. Oversize here is the Testing *summary*, which is never shed on its
    own -- and because the never-shed sections are ordered first, the truncation still eats
    only the tail."""

    summary = "\n".join(f"padding line {index} END" for index in range(8000))
    body = _fit_body_to_github_limit(**_NEVER_SHED, demonstrations=[], testing=_testing(summary))

    assert body.endswith(_TRUNCATION_MARKER)
    assert len(body) <= _MAX_BODY_LENGTH
    # Every line of the oversized summary ends in " END", so a head ending anywhere else
    # would mean the cut landed mid-line.
    assert body[: -len(_TRUNCATION_MARKER)].endswith(" END")
    _assert_never_shed_survives(body)


def test_body_guard_omits_the_risk_section_it_was_not_given() -> None:
    body = _fit_body_to_github_limit(
        what_changed=_NEVER_SHED["what_changed"],
        intent=_NEVER_SHED["intent"],
        risk=None,
        demonstrations=[],
        testing=None,
    )

    assert body == f"{_NEVER_SHED['what_changed']}\n\n{_NEVER_SHED['intent']}"


# --- Drafted-output sanitizing (unit) --------------------------------------------------------


def test_sanitized_title_flattens_newlines_and_strips_heading_markers() -> None:
    assert _sanitized_title("##  feat(pr): draft\n  the   title\n") == "feat(pr): draft the title"


def test_sanitized_title_caps_at_githubs_pr_title_limit() -> None:
    assert _sanitized_title("fix: " + "x" * 400) == ("fix: " + "x" * 400)[:256]


def test_sanitized_title_falls_back_when_nothing_survives_sanitizing() -> None:
    assert _sanitized_title("  ##  \n ") == _FALLBACK_TITLE


def test_cleaned_what_changed_bullets_drops_blanks_markers_and_echoed_headings() -> None:
    assert _cleaned_what_changed_bullets(
        [
            "## What Changed",
            "- retries transient failures",
            "  ",
            "* backs off exponentially",
            "Risk Assessment",
            "+ gives up after a bounded number of attempts",
            "## Intent",
            "Testing",
        ]
    ) == [
        "retries transient failures",
        "backs off exponentially",
        "gives up after a bounded number of attempts",
    ]


def test_cleaned_what_changed_bullets_is_empty_when_nothing_usable_survives() -> None:
    assert _cleaned_what_changed_bullets(["## Testing", "   ", ""]) == []


def test_name_status_bullet_renders_a_rename_with_both_paths() -> None:
    assert _name_status_bullet("R100\told/path.py\tnew/path.py") == (
        "- renamed `old/path.py` -> `new/path.py`"
    )


def test_name_status_bullet_ignores_a_line_that_is_not_a_name_status_entry() -> None:
    assert _name_status_bullet("") is None
