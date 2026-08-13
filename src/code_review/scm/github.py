"""GitHub wrapper via the `gh` CLI: find-or-create/update a PR for a branch.

Every mutating call passes an explicit `--repo owner/repo` (resolved from `cwd`'s `origin`
remote, never inferred by `gh` from cwd) and a PR body via `--body-file -` (piped over
stdin, avoiding shell-escaping/length limits). `gh_executable` is threaded from the caller
(`steps/pr.py`'s `PRStep.gh_executable` field), mirroring `steps/review.py`'s
`RunOpts.executable` test seam -- swapped to a fake script in tests.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from code_review.pipeline.step import current_activity_reporter, report_activity
from code_review.steps.gitutils import run_git

_SSH_REMOTE = re.compile(r"^git@[^:]+:(?P<slug>.+?)(?:\.git)?/?$")
_HTTPS_REMOTE = re.compile(r"^https://[^/]+/(?P<slug>.+?)(?:\.git)?/?$")


def parse_repo_slug(remote_url: str) -> str | None:
    """Parse an `origin` remote URL into `owner/repo`, or `None` if it matches neither the
    SSH (`git@host:owner/repo.git`) nor HTTPS (`https://host/owner/repo.git`) form. Pure,
    no subprocess -- independently testable against hand-written URLs.
    """

    for pattern in (_SSH_REMOTE, _HTTPS_REMOTE):
        match = pattern.match(remote_url.strip())
        if match:
            return match.group("slug")
    return None


async def resolve_repo_slug(cwd: Path) -> str | None:
    """Resolve `cwd`'s `origin` remote to an `owner/repo` slug via `git remote get-url
    origin` (`gitutils.run_git`, not a `gh` call -- keeps this fast and independent of `gh
    auth`). `None` if the remote is missing or its URL doesn't parse.
    """

    result = await run_git(["remote", "get-url", "origin"], cwd)
    if result.returncode != 0:
        return None
    return parse_repo_slug(result.stdout.strip())


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A PR as reported by `gh`: enough to find-or-update it again."""

    number: int
    url: str
    title: str
    body: str


class GhCommandError(RuntimeError):
    """A `gh` subprocess exited nonzero for a reason other than "no PR exists yet"."""


def _gh_activity_label(args: list[str]) -> str:
    subcommand = f"{args[0]} {args[1]}" if len(args) > 1 else (args[0] if args else "")
    return f"gh {subcommand}".rstrip()


async def _run_gh(
    args: list[str], cwd: Path, *, gh_executable: str | Path, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a `gh` subprocess in `cwd`, capturing output as text. Never raises on a nonzero
    exit -- callers inspect `.returncode` themselves, mirroring `gitutils.run_git`.
    """

    label = _gh_activity_label(args)
    async with report_activity(current_activity_reporter.get(), label) as activity:
        process = await asyncio.create_subprocess_exec(
            str(gh_executable),
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
        stdout_bytes, stderr_bytes = await process.communicate(stdin_bytes)
        # `communicate()` always waits for the process to exit before returning.
        assert process.returncode is not None
        if process.returncode != 0:
            activity.fail(f"exit {process.returncode}")
        return subprocess.CompletedProcess(
            args=[str(gh_executable), *args],
            returncode=process.returncode,
            stdout=stdout_bytes.decode("utf-8"),
            stderr=stderr_bytes.decode("utf-8"),
        )


def _pull_request_from_json(payload: str) -> PullRequest:
    data = json.loads(payload)
    return PullRequest(
        number=data["number"], url=data["url"], title=data["title"], body=data["body"]
    )


async def find_pull_request_for_branch(
    branch: str, repo_slug: str, cwd: Path, *, gh_executable: str | Path = "gh"
) -> PullRequest | None:
    """Find the open PR for `branch` in `repo_slug`, or `None` if none exists yet.

    Distinguishes "no PR exists" (`gh pr view`'s own "no pull requests found" message) from
    a genuine command failure (bad repo, auth, network) -- the latter raises `GhCommandError`
    rather than being silently treated as "no PR".
    """

    result = await _run_gh(
        ["pr", "view", branch, "--repo", repo_slug, "--json", "number,url,title,body"],
        cwd,
        gh_executable=gh_executable,
    )
    if result.returncode == 0:
        return _pull_request_from_json(result.stdout)
    if "no pull requests found" in result.stderr.lower():
        return None
    raise GhCommandError(
        f"gh pr view {branch!r} --repo {repo_slug} failed: {result.stderr.strip()}"
    )


async def create_pull_request(
    *,
    repo_slug: str,
    head: str,
    base: str,
    title: str,
    body: str,
    cwd: Path,
    gh_executable: str | Path = "gh",
) -> PullRequest:
    """Open a new PR for `head` against `base` in `repo_slug`, piping `body` via
    `--body-file -`. Raises `GhCommandError` on a nonzero exit.
    """

    result = await _run_gh(
        [
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body-file",
            "-",
        ],
        cwd,
        gh_executable=gh_executable,
        stdin=body,
    )
    if result.returncode != 0:
        raise GhCommandError(f"gh pr create --repo {repo_slug} failed: {result.stderr.strip()}")

    url = result.stdout.strip()
    match = re.search(r"/pull/(?P<number>\d+)\s*$", url)
    if match is None:
        raise GhCommandError(f"gh pr create --repo {repo_slug} printed an unexpected URL: {url!r}")
    return PullRequest(number=int(match.group("number")), url=url, title=title, body=body)


async def update_pull_request(
    number: int,
    *,
    repo_slug: str,
    title: str,
    body: str,
    cwd: Path,
    gh_executable: str | Path = "gh",
) -> PullRequest:
    """Edit PR `number` in `repo_slug`'s title/body in place, piping `body` via
    `--body-file -`. Raises `GhCommandError` on a nonzero exit.
    """

    result = await _run_gh(
        [
            "pr",
            "edit",
            str(number),
            "--repo",
            repo_slug,
            "--title",
            title,
            "--body-file",
            "-",
        ],
        cwd,
        gh_executable=gh_executable,
        stdin=body,
    )
    if result.returncode != 0:
        raise GhCommandError(
            f"gh pr edit {number} --repo {repo_slug} failed: {result.stderr.strip()}"
        )
    return PullRequest(
        number=number, url=f"https://github.com/{repo_slug}/pull/{number}", title=title, body=body
    )
