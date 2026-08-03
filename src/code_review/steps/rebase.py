"""Rebase step -- Milestone 4 (see docs/ROADMAP.md), sliced as issue #23.

Keeps the branch under review current with the latest default branch before Review runs,
so later steps never answer against a stale diff (see docs/GLOSSARY.md's "Rebase step").
On the happy path `RebaseStep.run` does `fetch`, then the issue #24 guard's own `git`
calls (at minimum one `rev-parse` checking whether a local `<default_branch>` branch even
exists -- see that section below), then `rebase`; no agent/LLM call anywhere -- mirroring
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
`.git/rebase-apply` exists (see `gitutils.rebase_in_progress`). Only when one of those is present
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

**The unpushed-local-default guard (issue #24)** runs immediately after the `fetch` call
above and before the `rebase` call -- it needs `origin/<default_branch>` current (hence
after fetch), and it must stop the step cold before any rebase is attempted if it fires
(hence before rebase). It reasons about a *third* ref this step otherwise never looks at: a
local branch literally named `<default_branch>` in `ctx.cwd`, distinct from both
`origin/<default_branch>` (the remote-tracking ref the rebase itself targets) and HEAD (the
branch under review). The scenario: a developer committed directly to their local
`<default_branch>` (or merged into it) without ever pushing, then that local tip ended up
folded into HEAD's own history (e.g. the feature branch was cut from it, or later merged it
in). Rebasing onto the *fresh* `origin/<default_branch>` would proceed without ever
noticing that content, because the ordinary rebase path only ever reasons about
`origin/<default_branch>` vs. HEAD -- it has no idea a local-only `<default_branch>` branch
even exists. The guard closes that blind spot.

It fires only when **both** hold, checked with `git merge-base --is-ancestor` (exit 0 means
"is an ancestor of, or equal to"; exit 1 means "is not"; see `gitutils.is_ancestor`):
1. Local `<default_branch>`'s tip is a strict descendant of `origin/<default_branch>`'s tip
   -- i.e. it has commits `origin/<default_branch>` doesn't. Checked as "the two tips
   differ" AND "origin's tip is an ancestor of local's tip" (the second alone would also
   accept equality, which condition one already excludes).
2. Local `<default_branch>`'s tip is itself an ancestor of HEAD -- the branch under review
   already carries it.

If no local branch literally named `<default_branch>` exists in `ctx.cwd` at all (the
common case -- most checkouts, including this module's own `origin_and_checkout` test
fixture, never create one), `gitutils.ref_sha` returns `None` and the guard is a no-op: there is
nothing to compare, not an error condition. When it fires, `RebaseStep.run` returns
immediately with `StepOutcome(needs_approval=True, auto_fixable=False)` and exactly *one*
`Finding` (not one per commit, unlike the conflict-findings loop below) whose `description`
names every offending commit (`git log --oneline origin/<default_branch>..<local tip>`,
short SHA + subject -- enough for a reviewer to look each one up) and every file they touch
(`git diff --name-only`, same range). One `Finding` rather than several because these
commits are one coherent "unpushed local history" fact about the branch, not independent
findings the way each conflicted file is its own independent collision; the acceptance
criteria's own phrasing ("one Finding... naming the unpushed commits and affected files",
plural nouns inside a singular Finding) is explicit about this. `severity="error"` and
`review_scope="source"` mirror the conflict-findings' own reasoning below: `error` because
this blocks the branch from safely proceeding, and `source` because it is about the
author's own branch content, not pipeline-generated delivery content.
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
    """Build the issue #24 guard's `Finding` if it fires, else `None` -- see module
    docstring's "The unpushed-local-default guard" section for the full scenario and the
    exact two conditions checked below. Must run after a successful `git fetch origin
    <default_branch>` so the `origin/<default_branch>` tip this compares against is
    current.
    """

    local_tip = await ref_sha(f"refs/heads/{default_branch}", cwd)
    if local_tip is None:
        # No local branch literally named `default_branch` exists here -- nothing to
        # compare, so the guard cannot fire. Not an error: most checkouts, including this
        # module's own test fixture, never create one.
        return None

    origin_tip = await ref_sha(f"refs/remotes/origin/{default_branch}", cwd)
    if origin_tip is None:
        # The fetch immediately before this call succeeded, so this should always
        # resolve; treat an unexpected miss the same as "nothing to compare" rather than
        # raising -- this guard has no stronger claim on the ref than the rebase call
        # right after it.
        return None

    if local_tip == origin_tip:
        # Local default branch is exactly origin's tip: nothing unpushed, condition 1 is
        # trivially false.
        return None

    if not await is_ancestor(origin_tip, local_tip, cwd):
        # Local `default_branch` has diverged from (or fallen behind) origin rather than
        # being genuinely ahead of it -- condition 1 fails.
        return None

    if not await is_ancestor(local_tip, "HEAD", cwd):
        # The local tip exists but never made it into HEAD's own history -- condition 2
        # fails. The unpushed commits sit on local `default_branch` only; the branch under
        # review never incorporated them, so there is nothing riding along to warn about.
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
        fetch = await run_git(["fetch", "origin", self.default_branch], ctx.cwd)
        if fetch.returncode != 0:
            raise RuntimeError(
                f"git fetch origin {self.default_branch} failed in {ctx.cwd}: "
                f"{fetch.stderr.strip()}"
            )

        # Issue #24 guard -- must run after fetch (needs a current origin/<default_branch>)
        # and before rebase (must stop cold, no rebase attempted, if it fires). See module
        # docstring's "The unpushed-local-default guard" section.
        unpushed_finding = await _unpushed_local_default_finding(ctx.cwd, self.default_branch)
        if unpushed_finding is not None:
            return StepOutcome(needs_approval=True, auto_fixable=False, findings=[unpushed_finding])

        # One-argument form: rebases current HEAD onto origin/<default_branch> in place.
        # Deliberately not the two-argument form -- see module docstring.
        rebase = await run_git(["rebase", f"origin/{self.default_branch}"], ctx.cwd)
        if rebase.returncode == 0:
            # Already up to date, or a clean fast-forward/rebase -- no findings, nothing
            # for a human to review here.
            return StepOutcome(needs_approval=False, auto_fixable=False, findings=[])

        if not rebase_in_progress(ctx.cwd):
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
        conflicts = await conflicted_files(ctx.cwd)

        # Non-negotiable: the repo must never be left mid-rebase, regardless of what the
        # caller does with the returned findings.
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
