"""Prevent pytest from ever collecting scripts in this directory as test modules.

Every file here is a fake CLI fixture invoked as a subprocess by `ClaudeCLI` (see e.g.
`tests/steps/test_review.py`), never imported by pytest itself. Some of these scripts are
named `test_*.py` (e.g. `test_sufficiency_output_clean.py`, matching this step's own
module name), which otherwise matches pytest's default `python_files` glob and gets
imported at collection time -- collection then fails outright, since each of these scripts
calls `sys.stdin.read()` unconditionally, which pytest's stdin capture rejects.
"""

from __future__ import annotations

collect_ignore_glob = ["*.py"]
