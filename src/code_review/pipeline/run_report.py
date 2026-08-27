"""Pure, Textual-independent summary of a pipeline run's LLM token usage.

`build_run_report` scans a run's `StepEvent`s (see `pipeline.schemas.StepEvent`) and sums
each step's `agent.Usage` (`StepOutcome.usage`) across every round it ran -- a step with
`supports_fix_round = True` (`ReviewStep`/`TestSufficiencyStep`) can run more than once per
pipeline slot (auto-fix rounds, or an uncapped human "fix" response at a park; see
`pipeline/AGENTS.md`'s "Milestone 7, ticket 2") -- into one `PipelineRunReport`. A step that
never called the agent (so never set `StepOutcome.usage`) contributes nothing and is omitted
from `per_step`, never rendered as a zeroed/empty row.

`format_run_report` renders that report as plain text, for the TUI's Status box
(`tui/state.py`'s `final_status_message`) and the run log (`run_log.py`). Returns `""` when
`report.per_step` is empty -- this codebase's "no box, not an empty box" rendering
discipline (see `tui/AGENTS.md`).

No Textual import -- usable from `tui/app.py` and, if ever needed later, a non-TUI caller.
This is v1: token usage only. A future field (e.g. issues identified/fixed) is a plain
addition to `PipelineRunReport`, not a reason to pre-build scaffolding here now.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from code_review.agent import Usage
from code_review.pipeline.schemas import StepEvent

_Number = TypeVar("_Number", int, float)


def _sum_optional(values: Iterable[_Number | None]) -> _Number | None:
    """Sum every non-`None` value in `values`; `None` if every value is `None` (never
    treated as 0 -- see module docstring on why an all-`None` run reports `None`, not 0)."""

    total: _Number | None = None
    for value in values:
        if value is None:
            continue
        total = value if total is None else total + value
    return total


@dataclass(frozen=True, slots=True)
class StepUsage:
    """One step's agent-call usage, summed across every round it ran this pipeline run."""

    step_name: str  # canonical name, e.g. "ReviewStep" -- see StepEvent.step_name
    usage: Usage


@dataclass(frozen=True, slots=True)
class PipelineRunReport:
    """A run's total LLM token usage, plus its per-step breakdown."""

    total_input_tokens: int | None
    total_output_tokens: int | None
    total_cache_creation_input_tokens: int | None
    total_cache_read_input_tokens: int | None
    total_cost_usd: float | None
    # Only steps that reported at least one non-None Usage field in any round, in
    # first-seen order -- a step that never called the agent has nothing to show.
    per_step: tuple[StepUsage, ...]


def build_run_report(events: Sequence[StepEvent]) -> PipelineRunReport:
    """Sum every `"completed"` event's `StepOutcome.usage` per step (across all its rounds),
    then across every step, into one `PipelineRunReport`. A run with no agent-usage data
    anywhere (e.g. one that failed before any agent call) reports all three totals as `None`
    and an empty `per_step`, not zeros."""

    usages_by_step: dict[str, list[Usage]] = {}
    order: list[str] = []
    for event in events:
        if event.status != "completed" or event.outcome is None or event.outcome.usage is None:
            continue
        if event.step_name not in usages_by_step:
            usages_by_step[event.step_name] = []
            order.append(event.step_name)
        usages_by_step[event.step_name].append(event.outcome.usage)

    per_step: list[StepUsage] = []
    for name in order:
        usages = usages_by_step[name]
        summed = Usage(
            input_tokens=_sum_optional(u.input_tokens for u in usages),
            output_tokens=_sum_optional(u.output_tokens for u in usages),
            cache_creation_input_tokens=_sum_optional(
                u.cache_creation_input_tokens for u in usages
            ),
            cache_read_input_tokens=_sum_optional(u.cache_read_input_tokens for u in usages),
            total_cost_usd=_sum_optional(u.total_cost_usd for u in usages),
        )
        if (
            summed.input_tokens is None
            and summed.output_tokens is None
            and summed.cache_creation_input_tokens is None
            and summed.cache_read_input_tokens is None
            and summed.total_cost_usd is None
        ):
            continue
        per_step.append(StepUsage(step_name=name, usage=summed))

    return PipelineRunReport(
        total_input_tokens=_sum_optional(su.usage.input_tokens for su in per_step),
        total_output_tokens=_sum_optional(su.usage.output_tokens for su in per_step),
        total_cache_creation_input_tokens=_sum_optional(
            su.usage.cache_creation_input_tokens for su in per_step
        ),
        total_cache_read_input_tokens=_sum_optional(
            su.usage.cache_read_input_tokens for su in per_step
        ),
        total_cost_usd=_sum_optional(su.usage.total_cost_usd for su in per_step),
        per_step=tuple(per_step),
    )


def _format_token_counts(
    input_tokens: int | None,
    cache_read_input_tokens: int | None,
    cache_creation_input_tokens: int | None,
    output_tokens: int | None,
) -> str | None:
    """Joins whichever of the four token figures are non-`None` with `" / "`, in a
    fixed reporting order (fresh input, cache read, cache write, output) -- cache reads
    and cache writes are billed at different rates than fresh input tokens, so they're
    kept as distinct segments rather than folded into "in"."""

    segments = [
        (input_tokens, "in"),
        (cache_read_input_tokens, "cache read"),
        (cache_creation_input_tokens, "cache write"),
        (output_tokens, "out"),
    ]
    parts = [f"{value:,} {label}" for value, label in segments if value is not None]
    return " / ".join(parts) if parts else None


def _format_usage(
    input_tokens: int | None,
    cache_read_input_tokens: int | None,
    cache_creation_input_tokens: int | None,
    output_tokens: int | None,
    total_cost_usd: float | None,
) -> str:
    """One usage line, e.g. `"8,000 in / 2,000 cache read / 500 cache write / 4,000 out
    ($0.0800)"` -- omits whichever figure is `None`. 4 decimal places on cost since this
    repo's real per-call costs are typically well under $1 (2 decimals would silently
    show "$0.00")."""

    tokens = _format_token_counts(
        input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens
    )
    cost = None if total_cost_usd is None else f"${total_cost_usd:.4f}"
    if tokens is not None and cost is not None:
        return f"{tokens} ({cost})"
    if tokens is not None:
        return tokens
    assert cost is not None  # a per_step entry always has at least one non-None field
    return cost


def format_run_report(
    report: PipelineRunReport, *, display_names: Mapping[str, str] | None = None
) -> str:
    """Render `report` as a multi-line block: a totals line, then one line per `per_step`
    entry, its `step_name` translated through `display_names` (a name with no entry renders
    as-is, mirroring `tui/state.py`'s `backfill`). `""` when `report.per_step` is empty."""

    if not report.per_step:
        return ""

    totals = _format_usage(
        report.total_input_tokens,
        report.total_cache_read_input_tokens,
        report.total_cache_creation_input_tokens,
        report.total_output_tokens,
        report.total_cost_usd,
    )
    lines = [f"Tokens used: {totals}"]
    for step_usage in report.per_step:
        display_name = (
            step_usage.step_name
            if display_names is None
            else display_names.get(step_usage.step_name, step_usage.step_name)
        )
        usage = step_usage.usage
        step_totals = _format_usage(
            usage.input_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
            usage.output_tokens,
            usage.total_cost_usd,
        )
        lines.append(f"  {display_name}: {step_totals}")
    return "\n".join(lines)
