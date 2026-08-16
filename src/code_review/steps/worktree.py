"""Worktree isolation: the pipeline's first step. Creates a throwaway `git worktree` with
`ctx.branch` checked out for real (never `--detach` -- see `steps/rebase.py`'s/`steps/pr.py`'s
own "the branch under review is `ctx.cwd`'s HEAD" assumption, which a detached checkout
would break) and redirects every later step's `ctx.cwd` at it (`StepOutcome.cwd_override`,
see `pipeline/AGENTS.md`'s WorktreeStep section), so a run never touches the user's real
checkout. Runs first so `ctx.cwd`'s HEAD is already the real `<branch>` by the time
`RebaseStep`/`PRStep` run.

`resolve_branch_head_short_sha`/`create_worktree` are async, built on `steps/gitutils.py`'s
`run_git`, exactly like every other step's git subprocess work -- non-blocking, and reported
as ambient activity in the TUI. `remove_worktree`, in contrast, stays sync: it is never
called from inside `run_steps` (it has no `Step` of its own -- `cli.py`'s `review` calls it
directly, after the TUI has fully exited, to clean up the worktree `WorktreeStep` created).
It deliberately does not go through `run_git`: at that point in `cli.py` there is no running
step and so no ambient `ActivityReporter` for it to report through, matching `cli.py`'s own
sync pre/post-TUI helpers (`_verify_branch`, `_diff_against_default_branch`).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from code_review.install_state import state_dir
from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.steps.gitutils import run_git

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class BranchAlreadyCheckedOutError(RuntimeError):
    """`ctx.branch` is already checked out elsewhere in this repo (most commonly the user's
    own main working copy) -- `git worktree add` refuses to check out the same branch into
    two worktrees at once, and this project's answer is to fail clearly rather than force
    or auto-detach past it (see `WorktreeStep.run`, the sole raiser)."""


def worktrees_root() -> Path:
    """Where every review run's throwaway worktree lives, under `install_state.state_dir()`
    (matches how `run_log.py` anchors per-run state under that same root)."""

    return state_dir() / "worktrees"


def sanitize_branch_name_for_path(branch: str) -> str:
    """Replace every filesystem-unsafe character in `branch` (starting with `/`, e.g.
    `feature/foo`) with `-`, so it collapses to a single valid path segment."""

    return _UNSAFE_PATH_CHARS.sub("-", branch)


async def resolve_branch_head_short_sha(branch: str, cwd: Path) -> str:
    """Short SHA of `branch`'s own tip commit, resolved in the user's real repo (`cwd`)
    before the worktree is created."""

    result = await run_git(["rev-parse", "--short", branch], cwd)
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse --short {branch} failed in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


async def worktree_path_for_branch(branch: str, cwd: Path) -> Path:
    """This run's worktree directory: `<state_dir>/worktrees/
    code_review_<branch_name>_<short_hash_head>`. Resolves `branch`'s tip SHA in `cwd` (the
    user's real repo) before the worktree exists."""

    short_sha = await resolve_branch_head_short_sha(branch, cwd)
    name = f"code_review_{sanitize_branch_name_for_path(branch)}_{short_sha}"
    return worktrees_root() / name


async def create_worktree(cwd: Path, worktree_path: Path, branch: str) -> None:
    """`git worktree add <worktree_path> <branch>` -- a real branch checkout, never
    `--detach` (see module docstring). Raises `BranchAlreadyCheckedOutError` if `branch` is
    already checked out in another worktree of this same repo; any other failure raises a
    plain `RuntimeError` carrying git's own stderr.

    Passes `worktree_path` to git relative to `cwd` (via `os.path.relpath`, so it still
    resolves correctly even when `worktree_path` sits outside `cwd`'s own tree, e.g. under
    `state_dir()`), rather than its full absolute form -- git resolves it against the
    subprocess's own `cwd` either way, and `run_git`'s activity label renders whatever args
    it's given, so this is also what shows up in the TUI's activity log.
    """

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    relative_worktree_path = os.path.relpath(worktree_path, cwd)
    result = await run_git(["worktree", "add", relative_worktree_path, branch], cwd)
    if result.returncode == 0:
        return
    if "already checked out" in result.stderr:
        raise BranchAlreadyCheckedOutError(
            f"'{branch}' is already checked out elsewhere in this repository -- check out a "
            "different branch there first, then retry."
        )
    raise RuntimeError(f"git worktree add {worktree_path} {branch} failed: {result.stderr.strip()}")


def remove_worktree(git: str, cwd: Path, worktree_path: Path) -> None:
    """`git worktree remove --force <worktree_path>`, run synchronously after the TUI has
    exited -- see module docstring for why this stays outside `run_git`. Forced because a
    run may leave uncommitted edits behind (e.g. an unfinished fix round never committed) --
    `--keep-worktree` is the escape hatch for a user who wants to inspect those instead of
    having this cleanup discard them."""

    result = subprocess.run(
        [git, "worktree", "remove", "--force", str(worktree_path)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree remove {worktree_path} failed: {result.stderr.strip()}")


@dataclass(frozen=True, slots=True)
class WorktreeStep(Step):
    """Creates this run's throwaway worktree and redirects `ctx.cwd` at it for every step
    after this one -- see module docstring. No agent call.
    """

    async def run(self, ctx: StepContext) -> StepOutcome:
        worktree_path = await worktree_path_for_branch(ctx.branch, ctx.cwd)
        await create_worktree(ctx.cwd, worktree_path, ctx.branch)
        return StepOutcome(
            needs_approval=False, auto_fixable=False, payload=[], cwd_override=worktree_path
        )
