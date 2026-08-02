"""Rebase step -- Milestone 4 (see docs/ROADMAP.md), sliced as issue #23.

Keeps the branch under review current with the latest default branch before Review runs,
so later steps never answer against a stale diff (see docs/GLOSSARY.md's "Rebase step").
`RebaseStep.run` does exactly two real `git` subprocess calls on the happy path (`fetch`,
then `rebase`), no more on success, and no agent/LLM call anywhere -- mirroring
`steps/intent.py`'s `IntentStep`, the other Step in this pipeline with no agent call.

**"The branch under review" is HEAD.** `StepContext` (`pipeline/step.py`) has no field
naming the branch under review -- only `cwd`/`agent`/`diff`/`intent`/`on_input_needed`. The
only coherent reading, given `RebaseStep` gets no explicit branch name, is that the branch
under review is whatever is currently checked out in `ctx.cwd`. So this step never passes a
branch name to `git rebase`: it runs the *one-argument* form, `git rebase
origin/<default_branch>`, which rebases current HEAD in place. It deliberately does NOT use
the two-argument form (`git rebase <upstream> <branch>`), which implicitly checks out
`<branch>` first -- that would silently switch `ctx.cwd`'s checkout out from under every
other step sharing it. `ctx.cwd` is assumed to already be on the right branch.

`default_branch` is a constructor field (default `"main"`), not a `StepContext` field and
not auto-detected. Auto-detecting it via `git symbolic-ref refs/remotes/origin/HEAD` (or
similar) was considered and rejected: that ref is often unset on freshly `git init` +
`remote add`-based repos, including the ones this module's own tests build, which would
make auto-detection actively fragile for exactly the checkouts it needs to work against.
This mirrors `steps/review.py`'s `ReviewStep.executable: str | Path = "claude"` field -- a
constructor field with a production-sensible default, overridable by a caller or test.

**Conflict detection**: a nonzero exit from `git rebase` is not on its own proof of a
conflict -- a dirty working tree or a bad upstream ref also exits nonzero, and neither
leaves anything to abort. This module instead reads the same on-disk signal `git status`
itself reads to print "rebase in progress": whether `.git/rebase-merge` or
`.git/rebase-apply` exists (see `_rebase_in_progress`). Only when one of those is present
does this step treat the failure as a conflict, list the unmerged paths (`git diff
--name-only --diff-filter=U`, read *before* aborting -- that state disappears once the
abort runs), and run `git rebase --abort`. Any other nonzero exit (no such state directory)
is re-raised as a `RuntimeError` instead of being reported as a conflict finding, since this
step has no other classification for it and must not misreport an unrelated failure as one
the pipeline's fail-safe default already has a shape for.

Leaving the repo mid-rebase is non-negotiable: whenever `git rebase` fails with a genuine
conflict, `git rebase --abort` runs unconditionally before this step returns, so `ctx.cwd`
is always back to its pre-rebase state (never left "rebase in progress") by the time control
returns to the executor.

Each conflicted file becomes its own `Finding` (`pipeline/findings.py`) with
`action="ask-user"`: a rebase conflict is exactly the kind of judgement call the fail-safe
default points at a human for (see docs/GLOSSARY.md), never something this step resolves on
its own. `severity="error"` and `review_scope="source"` are this step's chosen defaults --
`error` because an unresolved conflict blocks the branch from proceeding at all (not merely
a stylistic warning), and `source` because a rebase conflict is about the author's own
branch content colliding with upstream, not about pipeline-generated delivery content (see
`Finding.review_scope`'s field comment for what "pipeline-owned-delivery" is reserved for).

Out of scope for this ticket (see docs/ROADMAP.md milestone 4's own text and issue #23):
the "bundled local commits" guard -- detecting commits that exist only on the *local*
default branch and were never pushed to `origin/<default_branch>` -- is issue #24, blocked
by this one, and is not built here.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import Step, StepContext, StepOutcome


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a `git` subprocess in `cwd`, capturing output as text without raising on a
    nonzero exit -- callers below inspect `.returncode` themselves, since both a clean
    success and an expected failure (e.g. a conflicting rebase) are ordinary outcomes
    here, not exceptional ones."""

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _rebase_in_progress(cwd: Path) -> bool:
    """True if `cwd`'s `.git` shows a paused rebase -- either the merge-based
    (`rebase-merge`, used by the default rebase backend) or apply-based (`rebase-apply`,
    used by `--whitespace`/`-am` style rebases) state directory. This is the same on-disk
    signal `git status` reads to print "rebase in progress", and is what tells a genuine
    conflict (git paused mid-replay, leaving one of these behind) apart from a rebase that
    never started at all (e.g. a dirty working tree or a bad upstream ref, neither of which
    creates either directory) -- see module docstring's "Conflict detection" section.
    """

    git_dir = cwd / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _conflicted_files(cwd: Path) -> list[str]:
    """Return the sorted list of paths with unresolved merge conflicts, read via `git
    diff --name-only --diff-filter=U`. Must be called before `git rebase --abort` runs --
    the unmerged state this reads from disappears once the abort completes. Sorted for a
    deterministic `Finding` order regardless of the underlying filesystem/git enumeration
    order.
    """

    result = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd)
    return sorted(line for line in result.stdout.splitlines() if line)


