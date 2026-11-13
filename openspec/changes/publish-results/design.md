# Design

## Context

See proposal.md for why. Today the executor accumulates completed tasks on the integration
branch `ceo/<change>/integration` in CEO's own clone (`/var/lib/ceo/plans` on the box), and the
loop (`ceo/run.py`) announces a completed plan once, and handles completion before the budget gate,
so a plan whose last task reached the cap is still completed. Each run records the check it ran
(`runs.check_command`). Nothing is written to GitHub. CEO runs as
the `ceo` user; workers run as `ceo-worker` and never see CEO's settings. GitHub is already read
through `ceo/approval.py`, and outbound HTTP goes through `open_url` in `ceo/notify.py` (https
unless loopback, no redirects).

## Goals / Non-Goals

**Goals:**
- Publish a completed plan's integration branch and one pull request with its evidence.
- Keep the publishing token secret, and never write `main`.
- One small interface, so other hosts can be added later.

**Non-Goals:**
- Merging: the engineer merges (that is their approval of the result).
- Publishing partial progress before every task is completed.
- Implementations for hosts other than GitHub (only the interface and "none").
- GitHub App authentication (a later option behind the same interface).

## Constraints

- C1. Standard library only, plus what CEO already uses: GitHub's REST API through `open_url`, git through `subprocess` with an argument list (no shell).
- C2. The token never appears in a command line, a remote URL, git configuration, the ledger, chat or logs. It is read from CEO's environment and given to git only through the environment of the single git process that needs it.
- C3. Only branches whose name starts with `ceo/` are ever pushed; anything else is refused before git runs.
- C4. Publishing runs in the orchestrator as `ceo`, never in the worker sandbox; the worker never has the token.
- C5. HTTPS only, no redirects followed: all GitHub calls go through `open_url` and `check_transport`.
- C6. At most one publish per plan per approved commit, recorded in the ledger; a failed publish is retried on a later cycle.
- C7. The bot that publishes can't clear judgment gates: its login must not be one of the engineer's GitHub logins (already refused by `Engineer.from_env` via `CEO_GITHUB_BOT`).
- C8. The GitHub publisher can only be used with isolated workers: if `CEO_PUBLISHER` is `github` and `CEO_WORKER_USER` is empty (workers would run as CEO and could read its environment and settings), CEO refuses to start.

## Failure Modes

| ID | Failure | Mitigation |
|----|---------|------------|
| F1 | The token leaks through the process list, a remote URL or an error message | C2, D4; tests in 1.1 assert it never appears |
| F2 | CEO pushes to `main` or overwrites someone else's work | C3, D5 |
| F3 | A worker reads or sends the token | C4, C8 |
| F4 | Duplicate pull requests, or a push every cycle | C6, D3 |
| F5 | A publishing failure blocks the loop or other plans | D3 |
| F6 | The pull request's description misleads the engineer | D2; tests in 1.1 check it against the ledger |
| F7 | A redirect sends the token to another host | C5 |
| F8 | The push succeeds but the pull request call fails, and the retry's push is rejected | D5 |
| F9 | CEO stops after a push but before recording it, so its lease target is stale | D5 |

## Decisions

- D1. A publisher interface with one method that publishes a completed plan and returns where it went (a URL or nothing). Implementations: `none` (the default: keep results on the box) and `github`, chosen by `CEO_PUBLISHER`; an unknown value stops CEO at startup.
  Rejected: GitHub calls written into the loop (the engineer asked for a design that can serve non-GitHub hosts); a plugin system with entry points (more than the POC needs).
- D2. The pull request's description is built only from the ledger: the approved commit, each task's commit, check (`runs.check_command`) and exit code, model and cost, and every gate with its decision, decider and route. Nothing the worker wrote goes into it.
  Rejected: letting a worker write the description (unchecked claims, in exactly the place the engineer judges from).
- D3. Publishing is its own step in the loop for every completed plan, with its own record per approved commit: `publish.done` (with the approved commit and URL) once, only when a real publisher published; `publish.skipped` when the `none` publisher is configured, which doesn't stop a publisher configured later; or `publish.failed` with the error (token removed). It is checked independently of the completion announcement, so a failed publish is retried every cycle until it succeeds; it never fails the plan or stops other plans.
  Rejected: publishing from the executor after each task (partial results and many pushes).
- D4. `git push` over HTTPS with credentials supplied by a small `GIT_ASKPASS` helper that prints the token from an environment variable set only for that git process; the remote URL carries no credentials, and `GIT_TERMINAL_PROMPT=0` stops git from prompting.
  Rejected: the token in the URL (stored in git configuration, echoed in errors); `http.extraHeader` on the command line (visible in the process list); a credential store file (the token at rest in a second place).
- D5. After every successful push CEO records `publish.pushed` (branch and commit). The integration branch is pushed with `--force-with-lease` against the commit CEO last pushed (from those records), or against "the branch must not exist" the first time: a re-approved plan can be republished, a retry after a failed pull request call isn't rejected (F8), and work someone else pushed to that branch is never overwritten. Before pushing, CEO reads the remote branch's tip: if it already is the commit CEO is about to push (the push succeeded but CEO stopped before recording it), CEO records `publish.pushed` and skips the push; if it is a commit CEO never recorded as pushed, CEO doesn't push, and records `publish.failed` asking the engineer to look, rather than adopting or overwriting it (F9).
  Rejected: a plain force push (could overwrite other work); never forcing (a plan re-approved at a new commit could not be republished).

## Risks / Trade-offs

- [The token has more permissions than needed] → docs/hosting.md lists exactly what's needed: a fine-grained token for this one repository, with contents and pull requests read/write.
- [GitHub's API changes] → requests pin the API version header, as `ceo/approval.py` does.
- [The PR is large when a plan has many tasks] → acceptable for the POC; the engineer still reviews one PR per plan.
