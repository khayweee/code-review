"""Tests for `code-review review` (Milestone 13, issue #40; four-step pipeline wiring,
issue #60; `PRStep` joined as a fifth step in issue #119): the TTY-required fail-fast path,
`_diff_against_head`, and real end-to-end runs of the full `IntentStep` -> `RebaseStep` ->
`ReviewStep` -> `TestSufficiencyStep` -> `PRStep` pipeline (`steps/registry.py`'s
`IMPLEMENTED_STEPS`). `repo_with_branch` (see its own docstring) leaves the checkout on its
local "main" -- equal to `PRStep`'s own default `default_branch` -- so `PRStep` always
takes its clean-skip path in every full-run test below, with no `gh` call and no fake `gh`
executable needed on `PATH`.

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
superseding the `ApprovalPromptScreen` modal this docstring originally described) over a
real pty. Its per-finding menu was later simplified to drop approve as a reachable option
entirely, with no replacement -- every other outcome now goes through either the inline
chat ("f" jumps to it, typed text plus Enter submits it, always resolving with
`decision="fix"`) or one of two global, non-listed key bindings: "s" (skip) and "x" (abort).
Skip was briefly removed alongside approve, then restored once it became clear chat cannot
resolve every park -- `RebaseStep.run` never reads `ctx.fix_round` at all, so answering its
issue #24 guard with "fix" just re-parks on the identical finding forever; skip is the one
non-abort way past that specific park (see `tui/AGENTS.md`'s "Findings box" section). Once
`ReviewStep`/`TestSufficiencyStep` set `needs_approval` from `has_blocking_finding` (already
shipped, `steps/review.py`/`steps/test_sufficiency.py`), a blocking finding from either step
genuinely parks the run too (it used to be silently ignored, pre-#80) --
`test_review_skipping_both_findings_of_a_two_finding_park_completes_the_run` below is this
file's real-pty proof of that path. The single-finding, non-`RebaseStep` variant of that
same proof (`BLOCKING_FAKE_CLAUDE`/`claude_superset_blocking.py`) was removed once
`tests/tui/test_app.py::test_review_app_parks_with_a_review_output_outcome_without_crashing_on_markup`
started covering the identical scenario -- same finding text, same "s" resolution, same
no-crash assertion -- deterministically via Textual's `Pilot` instead of a real pty,
subprocess, git repo, and fake `claude` executable; the two-finding real-pty test above
still exercises that this parks for real through the actual pipeline wiring.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, NamedTuple

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
BLOCKING_TWO_FINDINGS_FAKE_CLAUDE = _FAKES / "claude_superset_blocking_two_findings.py"


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

    Also sets `LINES`/`COLUMNS`, generously past this pipeline's own real row count --
    Textual's `LinuxDriver._get_terminal_size` (`shutil.get_terminal_size`) checks these
    env vars before it ever queries the `script`-allocated pty's own (often small, e.g.
    24x80 when there is no real controlling terminal) `ioctl` window size, so without them
    a run with enough steps/nested activity rows to exceed that ioctl size never gets to
    compose -- let alone write to this captured byte stream -- its own closing Status box
    ("Pipeline ran successfully."). Raw ANSI bytes have no size limit of their own; only
    Textual's internal layout does.

    Also sets `CODE_REVIEW_STATE_DIR` to a directory under `tmp_path` -- `review` now writes
    a per-run log file under `install_state.state_dir()/runs` (`run_log.py`), so without this
    override a real end-to-end run here would write into the real developer/CI-user's actual
    `~/.code-review/runs` instead of an isolated one.
    """

    bin_dir = tmp_path / "fake_claude_bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(fake_cli.read_text())
    fake_claude.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LINES"] = "200"
    env["COLUMNS"] = "100"
    env["CODE_REVIEW_STATE_DIR"] = str(tmp_path / "state")
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


