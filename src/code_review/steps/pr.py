"""PR creation with evidence — Milestone 8 (see docs/ROADMAP.md).

Split: the agent drafts only a title and a "What Changed" section; everything else
(Intent, Risk, Pipeline summary) is assembled deterministically from data the pipeline
already has. Ship the deterministic fallback path (title from a template, body from
`git diff --name-status`) before wiring the agent-drafted path — it's the safety net for
when the drafting call fails outright.
"""
