# The gate model (and why we're not building it yet)

Background reading, not a plan. This explains how the Go tool this project learns from
(`no-mistakes`) *triggers* its pipeline — the "bare mirror + git hooks" design — and why
this project deliberately starts with a plain CLI command instead. Read it when deciding
whether to build a trigger layer at all (post-milestone-7); skip it otherwise.

The short reason it matters: the trigger mechanism is **orthogonal to the pipeline**, and
choosing the simple one deletes about six subsystems' worth of work.

## The one-sentence version

`no-mistakes` puts a **fake local GitHub** on your laptop, makes you push there instead of
the real GitHub, and only forwards your code to the real GitHub after an AI pipeline has
reviewed it.

Everything else is detail on top of that.

## What a "bare repo" is, and why there's one on your laptop

A normal git repo is two things: your **working files**, plus a hidden `.git/` folder
holding the actual commit database.

A **bare repo** is just the second half — the database, no working files. You can't `cd`
into it and edit code; there's nothing to edit. Its only job is to *receive pushes*.

The punchline that makes it click:

> **GitHub is a bare repo.** When you `git push origin`, the thing on the other end of
> that push is a bare repo sitting on GitHub's servers.

So when `no-mistakes` creates a "bare mirror," it's creating a private, GitHub-shaped
thing at `~/.no-mistakes/repos/<hash>/` on your own machine. It's called a *mirror*
because it holds copies of the same commits as your repo.

**Why does it need one?** Because of a hard git rule: hooks that fire *when a push
arrives* only run on the **receiving** side. To get "run my AI pipeline when a branch is
pushed," you need a repo that receives pushes, on a machine you control. You don't control
GitHub's servers. So: put one on your laptop.

After `no-mistakes init`, the working repo has **two remotes**:

```
your working repo
├── origin        → https://github.com/you/project.git    (the real one)
└── no-mistakes   → ~/.no-mistakes/repos/a3f2b8/          (the local gate)
```

And critically, **the gate has its own `origin` pointing at the real GitHub**, holding the
credentials (`internal/gate/gate.go`, `provisionGate`).

That's the actual insight — the gate isn't a sidecar that watches you. It's a **checkpoint
in the middle of the road**:

```
you  ──push──>  gate  ──(only if pipeline passes)──>  GitHub
```

You never push to GitHub yourself. The daemon owns that second hop. Unreviewed code can't
reach GitHub because *nobody ever sent it there*.

## The two hooks — a bouncer and a doorbell

A bare repo can run scripts when a push lands. `no-mistakes` installs two
(`internal/git/hook.go`), and they have completely different jobs.

### `pre-receive` — the bouncer (fail-closed)

Runs **before** git writes anything. If it exits non-zero, **the push is rejected** and the
gate is unchanged.

The part that surprises people: **it is not reviewing your code.** It's checking *who is
pushing*. It calls `daemon admit-push`, which verifies the pushing process's **ancestry**.

Why? Because `no-mistakes` runs AI agents as subprocesses, and those agents have shell
access. An agent could decide on its own to run `git push no-mistakes` — kicking off a
nested pipeline run or sneaking past a guard. `pre-receive` is the wall against that:
*"is this push coming from inside the house?"* If yes, refuse.

It authenticates via process ancestry (`internal/gatecontext`) rather than an environment
variable, because a subprocess inherits and can forge env vars — but it can't lie about
its own parent process.

### `post-receive` — the doorbell (fail-open, but loud)

Runs **after** the refs are already updated. **Git ignores its exit code entirely** — it
physically cannot reject the push. It calls `daemon notify-push` to start the pipeline,
then prints a banner.

Since it can't fail the push, failures get written to `notify-push.log` *and* stderr — the
code comment says this is "so a maintainer isn't confused by a run that silently never
started."

**The pattern worth stealing:** the point that *authorizes* an action fails closed. The
point that merely *announces* something already happened fails open, but noisily. Know
which of your integration points is which.

## The whole flow, from the developer's seat

```
1. You work on a feature branch — totally normal git, nothing special
     git checkout -b feat/thing
     git commit -am "add thing"

2. git push no-mistakes            ← the ONLY unusual step (note: not origin)

3. pre-receive: "are you a real human's shell?" → yes → allowed
   post-receive: "hey daemon, branch landed" → returns in ~1 second
   Your terminal is free again. Banner prints: "* Pipeline started"

4. Daemon (background):
   - locks the branch, cancels any older run on it
   - creates a git WORKTREE off the bare repo  ← your working dir is untouched,
                                                  you can keep coding
   - loads config, builds one Agent

5. Executor runs 9 steps, fixed order, in a detached copy of your code:
     Intent    → what was the developer trying to do?
     Rebase    → get onto latest main
     Review    → correctness + risk       ← may auto-fix, or PARK for you
     Test      → is there enough evidence?
     Document  → docs updated?
     Lint      → formatting/vet
     Push      → NOW it goes to real GitHub
     PR        → gh pr create, body assembled from pipeline data
     CI        → watch the run, auto-fix failures

6. You run `no-mistakes` to watch the TUI, approve anything parked,
   and a PR appears when it's done.
```

Two details that matter more than they look:

- **The worktree** (step 4) means the agent edits a *separate checkout*, not your files.
  You can keep working while the pipeline runs.
- **Push is step 7, not step 1.** Steps 1-6 all happen before the code has touched GitHub
  at all.

## Why bother with all this machinery

1. **Enforcement is physical, not procedural.** Not "please remember to review" — the code
   simply has no path to GitHub except through the pipeline.
2. **Your push is instant.** The slow LLM work happens in a background daemon; your
   terminal isn't held hostage for ten minutes.
3. **Git-native trigger.** No new verb to learn or wrapper script to remember.
4. **Your working directory is never touched.**

## What this means for this project

The design study's own chapter 00 is explicit:

> "For a first Python version, you don't need a bare-repo/git-hook trigger at all."

**The gate is a delivery mechanism, completely orthogonal to the pipeline itself.** All
nine steps work identically whether they were triggered by a git hook, a CLI command, or a
cron job.

The consequence worth internalizing: **the gate is why `no-mistakes` needs a daemon at
all.** A push must return in a second, so the work has to go somewhere async, so you need a
long-lived background process, a socket, an IPC protocol, run-state in SQLite, singleton
locking, crash recovery, and so on.

This project's `code-review review <branch> --intent "..."` is a **blocking foreground
command**. That single choice deletes the daemon, the IPC layer, the bare repo, the hooks,
the admission boundary, and the run-state machine — six subsystems — while keeping 100% of
the actual review intelligence.

That's why [`ROADMAP.md`](ROADMAP.md) puts the CLI trigger at milestone 2 and defers all of
this. If a gate is ever wanted, it's a genuinely drop-in upgrade: a hook script that shells
out to the CLI that already exists. A GitHub Action is an equally valid alternative that
skips the bare repo entirely.
