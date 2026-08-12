"""PR creation with evidence -- the pipeline's last step.

Fully deterministic for this ticket: no agent/LLM call anywhere in `PRStep.run`. Title is a
static conventional-commit-style placeholder; "What Changed" comes straight from `git diff
--name-status`; Intent/Risk/Testing come from the pipeline's own already-computed
`ctx.intent`/`ctx.step_outcomes` (see `pipeline/step.py`'s `StepContext.step_outcomes`).
#121 replaces the title/"What Changed" section with an agent draft plus this same
deterministic fallback; #122 adds a body length guard and screenshot/video artifacts.
Neither exists yet.

**"The branch under review" is HEAD**, exactly like `steps/rebase.py`: `StepContext` has no
field naming it, so this step resolves it via `gitutils.current_branch(ctx.cwd)` rather than
a `StepContext` field.

**"What Changed" diffs against `origin/<default_branch>`, never the literal local
`<default_branch>` ref** -- the same reasoning as `steps/rebase.py`'s own `git rebase
origin/<default_branch>` (never the local ref): `RebaseStep` runs earlier in this same
pipeline and already does `git fetch origin <default_branch>`, which updates
`refs/remotes/origin/<default_branch>`, not the local `refs/heads/<default_branch>` ref --
so by the time `PRStep` runs, `origin/<default_branch>` is the fresh, correct base; the
local ref can be arbitrarily stale (e.g. never pulled) and would otherwise make this
section over-report files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.scm.github import (
    create_pull_request,
    find_pull_request_for_branch,
    resolve_repo_slug,
    update_pull_request,
)
from code_review.steps.gitutils import current_branch, run_git
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput

# Deterministic fallback title (#121 replaces this with an agent-drafted one).
_FALLBACK_TITLE = "chore: update pull request"


async def _what_changed_section(cwd: Path, base: str, head: str) -> str:
    # origin/<base>, not the literal local <base> ref -- see module docstring.
    diff = await run_git(["diff", "--name-status", f"origin/{base}...{head}"], cwd)
    return f"## What Changed\n\n{diff.stdout.strip()}"


def _intent_section(ctx: StepContext) -> str:
    return f"## Intent\n\n{ctx.intent.summary}"


def _risk_section(ctx: StepContext) -> str | None:
    """`None` when `ReviewStep` hasn't run yet (or `ctx` was built directly, bypassing the
    executor) rather than a placeholder heading -- see `StepContext.step_outcomes`'s
    absent-case contract."""

    outcome = ctx.step_outcomes.get("ReviewStep")
    if outcome is None or not isinstance(outcome.payload, ReviewOutput):
        return None
    review = outcome.payload
    return f"## Risk Assessment\n\n**Risk level:** {review.risk_level}\n\n{review.risk_rationale}"


def _testing_section(ctx: StepContext) -> str | None:
    """Mirrors `_risk_section`'s absent-case contract for `TestSufficiencyStep`."""

    outcome = ctx.step_outcomes.get("TestSufficiencyStep")
    if outcome is None or not isinstance(outcome.payload, TestSufficiencyOutput):
        return None
    output = outcome.payload
    body = output.testing_summary
    if output.tested:
        body = f"{body}\n\n" + "\n".join(f"- {behavior}" for behavior in output.tested)
    return f"## Testing\n\n{body}"


async def _build_body(ctx: StepContext, default_branch: str, branch: str) -> str:
    sections = [
        await _what_changed_section(ctx.cwd, default_branch, branch),
        _intent_section(ctx),
    ]
    for section in (_risk_section(ctx), _testing_section(ctx)):
        if section is not None:
            sections.append(section)
    return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class PRStep(Step):
    """Finds or creates/updates the PR for the branch under review (`ctx.cwd`'s current
    HEAD), with a deterministic title/body -- see module docstring. No agent call.
    """

    # Remote's default branch, for both the skip check and the PR base. Not
    # auto-detected -- see steps/rebase.py's RebaseStep, the same pattern.
    default_branch: str = "main"
    # Subprocess test seam for scm.github's gh_executable; tests point this at a fake
    # script, mirroring ReviewStep.executable.
    gh_executable: str | Path = "gh"

    async def run(self, ctx: StepContext) -> StepOutcome:
        branch = await current_branch(ctx.cwd)
        if branch is None:
            raise RuntimeError(
                f"could not resolve the current branch in {ctx.cwd} (detached HEAD?)"
            )
        if branch == self.default_branch:
            # Already on the default branch -- nothing to open a PR for.
            return StepOutcome(needs_approval=False, auto_fixable=False, payload=[])

        repo_slug = await resolve_repo_slug(ctx.cwd)
        if repo_slug is None:
            raise RuntimeError(
                f"could not resolve a GitHub owner/repo slug from {ctx.cwd}'s origin remote"
            )

        title = _FALLBACK_TITLE
        body = await _build_body(ctx, self.default_branch, branch)

        existing = await find_pull_request_for_branch(
            branch, repo_slug, ctx.cwd, gh_executable=self.gh_executable
        )
        if existing is None:
            await create_pull_request(
                repo_slug=repo_slug,
                head=branch,
                base=self.default_branch,
                title=title,
                body=body,
                cwd=ctx.cwd,
                gh_executable=self.gh_executable,
            )
        else:
            await update_pull_request(
                existing.number,
                repo_slug=repo_slug,
                title=title,
                body=body,
                cwd=ctx.cwd,
                gh_executable=self.gh_executable,
            )

        return StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
