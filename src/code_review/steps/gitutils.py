"""Shared `git`-subprocess plumbing -- generic `git` operations with no step-orchestration
or `Finding`-construction logic of their own.

Extracted out of `steps/rebase.py` (Milestone 4, issues #23/#24): a staff-engineer audit
noted that `_run_git`, `_rebase_in_progress`, `_ref_sha`, `_is_ancestor`, and
`_conflicted_files` were pure git-subprocess primitives with no knowledge of `RebaseStep`,
`StepOutcome`, or `Finding`, while `steps/rebase.py` itself kept only the guard/orchestration
logic that decides *when* those primitives' results become a blocking `Finding`
(`_unpushed_local_default_finding` and `RebaseStep.run` stayed put for exactly that reason).
Moved here, unprefixed, rather than left rebase-step-private, so the next step that needs
the same kind of git subprocess call doesn't have to reach into `steps/rebase.py` or grow
its own duplicate copy -- matching `steps/intent.py`'s `wrap_intent`/`redact_secrets`/
`strip_adversarial` convention of a step-package module without a leading underscore
signalling "safe for a sibling step module to import".

Current consumer: `steps/rebase.py`'s `RebaseStep`. Anticipated future consumer:
`steps/pr.py`'s PR step (Milestone 8, currently a docstring-only stub), whose own docstring
already previews needing `git diff --name-status` for its deterministic fallback body --
the same kind of generic git subprocess call this module wraps.

This module knows nothing about `Step`, `StepContext`, `StepOutcome`, or `Finding` --
callers translate its raw subprocess results into pipeline types themselves. Keeping that
boundary here is what keeps this module reusable across steps instead of drifting back into
being rebase-step-private.

**Non-blocking (issue #62)**: `run_git` used to shell out via a blocking `subprocess.run`,
called from `RebaseStep.run` -- an `async def` method -- which froze the whole asyncio
event loop (including the Pipeline box's elapsed-duration tick, `tui/app.py`'s 0.25s timer)
for the duration of every git subprocess call. `run_git` is now `async def`, spawning via
`asyncio.create_subprocess_exec` and awaiting `process.communicate()`, matching
`agent/claude_cli.py`'s own precedent for the same reason (see that module's docstring). It
still returns a plain `subprocess.CompletedProcess[str]` -- built from the awaited
process's exit code and decoded stdout/stderr -- so every caller's existing
`.returncode`/`.stdout`/`.stderr` access keeps working unchanged; only the call site grows
an `await`. `ref_sha`, `is_ancestor`, and `conflicted_files` below are `async def` for the
same reason, since each calls `run_git` internally. `rebase_in_progress` does no subprocess
call (pure filesystem check) and stays sync.

**Timed sub-step activity (issue #64)**: `run_git` reports itself through an
`ActivityReporter`, start to finish, for every call it makes -- so `RebaseStep`'s whole
call sequence (fetch, the unpushed-local-default guard's own `run_git`/`ref_sha`/
`is_ancestor` calls, the rebase, a conflict read, an abort) renders as individually-timed
nested lines in the TUI's Pipeline box, with zero changes at any `steps/rebase.py` call
site. `run_git` has no `StepContext` parameter to read a reporter off of, so it reaches one
ambiently: `pipeline.step.current_activity_reporter` is a `contextvars.ContextVar` that
`executor.run_steps` binds from `ctx.activity_reporter` for the duration of each
`step.run(ctx)` call (see that module's docstring), and `run_git` reads it directly via
`.get()`. `pipeline.step.activity_or_nullcontext` supplies the "wrap in `reporter.
activity(label)`, or run unwrapped when nothing is bound" branch -- the same one
`StepContext.report_activity` uses for its own, explicit reporter -- so that branch is not
duplicated here. `_git_activity_label` derives a short label from the subcommand and its
main argument (e.g. `"git fetch origin"`, `"git rebase origin/main"`) -- informative enough
for a human watching the TUI to tell which call is in flight, not an exhaustive
description of every flag.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from code_review.pipeline.step import activity_or_nullcontext, current_activity_reporter


def _git_activity_label(args: list[str]) -> str:
    """Derive a short activity label from a `run_git` call's own `args`: the subcommand
    plus its main argument, e.g. `["fetch", "origin", "main"]` -> `"git fetch origin"`,
    `["rebase", "origin/main"]` -> `"git rebase origin/main"`. Doesn't need to be
    exhaustive (see module docstring's "Timed sub-step activity (issue #64)" section) --
    just enough for a human to tell which call is in flight. `args` is never empty in
    practice (every call site below and in `steps/rebase.py` passes a real subcommand);
    the empty-`args` case degrades to `"git"` rather than raising.
    """

    subcommand = args[0] if args else ""
    main_arg = args[1] if len(args) > 1 else ""
    return f"git {subcommand} {main_arg}".rstrip()


async def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a `git` subprocess in `cwd`, capturing output as text without raising on a
    nonzero exit -- callers below (and in `steps/rebase.py`) inspect `.returncode`
    themselves, since both a clean success and an expected failure (e.g. a conflicting
    rebase) are ordinary outcomes here, not exceptional ones.

    Non-blocking w.r.t. the asyncio event loop -- see module docstring's "Non-blocking
    (issue #62)" section. Mirrors `agent/claude_cli.py`'s `ClaudeCLI.run` fast path:
    `asyncio.create_subprocess_exec` plus `process.communicate()`.

    Reports itself, start to finish, through whichever `ActivityReporter` is ambient for
    the currently running step -- see module docstring's "Timed sub-step activity (issue
    #64)" section. A no-op when nothing is bound (e.g. every call from a test that doesn't
    attach a reporter), so this call site's behavior is unchanged when issue #66's seam is
    unused.
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
    """True if `cwd`'s `.git` shows a paused rebase -- either the merge-based
    (`rebase-merge`, used by the default rebase backend) or apply-based (`rebase-apply`,
    used by `--whitespace`/`-am` style rebases) state directory. This is the same on-disk
    signal `git status` reads to print "rebase in progress", and is what tells a genuine
    conflict (git paused mid-replay, leaving one of these behind) apart from a rebase that
    never started at all (e.g. a dirty working tree or a bad upstream ref, neither of which
    creates either directory) -- see `steps/rebase.py`'s module docstring's "Conflict
    detection" section.
    """

    git_dir = cwd / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


async def ref_sha(ref: str, cwd: Path) -> str | None:
    """Resolve `ref` to a SHA in `cwd`, or `None` if it does not exist. Uses `rev-parse
    --verify --quiet` so a missing ref (e.g. no local branch literally named
    `<default_branch>` in a given checkout) is an ordinary "not found" result -- nonzero
    exit, no stderr noise -- rather than something a caller has to except around.
    """

    result = await run_git(["rev-parse", "--verify", "--quiet", ref], cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


async def is_ancestor(maybe_ancestor: str, descendant: str, cwd: Path) -> bool:
    """True if `maybe_ancestor` is `descendant` itself or a commit `descendant` was built
    on top of, per `git merge-base --is-ancestor`'s own exit-code contract (0 for
    ancestor-or-equal, 1 for "not an ancestor", >1 for a bad ref). Callers are expected to
    only ever pass refs already resolved by a prior successful `ref_sha`/fetch, never a ref
    that might not exist.
    """

    result = await run_git(["merge-base", "--is-ancestor", maybe_ancestor, descendant], cwd)
    return result.returncode == 0


async def conflicted_files(cwd: Path) -> list[str]:
    """Return the sorted list of paths with unresolved merge conflicts, read via `git diff
    --name-only --diff-filter=U`. Must be called before `git rebase --abort` runs -- the
    unmerged state this reads from disappears once the abort completes. Sorted for a
    deterministic order regardless of the underlying filesystem/git enumeration order.
    """

    result = await run_git(["diff", "--name-only", "--diff-filter=U"], cwd)
    return sorted(line for line in result.stdout.splitlines() if line)