@dataclass(frozen=True, slots=True)
class RebaseStep(Step):
    """Syncs `ctx.cwd`'s current branch (HEAD) onto `origin/<default_branch>` before
    Review runs (see docs/GLOSSARY.md's "Rebase step"). No agent call: this step is pure
    `git` subprocess orchestration, the same shape as `steps/intent.py`'s `IntentStep`.
    """

    # Name of the remote's default branch to rebase onto, e.g. "main". A constructor
    # field rather than a `StepContext` field or auto-detected value -- see module
    # docstring's explanation of why auto-detection via `origin/HEAD` was rejected.
    # Mirrors `steps/review.py`'s `ReviewStep.executable` field for the same reason:
    # a production-sensible default, overridable by a caller or test.
    default_branch: str = "main"

    async def run(self, ctx: StepContext) -> StepOutcome:
        fetch = _run_git(["fetch", "origin", self.default_branch], ctx.cwd)
        if fetch.returncode != 0:
            raise RuntimeError(
                f"git fetch origin {self.default_branch} failed in {ctx.cwd}: "
                f"{fetch.stderr.strip()}"
            )

        # One-argument form: rebases current HEAD onto origin/<default_branch> in place.
        # Deliberately not the two-argument form -- see module docstring.
        rebase = _run_git(["rebase", f"origin/{self.default_branch}"], ctx.cwd)
        if rebase.returncode == 0:
            # Already up to date, or a clean fast-forward/rebase -- no findings, nothing
            # for a human to review here.
            return StepOutcome(needs_approval=False, auto_fixable=False, findings=[])

        if not _rebase_in_progress(ctx.cwd):
            # Nonzero exit with no paused-rebase state to abort: not a conflict this step
            # knows how to classify (e.g. a dirty working tree, or a bad upstream ref).
            # Re-raise rather than misreport it as a conflict finding -- see module
            # docstring's "Conflict detection" section.
            raise RuntimeError(
                f"git rebase origin/{self.default_branch} failed in {ctx.cwd} without "
                f"entering a conflict state: {rebase.stderr.strip()}"
            )

        # Read the conflicted paths before aborting -- the unmerged state this reads is
        # gone once `git rebase --abort` completes.
        conflicted_files = _conflicted_files(ctx.cwd)

        # Non-negotiable: the repo must never be left mid-rebase, regardless of what the
        # caller does with the returned findings.
        abort = _run_git(["rebase", "--abort"], ctx.cwd)
        if abort.returncode != 0:
            raise RuntimeError(f"git rebase --abort failed in {ctx.cwd}: {abort.stderr.strip()}")

        findings = [
            Finding(
                severity="error",
                description=(
                    f"Rebasing onto origin/{self.default_branch} conflicts in {path}. "
                    "The rebase was aborted; resolve the conflict manually and re-run."
                ),
                action="ask-user",
                review_scope="source",
            )
            for path in conflicted_files
        ]

        return StepOutcome(needs_approval=True, auto_fixable=False, findings=findings)
