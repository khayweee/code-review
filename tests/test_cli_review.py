"""Tests for `code-review review` (Milestone 13, issue #40): the TTY-required fail-fast
path, `_diff_against_head`, and a real end-to-end run.

`CliRunner`'s captured stdio is never a TTY, so it is this file's natural test of the
"needs a real terminal" error path -- no mocking `isatty` (see `cli.py`'s `review`
docstring: the TTY check runs first, before intent validation, so every `CliRunner`
invocation of `review` exercises that path). Reaching the code past the TTY check (intent
validation, and the full pipeline run) instead uses a real pty via the `script` command
against a real throwaway git repo, matching this project's "real subprocess, no mocking the
external tool" testing convention (see tests/pipeline/test_executor.py, tests/test_cli_update.py).
`IntentStep` never calls through `ctx.agent`, so this file's full run needs no real `claude`
CLI on PATH either. The full-run test also has to press "e" itself once the run finishes --
`ReviewApp` no longer exits on its own (see `tui/app.py`'s module docstring) -- so it uses
`_run_review_and_press_e_to_exit` instead of the simpler `_run_in_real_pty` the other
real-pty tests use, since those exit (on a validation error) before the TUI ever starts.
"""

from __future__ import annotations

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

from code_review.cli import _diff_against_head, app

runner = CliRunner()

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI_ESCAPE.sub("", output)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo_with_branch(tmp_path: Path) -> tuple[Path, str]:
    """A real repo on its initial branch, plus a `feature/change` branch one commit ahead
    with a real diff -- `git diff HEAD...feature/change` (this fixture's `HEAD`) sees it."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)

    greeting = repo / "greeting.txt"
    greeting.write_text("hello\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "init"], repo)

    _run_git(["checkout", "-q", "-b", "feature/change"], repo)
    greeting.write_text("hello\nworld\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "add world"], repo)
    _run_git(["checkout", "-q", "-"], repo)

    return repo, "feature/change"


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
    args: list[str], cwd: Path, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    """Run `args` under a real pty via the `script` command, returning its own exit code
    (`-e`) -- the only way to observe `review`'s behavior past the TTY check without faking
    `isatty`."""

    return subprocess.run(
        _script_argv(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _code_review_executable() -> str:
    """The `code-review` console script installed alongside the running interpreter --
    resolved explicitly rather than relying on `PATH` ordering inside a pty subprocess."""

    return str(Path(sys.executable).parent / "code-review")


def _run_review_and_press_e_to_exit(
    args: list[str], cwd: Path, *, wait_before_keypress: float = 2.0, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    """Like `_run_in_real_pty`, but for a full `review` run that reaches `ReviewApp`'s
    Status box. The app no longer exits itself once its event stream ends (see
    `tui/app.py`'s module docstring) -- it waits for "e" -- so this waits
    `wait_before_keypress` seconds for the (today, near-instant single-`IntentStep`)
    pipeline to finish, then sends "e" to close it. A generous margin over the run's own
    real duration (well under a second, observed), not a tight one, since this is a real
    subprocess and terminal, not a mock."""

    process = subprocess.Popen(
        _script_argv(args),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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


def test_review_runs_end_to_end_against_a_real_repo_and_exits_cleanly(
    repo_with_branch: tuple[Path, str],
) -> None:
    """A real terminal (pty), a real git repo and diff, a real `IntentStep` run through the
    real executor and `ReviewApp` -- exits with code 0 once "e" is pressed, no traceback,
    and (checked via `ps` right after `script` returns) no leftover `code-review`/textual
    process."""

    repo, branch = repo_with_branch

    result = _run_review_and_press_e_to_exit(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
    )

    assert result.returncode == 0
    assert "Traceback" not in result.stdout

    # `script`'s own child (the shell that execs `code-review`) is a grandchild of this
    # test process, not a direct child -- `communicate()` above only guarantees `script`
    # itself has been reaped, not that init has finished reaping that orphaned grandchild
    # too. Poll briefly rather than asserting once immediately after `communicate()`
    # returns, so that reaping delay can't turn into a flaky failure.
    for _ in range(20):
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=True)
        if "code-review review" not in ps.stdout:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("'code-review review' still present in `ps` output")
