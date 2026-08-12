"""The pipeline steps: intent, rebase, review, test_sufficiency, pr.

No re-exports here (unlike `agent/`/`pipeline/`'s `__init__.py`) -- deliberately, since
`steps.registry` imports every step module including `steps.pr`, which imports
`scm.github`, which imports `steps.gitutils`. An eager `from code_review.steps.registry
import ...` here would make importing *any* single submodule of this package (e.g. `import
code_review.steps.gitutils` from `scm/github.py`) first run this file, which would import
`registry`, which would import `pr`, which would import `scm.github` again -- a circular
import. Every real caller already imports what it needs from the specific submodule
(`code_review.steps.registry`, `code_review.steps.pr`, ...) rather than this package's own
namespace, so nothing is lost by not re-exporting here.
"""