class _ReviewRun(NamedTuple):
    """A finished real-pty `review` run plus the process-group id its whole subprocess tree
    (`script` -> shell -> `code-review`) shares -- `start_new_session=True` below makes the
    `script` process its own session/group leader, so every descendant inherits that same
    pgid for its entire life, surviving even the reparenting to init that happens once
    `script` itself is reaped (see `_assert_no_leftover_code_review_process`)."""

    result: subprocess.CompletedProcess[str]
    pgid: int


def _drain_in_background(stream: IO[str], chunks: list[str]) -> threading.Thread:
    """Start (and return, already started) a daemon thread appending `stream`'s lines to
    `chunks` as they arrive. `PipelineBox`'s own 4-times-a-second repaint (`_TICK_INTERVAL`)
    writes enough escape-code-heavy output that leaving a real pty child's output pipe
    undrained across a multi-second wait can fill its kernel buffer and block the child's
    own write -- wedging its single-threaded event loop, including the very stdin-read task
    that would otherwise pick up the next keypress. Observed directly while developing
    `_run_review_with_keypresses`: an earlier version that only read output via
    `communicate()` at the very end reproduced this deadlock intermittently (a keypress sent
    during an undrained wait window silently dropped, sometimes, depending on how much had
    already been rendered) -- draining throughout, in both real-pty helpers below, removes
    the dependency on wait length entirely."""

    def _drain() -> None:
        for line in iter(stream.readline, ""):
            chunks.append(line)

    thread = threading.Thread(target=_drain, args=())
    thread.start()
    return thread


