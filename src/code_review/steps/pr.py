"""PR creation with evidence -- the pipeline's last step.

One bounded agent call drafts the title and the "What Changed" bullets
(`code_review.prompt.pr.build_pr_draft_prompt` into `PRDraft`); when that call fails
outright the step falls back to a static conventional-commit-style title plus a "What
Changed" section derived from `git diff --name-status`. Intent/Risk/Testing are assembled
deterministically either way from the pipeline's own already-computed
`ctx.intent`/`ctx.step_outcomes` (see `pipeline/step.py`'s `StepContext.step_outcomes`), and
the agent is told not to write them.

Drafted output is sanitized before use (`_sanitized_title`/`_cleaned_what_changed_bullets`):
prompt wording alone is not a hard invariant, so the heading/bullet-marker/length rules the
prompt asks for are enforced again in code.

**The branch is pushed to `origin` before the PR is found or created** -- `gh pr create
--head <branch>` needs a remote head, and nothing earlier in this pipeline pushes, so a
branch that exists only locally would otherwise fail here. The push names the branch ref
explicitly (`refs/heads/<branch>:refs/heads/<branch>`) and never `HEAD`: see
`_push_branch_to_origin` for why that distinction is load-bearing, and why a rejected push
is reported rather than forced.

**"The branch under review" is `ctx.branch`**, not `ctx.cwd`'s HEAD: `WorktreeStep` checks
its throwaway worktree out detached (see `steps/worktree.py`'s module docstring), so unlike
`steps/rebase.py` (which needs no branch name -- `git rebase` works the same on a detached
HEAD), this step can't re-derive a name from `gitutils.current_branch(ctx.cwd)`; it would
just get `None`. `gh pr create --head <branch>`/`gh pr view <branch>` only ever needed
`branch` as a string naming the *remote* branch, never an actual local checkout of it, so
reading `ctx.branch` directly is correct regardless of what's checked out in `ctx.cwd`.

**The fallback "What Changed" diffs against `origin/<default_branch>`, never the literal
local `<default_branch>` ref** -- the same reasoning as `steps/rebase.py`'s own `git rebase
origin/<default_branch>` (never the local ref): `RebaseStep` runs earlier in this same
pipeline and already does `git fetch origin <default_branch>`, which updates
`refs/remotes/origin/<default_branch>`, not the local `refs/heads/<default_branch>` ref --
so by the time `PRStep` runs, `origin/<default_branch>` is the fresh, correct base; the
local ref can be arbitrarily stale (e.g. never pulled) and would otherwise make this
section over-report files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from code_review.agent import Result, RunOpts
from code_review.agent.errors import AgentError
from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.prompt.pr import build_pr_draft_prompt
from code_review.scm.github import (
    create_pull_request,
    find_pull_request_for_branch,
    resolve_repo_slug,
    update_pull_request,
)
from code_review.steps.gitutils import run_git
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.steps.tool_activity import tool_stream_relay

# Title used whenever no usable agent-drafted one is available (call failed, or the drafted
# title sanitized down to nothing).
_FALLBACK_TITLE = "chore: update pull request"

# GitHub rejects a PR title longer than this.
_MAX_TITLE_LENGTH = 256

# GitHub rejects a PR body longer than this; `_fit_body_to_github_limit` sheds to stay under.
_MAX_BODY_LENGTH = 65536

# Appended by the last-resort truncation, and counted inside the budget above.
_TRUNCATION_MARKER = "\n\n_Truncated to fit GitHub's pull-request body length limit._"

_BACKTICK_RUN = re.compile(r"`+")

_WHITESPACE_RUN = re.compile(r"\s+")
_LEADING_HEADING_MARKERS = re.compile(r"^#+\s*")
_LEADING_BULLET_MARKER = re.compile(r"^[-*+]\s+")

# Headings this module generates itself; an agent echoing one back as a bullet would render
# the section twice.
_GENERATED_SECTION_TITLES = frozenset(
    {"what changed", "intent", "risk assessment", "evidence", "testing"}
)


# --- PRDraft ------------------------------------------------------------------------------


class Demonstration(BaseModel):
    """One concrete illustration of what the change does, rendered into the body's
    `## Evidence` section. Every field is plain text: this module owns the markdown.
    """

    # Rendering shape: `api` a request/response block, `behavior` one row of a shared table.
    kind: Literal["api", "behavior"]

    # Short name for the behavior on show ("Backoff delay"), not the claim itself.
    label: str

    # The stimulus: request, input, or starting condition.
    given: str | None = None

    # The result before this change; left out of the rendering entirely when unset.
    was: str | None = None

    # The result after this change.
    now: str | None = None


class PRDraft(BaseModel):
    """The PR step's schema: what one agent call drafts for the pull request."""

    # Conventional-commit-style title; sanitized by `_sanitized_title` before use.
    title: str

    # "What Changed" bullets, without their markers; cleaned by
    # `_cleaned_what_changed_bullets` before use.
    what_changed: list[str]

    # Evidence-section material. Defaults to empty rather than being required so an omission
    # costs only the Evidence section, not the whole draft (which would fall back).
    demonstrations: list[Demonstration] = []


