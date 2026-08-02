"""Correctness/alignment review + risk assessment — Milestone 5 (see docs/ROADMAP.md).

Single prompt, single schema: findings *and* required risk_level/risk_rationale fields
together — risk is not a separate step. Once explicit intent (milestone 3) exists, add an
intent-conformance clause: a change that removes/contradicts a REQUIRED criterion must be
flagged `ask-user` even if otherwise risk-clean, and only when intent is explicit, never
inferred.
"""
