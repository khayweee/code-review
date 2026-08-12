# AGENTS.md — src/code_review/scm/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

## github.py

`gh` CLI wrapper: find-or-create/update a PR for a branch (Milestone 8, issue #119).
`steps/pr.py`'s `PRStep` is the sole caller.

- Every mutating call passes an explicit `--repo owner/repo` rather than letting `gh` infer
  repo context from cwd, per the module's own original design note. `resolve_repo_slug`
  derives that slug from `cwd`'s `origin` remote (`git remote get-url origin`, via
  `steps.gitutils.run_git` - not a `gh` call, so resolving it never needs `gh auth`) and
  `parse_repo_slug` (pure, no subprocess) parses both the SSH
  (`git@host:owner/repo.git`) and HTTPS (`https://host/owner/repo.git`) remote URL forms.
- `scm/` importing `steps.gitutils.run_git` here is a deliberate, narrow exception to this
  package otherwise having no dependency on `steps/` - see `steps/AGENTS.md`'s
  `gitutils.py` section. It exists solely because `resolve_repo_slug` needs the exact same
  non-blocking, activity-reporting `git` subprocess plumbing `gitutils.py` already
  centralizes; it is not a license for `scm/` to depend on `steps/` more broadly.
- `find_pull_request_for_branch` (`gh pr view <branch> --repo <slug> --json
  number,url,title,body`) distinguishes "no PR exists for this branch" (real `gh`'s own
  "no pull requests found for branch ..." message on stderr, exit 1) from a genuine command
  failure (bad repo, auth, network) - the latter raises `GhCommandError` rather than being
  silently treated as "no PR". Verified against a real, authenticated `gh` once during
  development (never in the test suite, which always points `gh_executable` at a fake
  script per this ticket's own testing decision - see `tests/scm/fakes/gh_fake.py`).
- `create_pull_request` (`gh pr create --repo <slug> --head <branch> --base
  <default_branch> --title <title> --body-file -`) parses the new PR's number back out of
  the URL `gh pr create` prints on success (its only stdout on success - no `--json` flag
  exists for `create`). `update_pull_request` (`gh pr edit <number> --repo <slug> --title
  <title> --body-file -`) doesn't need to parse anything back, since the caller already
  knows the number.
- Every call pipes its body over stdin via `--body-file -`, never `--body`, to avoid
  shell-escaping/length limits on a body that can carry arbitrarily long git-diff output.
- `gh_executable: str | Path = "gh"` is threaded from `PRStep.gh_executable`, mirroring
  `steps/review.py`'s `RunOpts.executable` test seam - swapped for a fake script (never a
  Python-level mock of the `gh` call itself) in every test.
- No PR-body byte-budget/truncation rule yet - deferred to #122, which also owns
  screenshot/video artifacts. Record it here once #122 lands.