# --- Drafted-output sanitizing --------------------------------------------------------------


def _collapse_whitespace(text: str) -> str:
    """Flatten every run of whitespace (newlines included) to a single space and trim -- a
    drafted title or bullet spanning lines would otherwise break the rendered markdown.
    """

    return _WHITESPACE_RUN.sub(" ", text).strip()


def _sanitized_title(title: str) -> str:
    """One-line, heading-marker-free, length-capped PR title, falling back to
    `_FALLBACK_TITLE` when nothing survives.
    """

    flattened = _collapse_whitespace(title)
    without_headings = _LEADING_HEADING_MARKERS.sub("", flattened).strip()
    if not without_headings:
        return _FALLBACK_TITLE
    return without_headings[:_MAX_TITLE_LENGTH]


def _is_generated_section_heading(text: str) -> bool:
    """True for an echoed heading of a section this module generates itself, with or
    without its `##` prefix (e.g. `## Testing`, `Testing`).
    """

    return _LEADING_HEADING_MARKERS.sub("", text).strip().casefold() in _GENERATED_SECTION_TITLES


def _cleaned_what_changed_bullets(what_changed: list[str]) -> list[str]:
    """Drafted bullets ready to render: whitespace-flattened, stripped of any bullet marker
    the agent added itself (this module adds its own), with blanks and echoed
    generated-section headings dropped. Empty means "nothing usable" -- the caller falls
    back to the deterministic section.
    """

    cleaned = []
    for entry in what_changed:
        flattened = _collapse_whitespace(entry)
        without_marker = _LEADING_BULLET_MARKER.sub("", flattened).strip()
        if not without_marker or _is_generated_section_heading(without_marker):
            continue
        cleaned.append(without_marker)
    return cleaned


# --- Body sections --------------------------------------------------------------------------


def _drafted_what_changed_section(bullets: list[str]) -> str:
    return "## What Changed\n\n" + "\n".join(f"- {bullet}" for bullet in bullets)


_NAME_STATUS_WORDS = {
    "A": "added",
    "C": "copied",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "T": "type changed",
    "U": "unmerged",
}


def _name_status_bullet(line: str) -> str | None:
    """Render one `git diff --name-status` line (`M<TAB>path`, or `R100<TAB>old<TAB>new`
    for a rename/copy) as a markdown bullet; `None` for a line that isn't one.
    """

    status, _, rest = line.partition("\t")
    paths = [path for path in rest.split("\t") if path]
    if not status or not paths:
        return None
    # Rename/copy statuses carry a similarity score (e.g. "R100"); only the letter matters.
    word = _NAME_STATUS_WORDS.get(status[0], status)
    if len(paths) > 1:
        return f"- {word} `{paths[0]}` -> `{paths[1]}`"
    return f"- {word} `{paths[0]}`"


