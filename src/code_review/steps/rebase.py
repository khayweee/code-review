"""Rebase step: keeps the branch under review current with the default branch before
Review runs, so later steps never answer against a stale diff.

On the happy path, `RebaseStep.run` does `fetch`, then the unpushed-local-default guard's
checks, then `rebase`. No agent call.

**"The branch under review" is HEAD.** `StepContext` has no field naming it, so this step
treats whatever is checked out in `ctx.cwd` as the branch under review, and runs the
one-argument `git rebase origin/<default_branch>` (rebases HEAD in place). It deliberately
avoids the two-argument form (`git rebase <upstream> <branch>`), which would check out
`<branch>` first and switch `ctx.cwd`'s checkout out from under other steps sharing it.

`default_branch` is a constructor field (default `"main"`), not auto-detected: detecting it
via `origin/HEAD` is fragile on freshly `git init`-ed repos (including this module's own
test fixtures) where that ref is often unset.

**Conflict detection**: a nonzero `git rebase` exit isn't proof of a conflict by itself (a
dirty tree or bad upstream ref also exits nonzero, with nothing to abort). This step checks
`gitutils.rebase_in_progress` (whether `.git/rebase-merge`/`rebase-apply` exists) to tell a
genuine conflict apart from those; only then does it list unmerged paths and run `git
rebase --abort`. Any other nonzero exit is re-raised as `RuntimeError` rather than
misreported as a conflict finding.

The repo is never left mid-rebase: `git rebase --abort` runs unconditionally whenever a
genuine conflict is detected, before this step returns.

Each conflicted file becomes its own `Finding` with `action="ask-user"` (a conflict is a
human judgement call), `severity="error"`, `review_scope="source"`.

**The unpushed-local-default guard** runs after `fetch`, before `rebase`. It catches a
specific blind spot: a local branch literally named `<default_branch>` that has unpushed
commits which are already ancestors of HEAD (e.g. committed to locally, then merged into
the feature branch) — the ordinary rebase against `origin/<default_branch>` would never
notice that content since it only compares `origin/<default_branch>` vs. HEAD.

It fires only when both hold, checked via `git merge-base --is-ancestor`:
1. Local `<default_branch>` has commits `origin/<default_branch>` doesn't (a strict
   descendant, not equal).
2. Local `<default_branch>`'s tip is already an ancestor of HEAD.

If no local branch named `<default_branch>` exists, `gitutils.ref_sha` returns `None` and
the guard no-ops. When it fires, `RebaseStep.run` returns immediately with
`StepOutcome(needs_approval=True, auto_fixable=False)` and exactly one `Finding` (not one
per commit) naming every offending commit and file, with `severity="error"`,
`review_scope="source"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.steps.gitutils import (
    conflicted_files,
    is_ancestor,
    rebase_in_progress,
    ref_sha,
    run_git,
)


async def _unpushed_local_default_finding(cwd: Path, default_branch: str) -> Finding | None:
    """Build the unpushed-local-default guard's `Finding` if it fires, else `None` -- see
    module docstring. Must run after a successful `git fetch origin <default_branch>`.
    """

    local_tip = await ref_sha(f"refs/heads/{default_branch}", cwd)
    if local_tip is None:
        # No local branch named `default_branch` -- nothing to compare, not an error.
        return None

    origin_tip = await ref_sha(f"refs/remotes/origin/{default_branch}", cwd)
    if origin_tip is None:
        # Unexpected after a successful fetch; treat like "nothing to compare".
        return None

    if local_tip == origin_tip:
        return None

    if not await is_ancestor(origin_tip, local_tip, cwd):
        # Local default branch diverged from or fell behind origin, not ahead of it.
        return None

    if not await is_ancestor(local_tip, "HEAD", cwd):
        # Unpushed commits never made it into HEAD, so nothing to warn about.
        return None

    commit_range = f"{origin_tip}..{local_tip}"
    commits = (await run_git(["log", "--oneline", commit_range], cwd)).stdout.strip()
    diff_result = await run_git(["diff", "--name-only", commit_range], cwd)
    files = sorted(line for line in diff_result.stdout.splitlines() if line)

    return Finding(
        severity="error",
        description=(
            f"The local branch '{default_branch}' has commits never pushed to "
            f"origin/{default_branch}, and the branch under review already carries them "
            f"as ancestors of HEAD. Rebasing onto origin/{default_branch} now would "
            f"silently drag this unreviewed, unpushed content along. Unpushed commits:\n"
            f"{commits}\nAffected files: {', '.join(files)}"
        ),
        action="ask-user",
        review_scope="source",
    )


@dataclass(frozen=True, slots=True)
class RebaseStep(Step):
    """Syncs `ctx.cwd`'s current branch (HEAD) onto `origin/<default_branch>` before
    Review runs. No agent call -- pure `git` subprocess orchestration.
    """

    # Remote's default branch to rebase onto. Not auto-detected -- see module docstring.
    default_branch: str = "main"

    async def run(self, ctx: StepContext) -> StepOutcome:
        fetch = await run_git(["fetch", "origin", self.default_branch], ctx.cwd)
        if fetch.returncode != 0:
            raise RuntimeError(
                f"git fetch origin {self.default_branch} failed in {ctx.cwd}: "
                f"{fetch.stderr.strip()}"
            )

        # Must run after fetch, before rebase -- see module docstring.
        unpushed_finding = await _unpushed_local_default_finding(ctx.cwd, self.default_branch)
        if unpushed_finding is not None:
            return StepOutcome(needs_approval=True, auto_fixable=False, findings=[unpushed_finding])

        # One-argument form rebases current HEAD in place; see module docstring.
        rebase = await run_git(["rebase", f"origin/{self.default_branch}"], ctx.cwd)
        if rebase.returncode == 0:
            return StepOutcome(needs_approval=False, auto_fixable=False, findings=[])

        if not rebase_in_progress(ctx.cwd):
            # Not a conflict this step can classify (e.g. dirty tree, bad upstream ref).
            raise RuntimeError(
                f"git rebase origin/{self.default_branch} failed in {ctx.cwd} without "
                f"entering a conflict state: {rebase.stderr.strip()}"
            )

        # Must read before aborting -- unmerged state disappears once abort completes.
        conflicts = await conflicted_files(ctx.cwd)

        abort = await run_git(["rebase", "--abort"], ctx.cwd)
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
            for path in conflicts
        ]

        return StepOutcome(needs_approval=True, auto_fixable=False, findings=findings)
