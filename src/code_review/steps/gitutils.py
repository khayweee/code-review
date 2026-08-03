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
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a `git` subprocess in `cwd`, capturing output as text without raising on a
    nonzero exit -- callers below (and in `steps/rebase.py`) inspect `.returncode`
    themselves, since both a clean success and an expected failure (e.g. a conflicting
    rebase) are ordinary outcomes here, not exceptional ones."""

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


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


def ref_sha(ref: str, cwd: Path) -> str | None:
    """Resolve `ref` to a SHA in `cwd`, or `None` if it does not exist. Uses `rev-parse
    --verify --quiet` so a missing ref (e.g. no local branch literally named
    `<default_branch>` in a given checkout) is an ordinary "not found" result -- nonzero
    exit, no stderr noise -- rather than something a caller has to except around.
    """

    result = run_git(["rev-parse", "--verify", "--quiet", ref], cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def is_ancestor(maybe_ancestor: str, descendant: str, cwd: Path) -> bool:
    """True if `maybe_ancestor` is `descendant` itself or a commit `descendant` was built
    on top of, per `git merge-base --is-ancestor`'s own exit-code contract (0 for
    ancestor-or-equal, 1 for "not an ancestor", >1 for a bad ref). Callers are expected to
    only ever pass refs already resolved by a prior successful `ref_sha`/fetch, never a ref
    that might not exist.
    """

    result = run_git(["merge-base", "--is-ancestor", maybe_ancestor, descendant], cwd)
    return result.returncode == 0


def conflicted_files(cwd: Path) -> list[str]:
    """Return the sorted list of paths with unresolved merge conflicts, read via `git diff
    --name-only --diff-filter=U`. Must be called before `git rebase --abort` runs -- the
    unmerged state this reads from disappears once the abort completes. Sorted for a
    deterministic order regardless of the underlying filesystem/git enumeration order.
    """

    result = run_git(["diff", "--name-only", "--diff-filter=U"], cwd)
    return sorted(line for line in result.stdout.splitlines() if line)
