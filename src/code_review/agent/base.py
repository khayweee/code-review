"""Agent protocol, RunOpts, and Result — Milestone 1 (see docs/ROADMAP.md).

Planned shape: an `Agent` Protocol/ABC with `run(opts: RunOpts) -> Result` and `close()`.
`RunOpts` carries prompt/cwd/json-schema/session; `Result` carries output/text/usage with
`Optional[...]` fields meaning "not reported," never a fabricated zero. Every pipeline
step is written only against this abstraction, never against a specific backend.
"""
