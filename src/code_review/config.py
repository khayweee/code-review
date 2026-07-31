"""Repo config loading: trusted-vs-descriptive field split.

Not built yet — see docs/ROADMAP.md milestone 9. When it lands: code-executing fields
(commands, agent choice) must load only from a pinned, freshly-fetched default-branch
commit, never from the pushed branch itself; purely descriptive fields (ignore patterns,
thresholds) can load from the pushed branch. Fail closed on any read/parse failure of a
trust-relevant field.
"""
