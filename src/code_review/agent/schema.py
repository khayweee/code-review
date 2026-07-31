"""Structured-output extraction and validation — Milestone 1 (see docs/ROADMAP.md).

Planned shape: one shared extractor tried in order — whole response as JSON, then fenced
```json``` blocks, then the last balanced `{...}` object — validated against a pydantic
model. Build once, reuse across every backend that doesn't natively support schema-
constrained output.
"""
