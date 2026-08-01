---
name: create-gh-branch
description: Create a new git branch in this repo following standard naming convention. Use this whenever starting work on a GitHub issue, or whenever a new branch is about to be created for any reason. Trigger on phrases like "create a branch", "start a new branch", "let's work on issue N", or "I need a branch for X", even if the user doesn't invoke it by name.
---

# Create GitHub Branch

Create a correctly named branch for new work in this repo, and link it to its GitHub
issue when one exists so merging the PR auto-closes the issue (see `AGENTS.md` → Issue tracking).

## Naming structure

```
<category>/<issue-number>-<short-description>
```

- **category** - one of the prefixes below.
- **issue-number** - the GitHub issue this branch closes. Omit only when there is
  genuinely no issue (e.g. a trivial docs typo fix) - prefer creating an issue first over
  skipping the number, since untracked branches are how issue state drifts.
- **short-description** - 2-4 lowercase words, hyphen-separated, summarizing the change.

### Category prefixes

| Prefix      | Use for                                                          |
| ----------- | ---------------------------------------------------------------- |
| `feature/`  | New features or enhancements                                     |
| `bugfix/`   | Standard bug fixes tied to an issue                              |
| `hotfix/`   | Urgent production fixes applied directly against `main`          |
| `docs/`     | Documentation-only changes                                       |
| `refactor/` | Restructuring code without changing behavior                     |
| `test/`     | Adding or fixing tests                                           |
| `chore/`    | Everything else with no user-facing behavior (CI, deps, tooling) |

### Core rules

- **Lowercase only** - avoids case-conflicts on case-insensitive filesystems.
- **Hyphens, not underscores or spaces** - `-` between words, never `_`.
- **No continuous hyphens** (`--`) and no leading/trailing hyphen.
- **Include the issue number** whenever an issue exists, so the branch is traceable.
- **Keep the description short** - 2-4 words is enough; this is a label, not a summary.
- **No author names** - git already tracks who created the branch; a name prefix is
  redundant and this repo doesn't use per-author namespacing.

### Good vs. bad

| Good                         | Bad                   | Why                                     |
| ---------------------------- | --------------------- | --------------------------------------- |
| `feature/892-user-profile`   | `Feature_UserProfile` | Uppercase, underscores, no issue number |
| `bugfix/341-payment-timeout` | `fix-bug`             | Too vague, no issue number              |
| `docs/api-endpoints`         | `john-docs-update`    | Author name baked into the branch       |

## Process

1. **Determine the category.** If the user names one (feature, bug, hotfix, docs,
   refactor, test, chore), map it to the matching prefix. Otherwise infer it from what
   the work actually does - a GitHub issue's labels (`bug`, `enhancement`, `documentation`,
   ...) are a good signal if one exists.

2. **Determine the issue number.** If the user references a GitHub issue (`#N`, a URL, or
   "issue N"), use that number. If they describe work that isn't yet tracked, ask whether
   to create an issue first rather than silently skipping the number - see
   `AGENTS.md` → Issue tracking for this repo's issue conventions.

3. **Write the short description.** 2-4 lowercase, hyphenated words from the issue title
   or the work itself - trim filler words, don't just lowercase the whole issue title
   verbatim if it's long.

4. **Create the branch.**
   - **Tied to a GitHub issue** - use `gh issue develop` so the branch is linked to the
     issue (merging the eventual PR then auto-closes it):
     ```
     gh issue develop <issue-number> --name <category>/<issue-number>-<short-description> --checkout
     ```
   - **Not tied to an issue** (rare - e.g. a trivial docs fix): create it directly:
     ```
     git checkout -b <category>/<short-description>
     ```

5. **Confirm** the branch name out loud before or right after creating it, so the user can
   catch a miscategorization early.

## Commit messages

This repo uses [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<scope>): <summary>` (e.g. `feat(agent): survive chatty agent output`,
`fix(ci.yml): setup-uv version`). `<type>` should match the branch category
(`feature`→`feat`, `bugfix`/`hotfix`→`fix`, `docs`→`docs`, `refactor`→`refactor`,
`test`→`test`, `chore`→`chore`). `<scope>` is the package or file most affected. Keep the
summary imperative and lowercase, no trailing period.
