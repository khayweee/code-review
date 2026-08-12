"""Repo config loading: trusted-vs-descriptive field split. Not built yet.

Planned: code-executing fields (commands, agent choice) load only from a pinned,
freshly-fetched default-branch commit, never the pushed branch; purely descriptive fields
(ignore patterns, thresholds) can load from the pushed branch. Fail closed on any
read/parse failure of a trust-relevant field.
"""
