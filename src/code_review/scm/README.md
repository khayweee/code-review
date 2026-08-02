# SCM

This package is the boundary between pipeline logic and a source-control hosting service.
It keeps GitHub and `gh` CLI details out of steps, so a PR step can request a host
operation without owning command construction, escaping, or host-specific failures.

## Subunits

| Subunit | Purpose | Input | Output | Status |
|---|---|---|---|---|
| `github.py` | Planned GitHub adapter that creates pull requests through `gh pr create`. | Repository identity, branch/base information, title, and body | Created PR metadata or a typed/actionable failure | Design stub only |
| `__init__.py` | Package boundary for SCM-facing exports. | — | No public wrapper exported yet | Scaffold only |

The planned adapter will send PR bodies through stdin (`--body-file -`) rather than a
shell argument, and will identify the repository explicitly rather than depending on the
current directory for GitHub context.

## Place in the complete pipeline

```text
Review + risk + test evidence
             |
             v
          PR step
       assembles title/body
             |
             v
       SCM GitHub adapter
             |
             v
          `gh` CLI ------> GitHub pull request
             |
             v
       PR metadata/error ------> StepOutcome
```

The separation is intentional:

- `steps/pr.py` owns what evidence belongs in the PR and the deterministic fallback when
  drafting fails.
- `scm/github.py` owns how that already-prepared PR is sent to GitHub.
- `pipeline` owns whether execution is allowed to reach PR creation at all.

No callable SCM API exists yet, so these inputs and outputs describe the milestone
contract rather than current runtime behavior. See the
[roadmap](../../../docs/ROADMAP.md) for implementation order.
