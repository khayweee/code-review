"""Intent detection — Milestone 3 (see docs/ROADMAP.md).

v1 shortcut: require `--intent` explicitly; skip transcript inference. Write one
sanitize-and-wrap function (redact secrets, strip adversarial delimiter shapes, wrap in a
clearly-marked block) and reuse it at every prompt site that embeds intent text.
Provenance (explicit vs. inferred) changes the framing of authority, never whether
sanitization applies. Transcript-based inference is v2 (milestone 10).
"""