def _kill_process_group(pgid: int) -> None:
    """Best-effort `SIGKILL` of the whole process group `start_new_session=True` gave the
    `script` process below (`_ReviewRun.pgid`), used only on a failure path -- a wait
    timing out, or any other exception raised before the process has been confirmed exited.

    Without this, a failed wait leaves the real `code-review review` subprocess running
    indefinitely: nothing else in either real-pty full-run helper ever sends it "e", and
    `PipelineBox`'s live spinner/gradient sweep (`_render_row`) keeps it animating its
    still-"running"/parked step at `ReviewApp`'s own `_TICK_INTERVAL` forever. Confirmed
    directly, not theorized: one such leaked process from an earlier timed-out run was
    still consuming ~38% CPU and writing a measured ~130KB/s of ANSI output minutes later,
    with no test still watching it -- degrading (and in one observed case, tipping over the
    30s/60s exit-wait bound of) every real-pty test that happened to run afterward, on the
    same machine, for the rest of that `pytest` session. `ProcessLookupError` means it's
    already gone by the time this runs; nothing else here is worth raising over a
    best-effort cleanup on top of an already-failing test.
    """

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class _LiveOutput:
    """Live-tails a real pty child's stdout, built once per real-pty helper below and
    shared by every wait against that one process's output.

    Keeps only the last `_TAIL_SIZE` characters drained so far, not the whole growing
    history: re-scanning everything drained-since-the-start on every ~20ms poll tick (this
    file's first version of this class) is an O(n^2) CPU cost that, at `PipelineBox`'s
    repaint volume (`_TICK_INTERVAL`, 1000+ drained lines over a few parked seconds), was
    itself enough to starve the real subprocesses these waits exist to observe once several
    of these tests ran concurrently under pytest-xdist (`-n auto`) -- reproduced directly,
    not theorized: a dozen test processes each repeatedly re-joining a
    thousands-of-lines-and-growing buffer 50 times a second pushed a real `code-review
    review` subprocess past its own 30-second `process.wait` timeout. `_TAIL_SIZE` only
    needs to comfortably outlast one full-screen repaint (a few KB, empirically), not the
    whole run.
    """

    _TAIL_SIZE = 16384

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self._read_upto = 0
        self._tail = ""

    def _absorb_new(self) -> None:
        new = self._chunks[self._read_upto :]
        if new:
            self._read_upto = len(self._chunks)
            self._tail = (self._tail + "".join(new))[-self._TAIL_SIZE :]

    def current(self) -> str:
        """The last `_TAIL_SIZE` characters drained so far, absorbing whatever's newly
        arrived first."""

        self._absorb_new()
        return self._tail

    def wait_until(self, predicate: Callable[[str], bool], timeout: float) -> bool:
        """Poll `self.current()` against `predicate` until it returns `True` or `timeout`
        elapses, returning whether it ever matched. `timeout` is a real, generous safety
        margin callers size against the run's own worst-case duration -- an upper bound,
        not an unconditional wait -- so this returns fast on the normal path (`predicate`
        is driven by a real subprocess call or a real `git` operation finishing, not a
        fixed duration) and degrades to `time.sleep(timeout)` when `predicate` never
        matches (e.g. `_PARK_MARKER` while sending "f", which mounts the chat `Input`
        without changing the Findings box's own border title -- see
        `_run_review_with_keypresses`'s docstring)."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.current()):
                return True
            time.sleep(0.02)
        return predicate(self.current())


_DONE_MARKER = "Press 'e' to exit."  # `state.final_status_message`'s own closing line
_PARK_MARKER = "Findings --"  # `FindingsList.border_title`'s own fixed prefix
# `FindingsList._set_footer_hint`'s own live progress readout, e.g. "1/2 decided".
_DECIDED_RE = re.compile(r"(\d+)/(\d+) decided")


def _latest_decided_progress(text: str) -> tuple[int, int] | None:
    matches = _DECIDED_RE.findall(text)
    return (int(matches[-1][0]), int(matches[-1][1])) if matches else None


def _send_key(process: subprocess.Popen[str], key: str) -> None:
    assert process.stdin is not None
    process.stdin.write(key)
    process.stdin.flush()


def _send_key_confirmed(
    process: subprocess.Popen[str], output: _LiveOutput, key: str, timeout: float
) -> None:
    """Write `key` to the parked app's stdin and confirm it actually landed, retrying
    once (by resending the identical `key`) if not.

    Confirmation reads `FindingsList`'s own live "N/M decided" progress counter
    (`_latest_decided_progress`) -- the only signal that distinguishes "this specific
    decision was recorded" from "a park is still showing". `_PARK_MARKER` alone can't:
    `FindingsList`'s border title re-renders identically on every ~250ms repaint tick
    regardless of whether anything changed, so waiting for it to merely reappear after a
    keypress (this file's first version of this helper) returns almost immediately whether
    or not that keypress actually arrived. "Progress" here is either the decided count
    changing (including to/from `None`, covering a decision that resolves the whole park
    -- the box unmounts, then either a new park's own fresh count or nothing appears until
    `_DONE_MARKER` does) or `_DONE_MARKER` itself showing up.

    Resending is safe here specifically because every keypress this file actually sends is
    idempotent against "already applied": "s" on an already-open park advances to skip the
    *next* undecided row (never re-decides the one just skipped), "x" aborts
    unconditionally, and "e" only matters once `_DONE_MARKER` is showing, so a duplicate
    lands on a screen with nothing left listening for it.

    This exists because a real pty relayed through the `script` command has been observed,
    under pytest-xdist (`-n auto`) running many of these tests concurrently, to
    occasionally drop a byte written to its own stdin -- confirmed directly via Python's
    `faulthandler`, not theorized: the real `code-review review` process was found
    completely idle (every thread parked in a blocking read/select, nothing spinning or
    holding a lock) waiting for a keystroke that plainly never reached its terminal
    driver.

    If `output.current()` shows no counter at all right now (`baseline is None`) -- the
    gap between one park fully resolving and the next one opening, or before the very
    first park exists yet -- this waits for a real counter to actually render before
    treating it as the baseline, rather than using `None` itself. Skipping that wait was a
    real, confirmed bug (not a theoretical one): the very next thing that renders once a
    park resolves is often the *next* park's own fresh "0/N decided" counter, appearing on
    its own regardless of whether this key was ever received -- `_progressed`'s `!=
    baseline` check treats `None -> "0/N"` exactly like "my key decided a row", since
    both are simply "the reading changed". A key sent into that gap was, on a real wedged
    run, silently credited as delivered by that unrelated park-open event alone -- caught
    directly via a live dump of the running app's own state: `TestSufficiencyStep`'s park
    had decided its first row but never even received the keypress meant for its second,
    permanently parked with no keypress left in the sequence to answer it. Waiting for a
    real counter first, before capturing the baseline this key's own delivery is judged
    against, removes that ambiguity."""

    if _latest_decided_progress(output.current()) is None:
        output.wait_until(lambda text: _latest_decided_progress(text) is not None, timeout)
    baseline = _latest_decided_progress(output.current())

    def _progressed(text: str) -> bool:
        return _DONE_MARKER in text or _latest_decided_progress(text) != baseline

    _send_key(process, key)
    if output.wait_until(_progressed, timeout):
        return
    _send_key(process, key)
    output.wait_until(_progressed, timeout)


def _send_e_and_confirm_exit(process: subprocess.Popen[str], timeout: float) -> None:
    """Send "e" and confirm the process actually starts exiting, resending once if not --
    the same dropped-keystroke safety net `_send_key_confirmed` documents, specialized for
    the closing "e", which has no `_PARK_MARKER`/"N/M decided"-style content signal of its
    own to poll (the app just exits; there's nothing left to render)."""

    _send_key(process, "e")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.02)
    if process.poll() is None:
        _send_key(process, "e")


def _run_review_and_press_e_to_exit(
    args: list[str],
    cwd: Path,
    *,
    wait_before_keypress: float = 4.0,
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
) -> _ReviewRun:
    """Like `_run_in_real_pty`, but for a full `review` run that reaches `ReviewApp`'s
    Status box. The app no longer exits itself once its event stream ends (see
    `tui/app.py`'s module docstring) -- it waits for "e" -- so this waits up to
    `wait_before_keypress` seconds for `_DONE_MARKER` to appear (see `_LiveOutput.
    wait_until`), then sends "e" to close it, resending once if the process doesn't
    actually start exiting (see `_send_e_and_confirm_exit`). `wait_before_keypress`'s
    default is a generous upper bound over the run's own real duration (well under two
    seconds even with all five steps running -- `IntentStep`/`RebaseStep`/`PRStep` are pure
    local `git` (`PRStep` takes its clean-skip path here, see this module's own docstring),
    and `ReviewStep`/`TestSufficiencyStep` each spawn one fake `claude` process that
    drains stdin and prints immediately), not a tight one, since this is a real subprocess
    and terminal, not a mock -- but the actual wait is normally a small fraction of it.
    `env` defaults to `None` (inherit this process's own environment); the full-run tests
    below pass `_env_with_fake_claude`'s result so `ReviewStep`/`TestSufficiencyStep`'s
    `ClaudeCLI` calls resolve a fake `claude` on `PATH` instead of hanging or failing
    waiting for a real one."""

    process = subprocess.Popen(
        _script_argv(args),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    pgid = process.pid
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = _drain_in_background(process.stdout, stdout_chunks)
    stderr_thread = _drain_in_background(process.stderr, stderr_chunks)
    output = _LiveOutput(stdout_chunks)

    try:
        output.wait_until(lambda text: _DONE_MARKER in text, timeout=wait_before_keypress)
        _send_e_and_confirm_exit(process, timeout=1.0)
        process.stdin.close()
        process.wait(timeout=timeout)
    except BaseException:
        # See `_kill_process_group`'s own docstring: a wait that raises here (most often
        # `process.wait`'s own `TimeoutExpired`) must not leave `process` running.
        _kill_process_group(pgid)
        raise
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
    return _ReviewRun(
        subprocess.CompletedProcess(
            process.args, process.returncode, "".join(stdout_chunks), "".join(stderr_chunks)
        ),
        pgid,
    )


def _run_review_with_keypresses(
    args: list[str],
    cwd: Path,
    *,
    keypresses: list[tuple[float, str]],
    final_wait: float = 3.0,
    timeout: float = 60.0,
    env: dict[str, str] | None = None,
) -> _ReviewRun:
    """Like `_run_review_and_press_e_to_exit`, generalized to send an ordered sequence of
    `(max_wait_seconds, key)` pairs -- issue #80's approval park needs at least one keypress
    answered on the parked `FindingsBox` *before* the run reaches its own Status box, unlike
    every prior full-run test here, which only ever needs the final "e". The first keypress
    waits up to its own `max_wait_seconds` for `_PARK_MARKER` (see `_LiveOutput.wait_until`)
    -- a park has to exist before anything can be sent to it -- then every keypress,
    including that first one, is written and confirmed via `_send_key_confirmed`, each
    against its own `max_wait_seconds` budget. `key` need not be a single character -- e.g.
    resolving via the inline chat's `Input` writes the typed text plus a trailing `"\r"`
    (Enter, submitting it) as one `key` string. It must NOT also include the "f" that opens
    the chat in that same string, though: "f" only mounts and focuses the chat's `Input`
    asynchronously, so text written in the very same `stdin.write()` call can arrive (and be
    silently dropped, with nothing yet focused to receive it) before that mount has actually
    happened -- confirmed empirically against this Textual version, not just reasoned about.
    "f" and the typed text must be two separate tuples, e.g. `(3.0, "f")` then `(0.5, "looks
    good\r")` -- `_send_key_confirmed` has no content signal for "f" specifically (opening
    the chat doesn't change `FindingsList`'s "N/M decided" counter), so it degrades to
    sending it, sleeping the full `max_wait_seconds`, then resending once more and sleeping
    again, before the second tuple's own wait covers the mount. `final_wait` is the same
    kind of upper bound, now against `_DONE_MARKER`, before the closing "e".

    `timeout` (bounding the final `process.wait` once "e" has been sent) defaults higher
    than `_run_review_and_press_e_to_exit`'s: `PipelineBox`'s live spinner and per-frame
    gradient sweep (`_render_row`) keep any still-"running"/parked step animating on
    `ReviewApp`'s own `_TICK_INTERVAL` (4Hz) for as long as a park is showing, which
    measured in the ~130KB/s steady state over a real pty even while idle, parked, and
    waiting on a keypress -- confirmed independent of terminal size, so not `LINES`/
    `COLUMNS`-driven. A caller that parks twice (this helper's whole reason to exist over
    the single-park helper) pushes twice the animated-frame volume through the same real
    `script`-relayed pty before `process.wait` ever starts counting, which is exactly the
    "dozen real subprocesses started, output-drain thread starved of CPU" class already
    documented on `_LiveOutput`/`_drain_in_background` above -- reproduced directly against
    this exact helper under load, not theorized. 60s (vs. 30s) buys headroom against that
    same contention for the multi-park case without masking a real hang: a genuinely wedged
    process (e.g. a dropped stdin byte with no keystroke left to retry) would still fail
    this bound, just later.
    """

    process = subprocess.Popen(
        _script_argv(args),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    pgid = process.pid
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = _drain_in_background(process.stdout, stdout_chunks)
    stderr_thread = _drain_in_background(process.stderr, stderr_chunks)
    output = _LiveOutput(stdout_chunks)

    try:
        first_wait, first_key = keypresses[0]
        output.wait_until(lambda text: _PARK_MARKER in text, timeout=first_wait)
        _send_key_confirmed(process, output, first_key, timeout=first_wait)
        for max_wait, key in keypresses[1:]:
            _send_key_confirmed(process, output, key, timeout=max_wait)

        output.wait_until(lambda text: _DONE_MARKER in text, timeout=final_wait)
        _send_e_and_confirm_exit(process, timeout=1.0)
        process.stdin.close()
        process.wait(timeout=timeout)
    except BaseException:
        # See `_kill_process_group`'s own docstring: a wait that raises here (most often
        # `process.wait`'s own `TimeoutExpired`) must not leave `process` running.
        _kill_process_group(pgid)
        raise
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    return _ReviewRun(
        subprocess.CompletedProcess(
            process.args, process.returncode, "".join(stdout_chunks), "".join(stderr_chunks)
        ),
        pgid,
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


def _assert_no_leftover_code_review_process(pgid: int) -> None:
    """`script`'s own child (the shell that execs `code-review`) is a grandchild of this
    test process, not a direct child -- `communicate()`/`process.wait()` in the callers
    above only guarantee `script` itself has been reaped, not that init has finished
    reaping that orphaned grandchild too. Poll briefly rather than asserting once
    immediately after, so that reaping delay can't turn into a flaky failure.

    Scoped to `pgid` (not a bare machine-wide `ps` scan) so this stays correct when other
    tests in this same file run concurrently under pytest-xdist, each with its own
    `code-review review` subprocess alive at the same time: `start_new_session=True` in
    both helpers above makes `script` a session/process-group leader, and every descendant
    it spawns inherits that same pgid for its whole life -- reparenting to init on `script`
    exiting changes ppid, never pgid, so this still finds an orphaned grandchild the plain
    parent-child walk above can't."""

    for _ in range(20):
        ps = subprocess.run(["ps", "-eo", "pgid,args"], capture_output=True, text=True, check=True)
        rows = ps.stdout.splitlines()[1:]
        leftover = [
            row
            for row in rows
            if row.split(maxsplit=1)[:1] == [str(pgid)] and "code-review review" in row
        ]
        if not leftover:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(
            f"'code-review review' still present in process group {pgid}'s own `ps` output"
        )


# Every test below spawns a real ReviewApp over a real pty and therefore inherits
# PipelineBox's live spinner/gradient animation for as long as any step is "running"/parked
# (see pipeline_box.py's `_SHIMMER_TICK_SECONDS`) -- real, sustained CPU and pty-output load
# for the whole test, not a brief spike. Under plain `-n auto` (`--dist=load`, pytest-xdist's
# default), pytest-xdist is free to schedule several of these onto different workers at the
# same time, so their animations compete for the CI runner's own small core count -- this is
# what was actually pushing `_run_review_with_keypresses`'s exit-wait past even a 60s bound
# in CI, not any single test being slow on its own (each passes in a couple of seconds in
# isolation). `xdist_group` (used with `--dist=loadgroup` -- see `.github/workflows/ci.yml`)
# pins every test carrying this marker to the same worker, so at most one of them ever
# animates at once; the rest of the suite still spreads across every other worker exactly as
# before. A plain `-n auto` run (e.g. this file's own local `uv run pytest -n auto` without
# the extra flag) ignores the marker and schedules these exactly as it did previously.
_REAL_PTY_FULL_RUN_GROUP = "real_pty_full_run"


@pytest.mark.xdist_group(name=_REAL_PTY_FULL_RUN_GROUP)
def test_review_runs_end_to_end_against_a_real_repo_and_exits_cleanly(
    repo_with_branch: tuple[Path, str], tmp_path: Path
) -> None:
    """A real terminal (pty), a real git repo and diff, a real five-step pipeline run
    (`IntentStep` -> `RebaseStep` -> `ReviewStep` -> `TestSufficiencyStep` -> `PRStep`, real
    `git` subprocesses and a real `ClaudeCLI` subprocess against a fake `claude` on `PATH`;
    `PRStep` itself takes its clean-skip path here, see this module's own docstring)
    through the real executor and `ReviewApp` -- exits with code 0 once "e" is pressed, no
    traceback, every step name rendered as completed in the Pipeline box, the clean-run
    Status message shown, and (checked via `ps` right after `script` returns) no leftover
    `code-review`/textual process. This is acceptance criterion 1 (all four steps from
    issue #60 run, in order -- `PRStep` joined later, issue #119) and criterion 4 (demoable
    end to end) from issue #60.

    Also covers `run_log.py`'s wiring end to end: `review` writes a per-run log file under
    `CODE_REVIEW_STATE_DIR/runs` (isolated to `tmp_path` by `_env_with_fake_claude`) and
    echoes its path -- see `tests/test_run_log.py` for `run_log.py`'s own unit tests."""

    repo, branch = repo_with_branch
    env = _env_with_fake_claude(CLEAN_FAKE_CLAUDE, tmp_path)

    run = _run_review_and_press_e_to_exit(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        env=env,
    )
    result = run.result
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    for step_name in ("Intent", "Rebase", "Review", "Test Sufficiency", "Pull Request"):
        assert step_name in output
    assert "Pipeline ran successfully." in output

    assert "Run log written to" in output
    run_logs = list((tmp_path / "state" / "runs").glob("*.log"))
    assert len(run_logs) == 1
    log_text = run_logs[0].read_text()
    assert "Pipeline ran successfully." in log_text
    for step_name in ("IntentStep", "RebaseStep", "ReviewStep", "TestSufficiencyStep", "PRStep"):
        assert step_name in log_text

    _assert_no_leftover_code_review_process(run.pgid)


@pytest.mark.xdist_group(name=_REAL_PTY_FULL_RUN_GROUP)
def test_review_skipping_both_findings_of_a_two_finding_park_completes_the_run(
    repo_with_branch: tuple[Path, str], tmp_path: Path
) -> None:
    """Repro/regression test for issue #98 (per-finding parking): unlike the single-finding
    park above, `BLOCKING_TWO_FINDINGS_FAKE_CLAUDE` returns two "ask-user" findings per
    step, so `ReviewStep`'s park has a real multi-row `FindingsList` -- the case #98's
    per-finding decision model exists for. Both rows must be answered with "s" before
    `ReviewStep`'s park actually resolves (issue #98's own new behavior), and the same
    again for `TestSufficiencyStep`'s park right after.

    This is the one test in this file whose 3rd keypress lands in the gap between one
    park fully resolving and the next one opening -- exactly the case
    `_send_key_confirmed`'s own docstring now covers (a real, confirmed bug: that gap's
    `baseline is None` used to look identical to "my keypress was received", so a
    keypress sent into it was sometimes silently credited to the *next* park merely
    opening, not to anything it actually did -- leaving `TestSufficiencyStep`'s second row
    permanently undecided with no keypress left in the sequence to answer it). Every
    `max_wait_seconds` budget below is still generous (matching every other keypress in
    this file, none below 3.0) and `final_wait` is passed well above
    `_run_review_with_keypresses`'s own 3.0s default: this is the only caller that needs
    two full approval round-trips *and* `PRStep` to all finish inside that one window
    before "e" is ever sent, and `stdin` closes for good right after -- no keypress sent
    after that point can ever reach the app, even once it finishes moments later."""

    repo, branch = repo_with_branch
    env = _env_with_fake_claude(BLOCKING_TWO_FINDINGS_FAKE_CLAUDE, tmp_path)

    run = _run_review_with_keypresses(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        keypresses=[(4.0, "s"), (4.0, "s"), (4.0, "s"), (4.0, "s")],
        final_wait=10.0,
        env=env,
    )
    result = run.result
    output = _plain(result.stdout)

    assert "Traceback" not in output
    assert result.returncode == 0
    assert "Pipeline ran successfully." in output

    _assert_no_leftover_code_review_process(run.pgid)


# --- The approval park, proven against RebaseStep's real, already-shipped guard (#80) ----


@pytest.mark.xdist_group(name=_REAL_PTY_FULL_RUN_GROUP)
def test_review_parks_at_rebase_step_on_unpushed_local_default_commits(
    repo_with_unpushed_local_default_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    """This is the ticket's own headline acceptance criterion: a branch whose history
    includes unpushed local-default commits parks at `RebaseStep` and presents the inline
    chat/skip/abort decision through the TUI, instead of silently rebasing as it did before
    #80 (approve is no longer reachable at all, a later simplification -- "chat", the
    global "s" skip, and the global "x" abort are what's left). Aborting proves this
    without needing a fake `claude` on `PATH` -- `ReviewStep`/`TestSufficiencyStep` never
    run; see `test_review_choosing_skip_at_the_rebase_park_records_it_skipped_and_continues`
    below for the non-abort path past this same guard."""

    repo, branch, unpushed_sha = repo_with_unpushed_local_default_commits

    # No fake claude needed here (see docstring), but `review` still writes a per-run log
    # file (`run_log.py`) before the park, so this still needs an isolated
    # CODE_REVIEW_STATE_DIR -- see `_env_with_fake_claude`'s own docstring.
    env = dict(os.environ)
    env["CODE_REVIEW_STATE_DIR"] = str(tmp_path / "state")

    run = _run_review_with_keypresses(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        keypresses=[(3.0, "x")],
        env=env,
    )
    result = run.result
    output = _plain(result.stdout)

    assert result.returncode == 1
    assert "Traceback" not in output
    assert "code-review review failed" in output
    assert "RebaseStep" in output
    # The finding this guard produces names the unpushed commit -- proof this is really
    # `steps/rebase.py`'s issue #24 guard firing, not some other park.
    assert unpushed_sha[:7] in output
    # No further step ran: aborting stopped the run before ReviewStep (rendered "Review")
    # ever started.
    assert "◌ Review" in output

    _assert_no_leftover_code_review_process(run.pgid)


@pytest.mark.xdist_group(name=_REAL_PTY_FULL_RUN_GROUP)
def test_review_choosing_skip_at_the_rebase_park_records_it_skipped_and_continues(
    repo_with_unpushed_local_default_commits: tuple[Path, str, str],
    tmp_path: Path,
) -> None:
    """Skip is not an error: later steps still run, and the run still finishes cleanly.

    Approve is no longer a reachable outcome from this UI at all (removed for good, with no
    replacement), and chat can't stand in for it here either: `RebaseStep.run` never reads
    `ctx.fix_round`, so a "fix" response just re-parks on the identical finding forever (see
    `tui/AGENTS.md`'s "Findings box" section) -- "s" (skip), restored as a bare global escape
    hatch alongside "x" (abort) for exactly this kind of park, is the only way past this
    specific guard short of aborting the whole run."""

    repo, branch, _unpushed_sha = repo_with_unpushed_local_default_commits
    env = _env_with_fake_claude(CLEAN_FAKE_CLAUDE, tmp_path)

    run = _run_review_with_keypresses(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        keypresses=[(3.0, "s")],
        env=env,
    )
    result = run.result
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    for step_name in ("Intent", "Rebase", "Review", "Test Sufficiency", "Pull Request"):
        assert step_name in output
    assert "Pipeline ran successfully." in output

    _assert_no_leftover_code_review_process(run.pgid)


@pytest.mark.xdist_group(name=_REAL_PTY_FULL_RUN_GROUP)
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

    run = _run_review_and_press_e_to_exit(
        [_code_review_executable(), "review", branch, "--intent", "add world greeting"],
        cwd=repo,
        wait_before_keypress=5.0,  # one extra fake-claude call over the other full-run tests
        env=env,
    )
    result = run.result
    output = _plain(result.stdout)

    assert result.returncode == 0
    assert "Traceback" not in output
    for step_name in ("Intent", "Rebase", "Review", "Test Sufficiency", "Pull Request"):
        assert step_name in output
    assert "Pipeline ran successfully." in output

    _assert_no_leftover_code_review_process(run.pgid)
