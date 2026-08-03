"""Tests for `code-review review` (Milestone 13, issue #40; four-step pipeline wiring,
issue #60): the TTY-required fail-fast path, `_diff_against_head`, and real end-to-end
runs of the full `IntentStep` -> `RebaseStep` -> `ReviewStep` -> `TestSufficiencyStep`
pipeline (`steps/registry.py`'s `IMPLEMENTED_STEPS`).

`CliRunner`'s captured stdio is never a TTY, so it is this file's natural test of the
"needs a real terminal" error path -- no mocking `isatty` (see `cli.py`'s `review`
docstring: the TTY check runs first, before intent validation, so every `CliRunner`
invocation of `review` exercises that path). Reaching the code past the TTY check (intent
validation, and the full pipeline run) instead uses a real pty via the `script` command
against a real throwaway git repo, matching this project's "real subprocess, no mocking the
external tool" testing convention (see tests/pipeline/test_executor.py, tests/test_cli_update.py).

Once `RebaseStep`/`ReviewStep`/`TestSufficiencyStep` joined `IntentStep` in
`IMPLEMENTED_STEPS` (issue #60), a full run needs two more real things this file did not
need before: a real `origin` remote for `RebaseStep`'s `git fetch`+`git rebase` (see
`repo_with_branch` below), and a real `claude` executable on `PATH` for `ReviewStep`'s and
`TestSufficiencyStep`'s one `ctx.agent.run` call each (see `_env_with_fake_claude` below;
`cli.py` builds both steps via `cls()` with no args, so there is no way to override
`RunOpts.executable` from here -- the only seam left is `PATH` itself). The full-run tests
also have to press "e" once the run finishes -- `ReviewApp` no longer exits on its own (see
`tui/app.py`'s module docstring) -- so they use `_run_review_and_press_e_to_exit` instead of
the simpler `_run_in_real_pty` the other real-pty tests use, since those exit (on a
validation error) before the TUI ever starts.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from code_review.agent import ClaudeCLI
from code_review.cli import _diff_against_head, _run_pipeline, app
from code_review.steps.intent import Intent
from code_review.tui.input_relay import InputRelay

runner = CliRunner()

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

_FAKES = Path(__file__).parent / "pipeline" / "fakes"
CLEAN_FAKE_CLAUDE = _FAKES / "claude_superset_clean.py"
BLOCKING_FAKE_CLAUDE = _FAKES / "claude_superset_blocking.py"


def _plain(output: str) -> str:
    return _ANSI_ESCAPE.sub("", output)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo_with_branch(tmp_path: Path) -> tuple[Path, str]:
    """A real repo (`repo`, this fixture's returned path) on a local branch literally named
    "main", wired to a real `origin` remote whose own "main" is the exact same commit --
    plus a `feature/change` branch one commit ahead of `repo`'s "main" with a real diff
    (`git diff HEAD...feature/change`, this fixture's `HEAD`, sees it).

    Two real local repos, mirroring `tests/steps/conftest.py`'s `origin_and_checkout`
    pattern: `origin` stands in for the remote, `repo` is the checkout under test.
    `RebaseStep` (`steps/rebase.py`) does `git fetch origin main` then `git rebase
    origin/main` against whatever `repo`'s HEAD is at call time -- so `repo`'s "main" is
    built by checking out `origin/main` directly, rather than committing its own content,
    making the two tips the exact same commit (not merely the same tree). That equality is
    what keeps `RebaseStep`'s rebase a deterministic, conflict-free no-op ("already up to
    date") regardless of which branch ends up checked out as HEAD when `review` runs, and
    is also why the issue #24 unpushed-local-default guard can never fire here: it only
    fires when local "main" is a *strict descendant* of "origin/main" (see
    `steps/rebase.py`'s module docstring), which two equal tips can never be.
    """

    origin = tmp_path / "origin"
    origin.mkdir()
    _run_git(["init", "-q"], origin)
    _run_git(["config", "user.email", "test@example.com"], origin)
    _run_git(["config", "user.name", "Test"], origin)
    _run_git(["checkout", "-q", "-b", "main"], origin)
    (origin / "greeting.txt").write_text("hello\n")
    _run_git(["add", "-A"], origin)
    _run_git(["commit", "-q", "-m", "init"], origin)

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    _run_git(["remote", "add", "origin", str(origin)], repo)
    _run_git(["fetch", "-q", "origin"], repo)
    _run_git(["checkout", "-q", "-b", "main", "origin/main"], repo)

    greeting = repo / "greeting.txt"
    _run_git(["checkout", "-q", "-b", "feature/change"], repo)
    greeting.write_text("hello\nworld\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "add world"], repo)
    _run_git(["checkout", "-q", "-"], repo)

    return repo, "feature/change"


def _env_with_fake_claude(fake_cli: Path, tmp_path: Path) -> dict[str, str]:
    """Build a subprocess environment whose `PATH` resolves the literal executable name
    "claude" (`RunOpts.executable`'s production default -- see module docstring) to
    `fake_cli`, prepended ahead of the real `PATH` so it wins over any real `claude` CLI
    that might happen to be installed on the machine running this test.
    """

    bin_dir = tmp_path / "fake_claude_bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(fake_cli.read_text())
    fake_claude.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


# --- TTY-required fail-fast path, via CliRunner (never a real TTY) ---------------------


def test_review_fails_fast_when_not_attached_to_a_tty(
    repo_with_branch: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, branch = repo_with_branch
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["review", branch, "--intent", "add world greeting"])
    output = _plain(result.output)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in output
    assert "interactive terminal" in output
    assert "tty" in output.lower() or "terminal" in output.lower()


def test_review_tty_check_runs_before_intent_validation(
    repo_with_branch: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TTY check is the very first thing `review` does -- even an invalid `--intent`
    surfaces the TTY error under `CliRunner`, not the intent `BadParameter`."""

    repo, branch = repo_with_branch
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["review", branch, "--intent", "   "])
    output = _plain(result.output)

    assert result.exit_code == 1
    assert "interactive terminal" in output
    assert "must be non-empty" not in output


# --- _diff_against_head, direct calls against a real repo ------------------------------


def test_diff_against_head_returns_the_branchs_changes_since_merge_base(
    repo_with_branch: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, branch = repo_with_branch
    monkeypatch.chdir(repo)

    diff = _diff_against_head(branch)

    assert "+world" in diff


def test_diff_against_head_surfaces_a_bad_ref_as_a_clear_cli_exit(
    repo_with_branch: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _branch = repo_with_branch
    monkeypatch.chdir(repo)

    # `_diff_against_head` is called directly here, not through Typer's CLI runner, so its
    # `typer.Exit` (Click's `Exit`) surfaces as itself rather than the `SystemExit` Typer
    # converts it to when a full command run raises it. The message is this module's own
    # wording, not `git diff`'s raw "ambiguous argument"/path-vs-revision stderr -- a bad
    # BRANCH is caught by a dedicated `git rev-parse --verify` check before the real diff
    # call runs.
    with pytest.raises(typer.Exit):
        _diff_against_head("does-not-exist")

    err = capsys.readouterr().err
    assert "'does-not-exist' is not a valid branch or ref" in err
    assert "ambiguous argument" not in err


def test_diff_against_head_reports_when_git_is_missing(
    repo_with_branch: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, branch = repo_with_branch
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(typer.Exit):
        _diff_against_head(branch)

    assert "git" in capsys.readouterr().err.lower()


# --- _run_pipeline: the diff fetch must not block the TUI's own event loop -------------


def test_run_pipeline_diff_fetch_does_not_block_the_event_loop(
    repo_with_branch: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run_pipeline` (the `events` generator `review` hands `ReviewApp`) fetches the diff
    via `asyncio.to_thread`, not a direct call -- so a slow `_diff_against_head` must not
    stall the event loop `ReviewApp` itself relies on to paint. Proven here by running a
    concurrent ticker alongside a `_diff_against_head` replaced with a one-second
    `time.sleep`: if the diff fetch blocked the loop, the ticker would get zero ticks in
    that second instead of many."""

    repo, branch = repo_with_branch
    monkeypatch.chdir(repo)
    monkeypatch.setattr("code_review.cli._diff_against_head", lambda _branch: _slow_diff())

    tick_count = asyncio.run(_run_pipeline_first_event_while_ticking(branch))

    assert tick_count > 10


def _slow_diff() -> str:
    time.sleep(1.0)
    return "+world\n"


async def _run_pipeline_first_event_while_ticking(branch: str) -> int:
    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    agent = ClaudeCLI()
    relay = InputRelay()
    intent = Intent(summary="add world greeting", source="explicit", score=1.0)
    events = _run_pipeline(branch, intent, agent, relay)

    ticker = asyncio.create_task(_tick())
    await events.__anext__()  # IntentStep's "running" event -- reached only after the diff
    ticker.cancel()
    return ticks


# --- Full run under a real pty, no mocked isatty ----------------------------------------


def _script_argv(args: list[str]) -> list[str]:
    """Build a `script` invocation that runs `args` under a real pty and reports the
    child's own exit code (`-e`), on either the util-linux `script` (Linux CI) or the BSD
    `script` shipped with macOS. The two take the wrapped command differently: util-linux
    wants a single shell string after `-c`, BSD takes the argument vector directly as
    trailing positional args (after the mandatory typescript file) and execs it without a
    shell."""

    script_bin = shutil.which("script")
    assert script_bin is not None, "the 'script' command is required for this test"

    if sys.platform == "darwin":
        return [script_bin, "-qe", "/dev/null", *args]
    return [script_bin, "-qec", shlex.join(args), "/dev/null"]


def _run_in_real_pty(
    args: list[str], cwd: Path, timeout: float = 30.0, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `args` under a real pty via the `script` command, returning its own exit code
    (`-e`) -- the only way to observe `review`'s behavior past the TTY check without faking
    `isatty`. `env` defaults to `None` (inherit this process's own environment) -- only the
    full-run tests below, which need `PATH` to resolve a fake `claude`, pass one explicitly
    (see `_env_with_fake_claude`)."""

    return subprocess.run(
        _script_argv(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _code_review_executable() -> str:
    """The `code-review` console script installed alongside the running interpreter --
    resolved explicitly rather than relying on `PATH` ordering inside a pty subprocess."""

    return str(Path(sys.executable).parent / "code-review")


def _run_review_and_press_e_to_exit(
    args: list[str],
    cwd: Path,
    *,
    wait_before_keypress: float = 4.0,
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Like `_run_in_real_pty`, but for a full `review` run that reaches `ReviewApp`'s
    Status box. The app no longer exits itself once its event stream ends (see
    `tui/app.py`'s module docstring) -- it waits for "e" -- so this waits
    `wait_before_keypress` seconds for the pipeline to finish, then sends "e" to close it.
    A generous margin over the run's own real duration (well under two seconds even with
    all four steps running -- `IntentStep`/`RebaseStep` are pure local `git`, and
    `ReviewStep`/`TestSufficiencyStep` each spawn one fake `claude` process that drains
    stdin and prints immediately), not a tight one, since this is a real subprocess and
    terminal, not a mock. `env` defaults to `None` (inherit this process's own
    environment); the full-run tests below pass `_env_with_fake_claude`'s result so
    `ReviewStep`/`TestSufficiencyStep`'s `ClaudeCLI` calls resolve a fake `claude` on
    `PATH` instead of hanging or failing waiting for a real one."""

    process = subprocess.Popen(
        _script_argv(args),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    time.sleep(wait_before_keypress)
    stdout, stderr = process.communicate(input="e", timeout=timeout)
    assert process.returncode is not None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def test_review_rejects_empty_intent_under_a_real_terminal(
    repo_with_branch: tuple[Path, str],
) -> None:
    repo, branch = repo_with_branch

    result = _run_in_real_pty(
        [_code_review_executable(), "review", branch, "--intent", ""], cwd=repo
    )

    assert result.returncode == 2  # Typer's BadParameter exit code
    assert "must be non-empty" in _plain(result.stdout)


def _assert_no_leftover_code_review_process() -> None:
    """`script`'s own child (the shell that execs `code-review`) is a grandchild of this
    test process, not a direct child -- `communicate()` in the caller above only guarantees
    `script` itself has been reaped, not that init has finished reaping that orphaned
    grandchild too. Poll briefly rather than asserting once immediately after
    `communicate()` returns, so that reaping delay can't turn into a flaky failure."""

    for _ in range(20):
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=True)
        if "code-review review" not in ps.stdout:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("'code-review review' still present in `ps` output")


def test_review_runs_end_to_end_against_a_real_repo_and_exits_cleanly(
    repo_with_branch: tuple[Path, str], tmp_path: Path
) -> None:
    """A real terminal (pty), a real git repo and diff, a real four-step pipeline run
    (`IntentStep` -> `RebaseStep` -> `ReviewStep` -> `TestSufficiencyStep`, real `git`
    subprocesses and a real `ClaudeCLI` subprocess against a fake `claude` on `PATH`)
    through the real executor and `ReviewApp` -- exits with code 0 once "e" is pressed, no
    traceback, every step name rendered as completed in the Pipeline box, the clean-run
    Status message shown, and (checked via `ps` right after `script` returns) no leftover
    `code-review`/textual process. This is acceptance criterion 1 (all four steps run, in
    order) and criterion 4 (demoable end to end) from issue #60."""

    repo, branch = repo_with_branch
    env = _env_with_fake_claude(CLEAN_FAKE_CLAUDE, tmp_path)

    result = _run_review_and_press_e_to_exit(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        env=env,
    )
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    for step_name in ("IntentStep", "RebaseStep", "ReviewStep", "TestSufficiencyStep"):
        assert step_name in output
    assert "Pipeline ran successfully." in output

    _assert_no_leftover_code_review_process()


def test_review_surfaces_a_blocking_finding_without_crashing(
    repo_with_branch: tuple[Path, str], tmp_path: Path
) -> None:
    """Acceptance criterion 3 from issue #60: a blocking ("ask-user") finding from
    `ReviewStep` is surfaced -- rendered in the Findings box -- without the run crashing.
    `run_steps` (`pipeline/executor.py`) never branches on a prior `StepOutcome`, and
    `ReviewApp` only sets `self.error`/exits nonzero on an actually raised exception (see
    `tui/app.py`'s module docstring) -- a blocking-but-non-exception outcome is a normal
    return value, so this still exits 0 and still reaches the "Pipeline ran successfully."
    Status message; no auto-fix/approval loop exists yet (Milestone 7) to make the run stop
    early or wait on the finding interactively."""

    repo, branch = repo_with_branch
    env = _env_with_fake_claude(BLOCKING_FAKE_CLAUDE, tmp_path)

    result = _run_review_and_press_e_to_exit(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        env=env,
    )
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    assert "Pipeline ran successfully." in output
    assert "drops error handling required by the caller's contract" in output

    _assert_no_leftover_code_review_process()
