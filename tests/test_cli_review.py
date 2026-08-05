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

Issue #80's approval park adds a real end-to-end proof against an already-shipped bug:
`repo_with_unpushed_local_default_commits` builds a checkout where `RebaseStep`'s issue #24
guard fires for real, and `_run_review_with_keypresses` (generalizing
`_run_review_and_press_e_to_exit` to an ordered sequence of keypresses, not just the final
"e") answers the resulting parked `FindingsBox` (issue #87's inline decision selector,
superseding the `ApprovalPromptScreen` modal this docstring originally described) with
"a"/"s"/"x" over a real pty. Once
`ReviewStep`/`TestSufficiencyStep` set `needs_approval` from `has_blocking_finding` (already
shipped, `steps/review.py`/`steps/test_sufficiency.py`), `test_review_surfaces_a_blocking_
finding_without_crashing` below changed too: a blocking finding now genuinely parks the run
(it used to be silently ignored, pre-#80) -- `BLOCKING_FAKE_CLAUDE`'s own module docstring
already anticipated this ("both steps report needs_approval=True from this one script").
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

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
AUTO_FIX_ROUND_FAKE_CLAUDE = _FAKES / "claude_superset_auto_fix_then_clean.py"
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


@pytest.fixture
def repo_with_unpushed_local_default_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """Like `repo_with_branch` above, but real, end-to-end proof for issue #80: `repo`'s
    checked-out HEAD (local "main", exactly as `repo_with_branch` leaves it) gains one
    commit never pushed to `origin` -- so local "main"'s tip and HEAD are literally the
    same commit, which trivially satisfies `steps/rebase.py`'s issue #24 guard's second
    condition ("local `default_branch`'s tip is itself an ancestor of HEAD") by identity,
    while the first condition (local `default_branch` strictly ahead of `origin/main`)
    holds because that commit was never pushed. `RebaseStep`'s own guard fires the moment
    `review` reaches it, parking the whole run -- this is the already-shipped bug (issue
    #24) this ticket (#80) is proven against: before #80, the executor silently ignored
    `needs_approval=True` and rebased anyway.

    Returns `(repo, branch, unpushed_sha)`: `branch` is `repo_with_branch`'s own
    "feature/change" (used only for `_diff_against_head`'s diff, unrelated to the guard);
    `unpushed_sha` is the local-only commit's full SHA, whose short form both `RebaseStep`'s
    resulting `Finding.description` and the parked `FindingsBox`'s own displayed text name.
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
    _run_git(["checkout", "-q", "main"], repo)

    (repo / "local_main_only.txt").write_text("unpushed\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "add local-main-only file"], repo)
    unpushed_sha = _run_git(["rev-parse", "HEAD"], repo).stdout.strip()

    return repo, "feature/change", unpushed_sha


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


def _run_review_with_keypresses(
    args: list[str],
    cwd: Path,
    *,
    keypresses: list[tuple[float, str]],
    final_wait: float = 3.0,
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Like `_run_review_and_press_e_to_exit`, generalized to send an ordered sequence of
    `(delay_seconds, key)` pairs -- issue #80's approval park needs a single keypress
    ("a"/"s"/"x") answered on the parked `FindingsBox` *before* the run reaches its own
    Status box, unlike
    every prior full-run test here, which only ever needs the final "e". Each `key` is
    written directly to the child's stdin after sleeping `delay_seconds` -- the same
    "generous margin over the run's own real duration, not a tight one" reasoning
    `_run_review_and_press_e_to_exit` already documents. `final_wait` is the margin before
    the closing "e".

    Unlike `_run_review_and_press_e_to_exit` (a single `time.sleep` immediately followed by
    `communicate()`), this drains `stdout`/`stderr` continuously on background threads for
    the whole run, not just after the last keypress: `PipelineBox`'s own 4-times-a-second
    repaint (`_TICK_INTERVAL`) writes enough escape-code-heavy output that leaving the
    child's pty output pipe undrained across *multiple* waits (several seconds each, one
    per keypress plus `final_wait`) can fill its kernel buffer and block the child's own
    write -- wedging its single-threaded event loop, including the very stdin-read task
    that would otherwise pick up the next keypress. Observed directly while developing this
    helper: an earlier version that only read output via `communicate()` at the very end
    reproduced this deadlock intermittently (a keypress sent during an undrained wait
    windows to be silently dropped, sometimes, depending on how much had already been
    rendered) -- draining throughout removes the dependency on wait length entirely.
    """

    process = subprocess.Popen(
        _script_argv(args),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _drain(stream: IO[str], chunks: list[str]) -> None:
        for line in iter(stream.readline, ""):
            chunks.append(line)

    stdout_thread = threading.Thread(target=_drain, args=(process.stdout, stdout_chunks))
    stderr_thread = threading.Thread(target=_drain, args=(process.stderr, stderr_chunks))
    stdout_thread.start()
    stderr_thread.start()

    for delay, key in keypresses:
        time.sleep(delay)
        process.stdin.write(key)
        process.stdin.flush()
    time.sleep(final_wait)
    process.stdin.write("e")
    process.stdin.close()

    process.wait(timeout=timeout)
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    return subprocess.CompletedProcess(
        process.args, process.returncode, "".join(stdout_chunks), "".join(stderr_chunks)
    )


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


def test_review_surfaces_a_blocking_finding_and_approving_both_parks_completes_the_run(
    repo_with_branch: tuple[Path, str], tmp_path: Path
) -> None:
    """Acceptance criterion 3 from issue #60, updated for issue #80: a blocking ("ask-user")
    finding from `ReviewStep` is surfaced -- rendered in the Findings box -- without the run
    crashing. Before #80 this outcome's `needs_approval=True` was silently ignored; now it
    genuinely parks the run (`BLOCKING_FAKE_CLAUDE`'s own module docstring already
    anticipated this: "both steps report needs_approval=True from this one script", since
    `TestSufficiencyStep` resolves the same fake `claude` on `PATH` and parks a second time
    right after). Answering both parks with "approve" ("a") lets the run reach its own
    "Pipeline ran successfully." Status message, proving a blocking finding still does not
    crash the run -- it now genuinely pauses for a human instead of being silently
    ignored."""

    repo, branch = repo_with_branch
    env = _env_with_fake_claude(BLOCKING_FAKE_CLAUDE, tmp_path)

    result = _run_review_with_keypresses(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        keypresses=[(3.0, "a"), (2.0, "a")],
        env=env,
    )
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    assert "Pipeline ran successfully." in output
    assert "drops error handling required by the caller's contract" in output

    _assert_no_leftover_code_review_process()


# --- The approval park, proven against RebaseStep's real, already-shipped guard (#80) ----


def test_review_parks_at_rebase_step_on_unpushed_local_default_commits(
    repo_with_unpushed_local_default_commits: tuple[Path, str, str],
) -> None:
    """This is the ticket's own headline acceptance criterion: a branch whose history
    includes unpushed local-default commits parks at `RebaseStep` and presents approve/
    skip/abort through the TUI, instead of silently rebasing as it did before #80. Aborting
    proves this without needing a fake `claude` on `PATH` -- `ReviewStep`/
    `TestSufficiencyStep` never run."""

    repo, branch, unpushed_sha = repo_with_unpushed_local_default_commits

    result = _run_review_with_keypresses(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        keypresses=[(3.0, "x")],
    )
    output = _plain(result.stdout)

    assert result.returncode == 1
    assert "Traceback" not in output
    assert "code-review review failed" in output
    assert "RebaseStep" in output
    # The finding this guard produces names the unpushed commit -- proof this is really
    # `steps/rebase.py`'s issue #24 guard firing, not some other park.
    assert unpushed_sha[:7] in output
    # No further step ran: aborting stopped the run before ReviewStep ever started.
    assert "ReviewStep" not in output or "◌ ReviewStep" in output

    _assert_no_leftover_code_review_process()


def test_review_choosing_approve_at_the_rebase_park_continues_the_run(
    repo_with_unpushed_local_default_commits: tuple[Path, str, str],
    tmp_path: Path,
) -> None:
    repo, branch, _unpushed_sha = repo_with_unpushed_local_default_commits
    env = _env_with_fake_claude(CLEAN_FAKE_CLAUDE, tmp_path)

    result = _run_review_with_keypresses(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        keypresses=[(3.0, "a")],
        env=env,
    )
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    for step_name in ("IntentStep", "RebaseStep", "ReviewStep", "TestSufficiencyStep"):
        assert step_name in output
    assert "Pipeline ran successfully." in output

    _assert_no_leftover_code_review_process()


def test_review_reaches_success_via_reviewsteps_automatic_fix_round_with_no_park(
    repo_with_branch: tuple[Path, str], tmp_path: Path
) -> None:
    """Real end-to-end proof of issue #81's automatic path, through the real `code-review
    review` command: `ReviewStep`'s first round returns one auto-fix finding and no
    ask-user finding (`auto_fixable=True`, `needs_approval=False`), which `pipeline/
    executor.py`'s round loop re-runs automatically -- no park, no keypress, no human
    interaction of any kind -- before `TestSufficiencyStep` ever runs. `AUTO_FIX_ROUND_
    FAKE_CLAUDE` answers clean on every call after its first (both `ReviewStep`'s own
    automatic fix round and `TestSufficiencyStep`'s later, separate call resolve this same
    fake `claude` on `PATH`), so this run reaches "Pipeline ran successfully." with only
    the closing "e" ever sent -- if the automatic round instead parked (the regression this
    test guards against), no approval prompt would ever be answered and this would time out
    instead of exiting cleanly."""

    repo, branch = repo_with_branch
    env = _env_with_fake_claude(AUTO_FIX_ROUND_FAKE_CLAUDE, tmp_path)

    result = _run_review_and_press_e_to_exit(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        wait_before_keypress=5.0,  # one extra fake-claude call over the other full-run tests
        env=env,
    )
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    for step_name in ("IntentStep", "RebaseStep", "ReviewStep", "TestSufficiencyStep"):
        assert step_name in output
    assert "Pipeline ran successfully." in output

    _assert_no_leftover_code_review_process()


def test_review_choosing_skip_at_the_rebase_park_records_it_skipped_and_continues(
    repo_with_unpushed_local_default_commits: tuple[Path, str, str],
    tmp_path: Path,
) -> None:
    """Skip is not an error: later steps still run, and the run still finishes cleanly."""

    repo, branch, _unpushed_sha = repo_with_unpushed_local_default_commits
    env = _env_with_fake_claude(CLEAN_FAKE_CLAUDE, tmp_path)

    result = _run_review_with_keypresses(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        keypresses=[(3.0, "s")],
        env=env,
    )
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    for step_name in ("IntentStep", "RebaseStep", "ReviewStep", "TestSufficiencyStep"):
        assert step_name in output
    assert "Pipeline ran successfully." in output

    _assert_no_leftover_code_review_process()
