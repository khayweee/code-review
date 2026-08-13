"""Shared `git`-subprocess plumbing, with no step-orchestration or `Finding`-construction
logic of its own. Knows nothing about `Step`, `StepContext`, `StepOutcome`, or `Finding` --
callers translate raw subprocess results into pipeline types themselves.

`run_git` is non-blocking (spawns via `asyncio.create_subprocess_exec`, awaits
`process.communicate()`) so it doesn't freeze the asyncio event loop during a call. It also
reports itself through the ambient `ActivityReporter` (read via
`current_activity_reporter.get()`, bound per-step by `executor.run_steps`), so each git call
renders as its own timed line in the TUI.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from code_review.pipeline.step import activity_or_nullcontext, current_activity_reporter


def _git_activity_label(args: list[str]) -> str:
    """Render the full command as an activity label, e.g. `["fetch", "origin", "main"]`
    -> `"git fetch origin main"`. Empty `args` degrades to `"git"` rather than raising.
    """

    return f"git {' '.join(args)}".rstrip()


async def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a `git` subprocess in `cwd`, capturing output as text. Never raises on a nonzero
    exit -- callers inspect `.returncode` themselves, since an expected failure (e.g. a
    conflicting rebase) is an ordinary outcome here, not an exceptional one.
    """

    label = _git_activity_label(args)
    async with activity_or_nullcontext(current_activity_reporter.get(), label):
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        # `communicate()` always waits for the process to exit before returning.
        assert process.returncode is not None
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=process.returncode,
            stdout=stdout_bytes.decode("utf-8"),
            stderr=stderr_bytes.decode("utf-8"),
        )


def rebase_in_progress(cwd: Path) -> bool:
    """True if `cwd`'s `.git` shows a paused rebase (`rebase-merge` or `rebase-apply`
    directory exists). The same on-disk signal `git status` uses to print "rebase in
    progress"; distinguishes a genuine conflict from a rebase that never started (e.g. a
    dirty working tree or bad upstream ref).
    """

    git_dir = cwd / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


async def current_branch(cwd: Path) -> str | None:
    """Resolve the currently checked-out branch name in `cwd`, or `None` on failure or a
    detached HEAD (`rev-parse --abbrev-ref HEAD` prints the literal string "HEAD" when
    detached, treated the same as a resolution failure) -- mirrors `ref_sha`'s
    `None`-on-failure convention.
    """

    result = await run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


async def ref_sha(ref: str, cwd: Path) -> str | None:
    """Resolve `ref` to a SHA in `cwd`, or `None` if it does not exist (uses `rev-parse
    --verify --quiet` so a missing ref is a plain result, not an exception).
    """

    result = await run_git(["rev-parse", "--verify", "--quiet", ref], cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


async def is_ancestor(maybe_ancestor: str, descendant: str, cwd: Path) -> bool:
    """True if `maybe_ancestor` is `descendant` itself or an ancestor of it (via `git
    merge-base --is-ancestor`). Both refs must already exist.
    """

    result = await run_git(["merge-base", "--is-ancestor", maybe_ancestor, descendant], cwd)
    return result.returncode == 0


async def conflicted_files(cwd: Path) -> list[str]:
    """Return the sorted list of paths with unresolved merge conflicts. Must be called
    before `git rebase --abort` -- the unmerged state disappears once the abort completes.
    """

    result = await run_git(["diff", "--name-only", "--diff-filter=U"], cwd)
    return sorted(line for line in result.stdout.splitlines() if line)