async def _deterministic_what_changed_section(cwd: Path, base: str, head: str) -> str:
    """The no-agent "What Changed": one markdown bullet per changed path, derived from `git
    diff --name-status`. Interpolated raw, that output's tab-separated lines collapse into
    one run-on line wherever GitHub renders the body as prose.
    """

    # origin/<base>, not the literal local <base> ref -- see module docstring.
    diff = await run_git(["diff", "--name-status", f"origin/{base}...{head}"], cwd)
    bullets = [
        bullet
        for bullet in (_name_status_bullet(line) for line in diff.stdout.splitlines())
        if bullet is not None
    ]
    body = "\n".join(bullets) if bullets else "No file-level changes detected."
    return f"## What Changed\n\n{body}"


# --- Publishing the branch --------------------------------------------------------------


# git's own marker for a ref it refused to update, on both the "non-fast-forward" and the
# "fetch first" wordings (which of the two it prints depends only on whether the remote's
# commits are already in the local object store).
_PUSH_REJECTED_MARKER = "[rejected]"


async def _push_branch_to_origin(cwd: Path, branch: str) -> None:
    """Publish `branch` to `origin` so the PR has a remote head to open against. Already
    published and up to date is `git push`'s own no-op ("Everything up-to-date", exit 0), so
    it needs no special case here.

    Pushes the branch ref by name, never `HEAD`. `WorktreeStep` checks its worktree out
    detached and `RebaseStep` rebases that detached HEAD, so `HEAD` in `cwd` is rewritten
    history that does not belong to the branch ref -- pushing it would publish those commits
    as the branch. Worktrees share the repository's common ref store (only `HEAD` is
    per-worktree), so the explicit refspec resolves correctly from the worktree anyway.

    Never `--force`/`--force-with-lease`, and never `-u` (which would write to the user's
    git config for no benefit): a remote branch that has moved on is a divergence the user
    resolves, not something this step overwrites.
    """

    result = await run_git(["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], cwd)
    if result.returncode == 0:
        return

    detail = result.stderr.strip() or result.stdout.strip()
    if _PUSH_REJECTED_MARKER in result.stderr:
        raise RuntimeError(
            f"origin rejected the push of branch {branch!r}: the remote branch has diverged "
            f"from the local one. This step never force-pushes -- reconcile the two yourself "
            f"(e.g. `git pull --rebase origin {branch}`) and re-run. git said:\n{detail}"
        )
    raise RuntimeError(f"could not push branch {branch!r} to origin. git said:\n{detail}")


# --- Evidence section -----------------------------------------------------------------------


def _is_renderable(demonstration: Demonstration) -> bool:
    """True when there is both a claim and something to show for it. An empty fence or an
    empty table row reads worse than no Evidence section at all, so anything else is dropped
    here rather than rendered -- prompt wording is not an invariant.
    """

    if not demonstration.label.strip():
        return False
    return bool((demonstration.given or "").strip() or (demonstration.now or "").strip())


# Stands in for a table cell with no value, so the row never renders as a blank gap.
_EMPTY_CELL = "-"


def _escaped_table_cell(text: str | None) -> str:
    """One table cell's text: whitespace flattened (a newline would end the row early) and
    any literal `|` escaped (it would otherwise open a new column).

    An absent value renders as `_EMPTY_CELL` rather than as nothing. The `Was` column is
    included whenever *any* row has a prior value, so rows without one would otherwise
    render as a blank gap that reads as a broken table instead of "no prior state".
    """

    escaped = _collapse_whitespace(text or "").replace("|", "\\|")
    return escaped or _EMPTY_CELL


def _fence_long_enough_for(content: str) -> str:
    """A fence with more backticks than the longest run inside `content`, so a ``` in a
    payload is contained instead of closing the block early. CommonMark's own mechanism,
    and verified against GitHub's renderer, which still syntax-highlights the longer fence.
    """

    longest_run = max((len(run) for run in _BACKTICK_RUN.findall(content)), default=0)
    return "`" * max(3, longest_run + 1)


def _api_demonstration_block(demonstration: Demonstration) -> str:
    """A bolded claim over a fenced `http` exchange (the info string GitHub highlights as an
    HTTP transcript).

    Prior and current responses are labeled only when both are present: with one response
    there is nothing to disambiguate, and unlabeled is the shape verified against GitHub's
    renderer.
    """

    given = (demonstration.given or "").strip()
    was = (demonstration.was or "").strip()
    now = (demonstration.now or "").strip()

    parts = [given] if given else []
    if was:
        parts.append(f"Was:\n{was}")
        if now:
            parts.append(f"Now:\n{now}")
    elif now:
        parts.append(now)

    exchange = "\n\n".join(parts)
    fence = _fence_long_enough_for(exchange)
    label = _collapse_whitespace(demonstration.label)
    return f"**{label}**\n\n{fence}http\n{exchange}\n{fence}"


def _behavior_demonstrations_table(demonstrations: list[Demonstration]) -> str:
    """Every behavioral demonstration in ONE table, never a one-row table each -- a table
    per demonstration is visually heavy and defeats the point of a glanceable section. The
    `Was` column is dropped entirely when no row has a prior value to put in it.
    """

    include_was = any((demonstration.was or "").strip() for demonstration in demonstrations)
    headers = ["Behavior", "Given", "Was", "Now"] if include_was else ["Behavior", "Given", "Now"]

    rows = [headers, ["---"] * len(headers)]
    for demonstration in demonstrations:
        cells = [
            _escaped_table_cell(demonstration.label),
            _escaped_table_cell(demonstration.given),
        ]
        if include_was:
            cells.append(_escaped_table_cell(demonstration.was))
        cells.append(_escaped_table_cell(demonstration.now))
        rows.append(cells)

    return "\n".join("| " + " | ".join(row) + " |" for row in rows)


def _evidence_section(demonstrations: list[Demonstration]) -> str | None:
    """The grouped behavior table (if any) followed by one block per API demonstration, or
    `None` when nothing survives `_is_renderable` -- an empty heading is worse than none.
    """

    renderable = [
        demonstration for demonstration in demonstrations if _is_renderable(demonstration)
    ]
    if not renderable:
        return None

    blocks = []
    behaviors = [demonstration for demonstration in renderable if demonstration.kind == "behavior"]
    if behaviors:
        blocks.append(_behavior_demonstrations_table(behaviors))
    blocks.extend(
        _api_demonstration_block(demonstration)
        for demonstration in renderable
        if demonstration.kind == "api"
    )
    return "## Evidence\n\n" + "\n\n".join(blocks)


def _label_only_evidence_section(demonstrations: list[Demonstration]) -> str | None:
    """The Evidence section degraded to its claims alone -- the body guard's first shedding
    step, which keeps what each demonstration asserts while dropping the payloads that make
    it long.
    """

    labels = [
        _collapse_whitespace(demonstration.label)
        for demonstration in demonstrations
        if _is_renderable(demonstration)
    ]
    if not labels:
        return None
    return "## Evidence\n\n" + "\n".join(f"- {label}" for label in labels)


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


def _test_sufficiency_output(ctx: StepContext) -> TestSufficiencyOutput | None:
    """`TestSufficiencyStep`'s own answer, under the same absent-case contract as
    `_risk_section`. Read both to render the Testing section and to ground the drafting
    prompt (`_observed_testing_material`).
    """

    outcome = ctx.step_outcomes.get("TestSufficiencyStep")
    if outcome is None or not isinstance(outcome.payload, TestSufficiencyOutput):
        return None
    return outcome.payload


def _testing_section(output: TestSufficiencyOutput, *, include_tested: bool) -> str:
    """`include_tested=False` keeps the summary but drops the per-behavior list -- the body
    guard's third shedding step.
    """

    body = output.testing_summary
    if include_tested and output.tested:
        body = f"{body}\n\n" + "\n".join(f"- {behavior}" for behavior in output.tested)
    return f"## Testing\n\n{body}"


def _observed_testing_material(ctx: StepContext) -> str | None:
    """What `TestSufficiencyStep` reported it actually observed, formatted as prompt
    material so drafted demonstrations can be grounded in it. `None` when that step has no
    usable outcome here.
    """

    output = _test_sufficiency_output(ctx)
    if output is None:
        return None

    lines = [f"- verified behavior: {behavior}" for behavior in output.tested]
    for artifact in output.artifacts:
        location = f" ({artifact.location})" if artifact.location else ""
        lines.append(f"- {artifact.kind}: {artifact.description}{location}")
    if not lines:
        return None
    return "\n".join(lines)


# --- Body assembly and the length guard ------------------------------------------------------


def _assemble_body(
    never_shed: list[str],
    evidence: str | None,
    testing: TestSufficiencyOutput | None,
    *,
    include_tested: bool,
) -> str:
    sections = list(never_shed)
    if evidence is not None:
        sections.append(evidence)
    if testing is not None:
        sections.append(_testing_section(testing, include_tested=include_tested))
    return "\n\n".join(sections)


def _truncated_at_a_line_boundary(body: str) -> str:
    """Last resort when every shedding step still leaves the body too long: cut on a newline
    so no markdown construct is left half-rendered, and count the visible marker inside the
    budget rather than pushing the result back over it.
    """

    head = body[: _MAX_BODY_LENGTH - len(_TRUNCATION_MARKER)]
    boundary = head.rfind("\n")
    if boundary > 0:
        head = head[:boundary]
    return head + _TRUNCATION_MARKER


def _fit_body_to_github_limit(
    *,
    what_changed: str,
    intent: str,
    risk: str | None,
    demonstrations: list[Demonstration],
    testing: TestSufficiencyOutput | None,
) -> str:
    """The PR body, shed in a fixed order until it fits GitHub's `_MAX_BODY_LENGTH` cap:
    full body, then demonstrations degraded to label-only bullets, then Evidence dropped
    entirely, then the Testing section's per-behavior list dropped (its summary stays), then
    a hard truncation at a line boundary. Each form is measured in turn and the first that
    fits wins.

    `what_changed`/`intent`/`risk` are never shed, and are ordered first so even the final
    truncation eats the sheddable tail before reaching them.

    Pure: no `StepContext`, no subprocess, so the shedding order is testable directly.
    """

    never_shed = [what_changed, intent] + ([risk] if risk is not None else [])
    candidates = [
        _assemble_body(never_shed, _evidence_section(demonstrations), testing, include_tested=True),
        _assemble_body(
            never_shed, _label_only_evidence_section(demonstrations), testing, include_tested=True
        ),
        _assemble_body(never_shed, None, testing, include_tested=True),
        _assemble_body(never_shed, None, testing, include_tested=False),
    ]
    for candidate in candidates:
        if len(candidate) <= _MAX_BODY_LENGTH:
            return candidate
    return _truncated_at_a_line_boundary(candidates[-1])


@dataclass(frozen=True, slots=True)
class PullRequestOutcome:
    """`PRStep`'s `StepOutcome.payload` shape once it actually opens/updates a PR --
    mirrors `steps/intent.py`'s `Intent` precedent for a non-`Finding` payload in that same
    closed union (see `pipeline/step.py`'s `StepOutcome.payload` docstring).

    `url`/`number` come straight from `scm.github.PullRequest`, the real PR `gh` just
    reported back. `created` distinguishes "opened a new PR" (`True`) from "updated an
    existing one" (`False`) -- `PRStep.run`'s own create-vs-update branch, carried forward
    so a renderer (e.g. the TUI's Pipeline box) can say "opened"/"updated" without
    re-deriving it.
    """

    url: str
    number: int
    created: bool


@dataclass(frozen=True, slots=True)
class PRStep(Step):
    """Pushes the branch under review (`ctx.branch`) to `origin`, then finds or
    creates/updates its PR with an agent-drafted title/"What Changed" and a deterministic
    fallback -- see module docstring.
    """

    # Remote's default branch, for both the skip check and the PR base. Not
    # auto-detected -- see steps/rebase.py's RebaseStep, the same pattern.
    default_branch: str = "main"
    # Subprocess test seam for scm.github's gh_executable; tests point this at a fake
    # script, mirroring ReviewStep.executable.
    gh_executable: str | Path = "gh"
    # Subprocess test seam for RunOpts.executable; tests point this at a fake CLI script.
    executable: str | Path = "claude"

    async def _draft(self, ctx: StepContext) -> Result[PRDraft] | None:
        """The one bounded agent call, or `None` when it fails outright so the caller can
        fall back to the deterministic title/body.

        Only `AgentError` (the backend's whole failure hierarchy) is caught, and only around
        the call itself -- a failure in the surrounding git/`gh` work has no fallback and
        must still propagate.
        """

        # Static label ("via claude"), not self.executable -- that field is a test seam,
        # this names the production backend. Reports "finished" even if the call raises.
        async with ctx.report_activity("Agent: drafting pull request via claude") as activity:
            # None with no reporter attached (rather than a relay that's a no-op at
            # runtime) so the call stays on claude_cli.py's legacy --output-format json
            # path when there's nothing to stream tool calls to.
            on_stream_event = (
                tool_stream_relay(ctx.activity_reporter, ctx.cwd)
                if ctx.activity_reporter is not None
                else None
            )
            try:
                return await ctx.agent.run(
                    RunOpts(
                        prompt=build_pr_draft_prompt(
                            ctx, observed_testing=_observed_testing_material(ctx)
                        ),
                        cwd=ctx.cwd,
                        output_schema=PRDraft,
                        executable=self.executable,
                        on_stream_event=on_stream_event,
                    )
                )
            except AgentError as error:
                activity.fail(str(error))
                return None

    async def run(self, ctx: StepContext) -> StepOutcome:
        branch = ctx.branch
        if branch == self.default_branch:
            # Already on the default branch -- nothing to open a PR for.
            return StepOutcome(needs_approval=False, auto_fixable=False, payload=[])

        repo_slug = await resolve_repo_slug(ctx.cwd)
        if repo_slug is None:
            raise RuntimeError(
                f"could not resolve a GitHub owner/repo slug from {ctx.cwd}'s origin remote"
            )

        # Before the drafting call, not after: an unpushable branch has no PR to open, and
        # failing fast here avoids spending an LLM call whose result is then thrown away.
        await _push_branch_to_origin(ctx.cwd, branch)

        drafted = await self._draft(ctx)
        draft = drafted.output if drafted is not None else None
        title = _sanitized_title(draft.title) if draft is not None else _FALLBACK_TITLE
        bullets = _cleaned_what_changed_bullets(draft.what_changed) if draft is not None else []
        what_changed = (
            _drafted_what_changed_section(bullets)
            if bullets
            else await _deterministic_what_changed_section(ctx.cwd, self.default_branch, branch)
        )
        body = _fit_body_to_github_limit(
            what_changed=what_changed,
            intent=_intent_section(ctx),
            risk=_risk_section(ctx),
            demonstrations=draft.demonstrations if draft is not None else [],
            testing=_test_sufficiency_output(ctx),
        )

        existing = await find_pull_request_for_branch(
            branch, repo_slug, ctx.cwd, gh_executable=self.gh_executable
        )
        if existing is None:
            pull_request = await create_pull_request(
                repo_slug=repo_slug,
                head=branch,
                base=self.default_branch,
                title=title,
                body=body,
                cwd=ctx.cwd,
                gh_executable=self.gh_executable,
            )
            created = True
        else:
            pull_request = await update_pull_request(
                existing.number,
                repo_slug=repo_slug,
                title=title,
                body=body,
                cwd=ctx.cwd,
                gh_executable=self.gh_executable,
            )
            created = False

        return StepOutcome(
            needs_approval=False,
            auto_fixable=False,
            payload=PullRequestOutcome(
                url=pull_request.url, number=pull_request.number, created=created
            ),
            usage=drafted.usage if drafted is not None else None,
        )
