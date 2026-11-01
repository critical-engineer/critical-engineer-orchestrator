---
id: ceo-bootstrap
title: Critical Engineer Orchestrator — bootstrap
status: drafting          # informational only; approval = this file merged to protected main
owner: dries
created: 2026-11-01
---

# Critical Engineer Orchestrator (CEO): bootstrap plan

## 1. Problem

AI has made expression (writing code) cheap. Judgment hasn't become cheaper: deciding what to build, why, and whether the result actually achieves it.
Existing agent frameworks make the *ticket* the unit of coordination and approval. Some of them (e.g. Paperclip) have real approval and budget controls, but the reasoning behind the work (constraints, failure modes, decisions) has no first-class home.
The work drifts from its reasoning and nobody can answer "why is it like this?"

CEO makes the *plan* the unit of coordination. It's a small, auditable orchestrator. Agents carry out work against an approved plan, the orchestrator checks the results, and the human owns every decision that matters, without having to sit at a terminal.

## 2. Goals / non-goals

**Goals (v1)**
- G1. A plan in a git repo is the single source of engineering intent. Approval is explicit, attributable and tied to a commit.
- G2. Async human interface. Plans are edited and approved on GitHub; questions and notifications go through Signal. No tmux babysitting.
- G3. Tasks complete only when an orchestrator-run check passes, with the evidence stored.
- G4. Workers are contained by the environment (OS user, network), not by prompt or blocklist.
- G5. Total model spend ≤ $20/month, enforced.
- G6. Readable enough to serve as a reference implementation for criticalengineer.ai.

**Non-goals (v1)**
- Running tasks in parallel. Execution is sequential, on purpose.
- Multiple users or a web UI.
- Building our own coding agent. We call existing CLIs.
- Being general-purpose. The first real user is the media-box project.

## 3. Constraints

- C1. Runs on media-box: i5-8500T, 16 GB, Ubuntu 26.04 Desktop, no GPU. All inference goes through remote APIs.
- C2. Python 3.11+, stdlib plus PyYAML. Every additional dependency needs a recorded decision. Exception (D10): the OpenSpec CLI (Node 22.12+), pinned, called as a subprocess, and relied on only for its file format and `--json` output (D10).
- C3. Model access through OpenRouter, with a hard credit limit on the key (backstop) and per-run cost tracked in the ledger.
- C4. Workers never hold the GitHub or Signal credentials, and can't write the ledger or the plan.
- C5. Workers have no sudo, no Docker group membership, no access to `/home/dries` or `/media`, no access to the home network, and no access to the Signal API.
- C6. The approved plan is read-only during execution. Any change goes through a PR and re-approval.
- C7. The ledger is the only store of execution status. No status is kept in the plan file. Under D10, the `tasks.md` checkboxes are written only by the orchestrator, at archive time, after checks pass. Agents never tick them; an edit to the approved file on `main` counts as a plan change (F3).
- C8. Every decision a human makes through Signal is saved in the ledger. Decisions that change the plan become PRs.
- C9. Changes to the gate code (approval, check, permission logic) always need human review, including when CEO is building itself.

## 4. Failure modes

| ID | Failure | Mitigation |
|----|---------|------------|
| F1 | Worker exits 0 without having done the task | G3: orchestrator runs the task's `check`; the exit code is ignored for completion |
| F2 | Worker edits the plan or self-approves | C4/C6: plan outside the worker's workspace; approval = protected-main SHA |
| F3 | Plan edited after approval | Orchestrator compares main HEAD with the approved SHA; on mismatch it halts and re-gates |
| F4 | Signal message spoofed or injected | Accept only from an allowlisted sender number; answers are data, never instructions |
| F5 | Decisions lost in chat | C8 |
| F6 | Runaway spend | OpenRouter key limit, per-task attempt limit, cost per run in the ledger |
| F7 | Worker reads personal data or messages | C5 plus a separate Signal bot number (see OQ1) |
| F8 | Self-build weakens its own gates | C9: protected paths in CODEOWNERS / review rule |
| F9 | Task IDs shift when the plan is edited | Explicit `X.Y` labels parsed by CEO, never OpenSpec's positional JSON `id` (T0 R4); deleted IDs are tombstoned in the ledger, never reused |
| F10 | A cheap model's plan is wrong and nobody notices | Critic review of the plan, then human approval, before any execution |

## 5. Decisions

- D1. **Plan = Markdown in git; approval = merge to protected `main`.**
  Rejected: a YAML `status: approved` field, which anyone who can write the file can set; a hash recorded by a local `ceo approve` command, which needs a terminal.
- D2. **Ledger = SQLite**, owned by the `ceo` user. Rejected: Postgres (unneeded at this scale); graph-framework state (a second copy of reality).
- D3. **Orchestrator = a plain Python loop.** Rejected: LangGraph (extra abstraction that hides the state machine).
- D4. **Workers = existing CLIs, called headless.** Default `opencode run --format json` (OpenRouter). Optional: `claude -p`.
  Rejected: our own coding agent (not the new part); OpenHands (strong sandbox, but centred on conversations and a heavier setup).
- D5. **Privilege separation:** orchestrator runs as `ceo`, workers as `ceo-worker` via a single sudoers rule. nftables blocks `ceo-worker` from home-network ranges and from local port 9922.
  Rejected: a single user (workers could tamper with the ledger and credentials); a VM (heavier; revisit if workers need Docker).
- D6. **Human interface:** GitHub for durable artefacts (plan, plan-change PRs, critic reports); Signal for notifications and short answers. Both are polled outbound, with no inbound ports.
- D7. **Elevated actions are ledger requests,** not interactive prompts. They sit as `blocked:needs-approval` until approved.
- D8. **Status comes from artefacts:** each task runs on a branch with one commit per task. The ledger stores the commit SHA, check output and cost.
- D9. **Paperclip isn't used,** because its unit of coordination is the ticket. Ideas borrowed from it: budgets, atomic task checkout, an audit log.
- D10. **(Accepted 2026-11-01 after the T0 spike; see docs/spikes/T0-openspec.md) OpenSpec is the plan format; CEO is the approval and execution layer.** CEO additionally owns: stable task IDs parsed from `X.Y` labels (OpenSpec's JSON IDs are positional), a CE validator on top of `openspec validate` (which doesn't check custom sections or `ceo.yaml`), and telemetry off. A custom `critical-engineer` schema forked from `spec-driven` adds constraints, failure modes and rejected alternatives, plus a CEO-owned per-task file (check / depends_on / blast_radius keyed by task ID). Archive merges deltas into living specs, so the record of how the system works and why outlives each change.
  Rejected: our own PLAN.md format. It works, but it goes stale after execution and has to reinvent validation.
  Risks: OpenSpec moves fast (its repo docs are marked partly legacy; custom schemas are partly experimental), so pin the version; there's a status-location conflict, resolved by C7.

## 6. Plan file format (v1)

Each task is a level-3 heading with a stable ID, followed by fenced YAML:

~~~~markdown
### T3: Plan parser
```yaml
depends_on: [T1]
blast_radius: low        # low | medium | high (high → critic review of output + human sign-off)
check: python -m pytest tests/test_parser.py
```
Description in prose.
~~~~

## 7. Tasks

### Phase 0: manual (the human is the orchestrator)

### T0: OpenSpec spike
```yaml
depends_on: []
blast_radius: low
check: test -f docs/spikes/T0-openspec.md
```
Pinned OpenSpec; fork `spec-driven` into `critical-engineer`; express this plan as an OpenSpec change; record what `validate/show/status/list --json` return (task-level detail? stable shape?). Outcome: accept or reject D10, and re-scope T1/T3 to match.

### T1: Repo skeleton and plan schema
```yaml
depends_on: [T0]
blast_radius: low
check: python3 -c "import ceo" && OPENSPEC_TELEMETRY=0 tools/node_modules/.bin/openspec schema validate critical-engineer
```
Rescoped by T0. Git repo with: the `critical-engineer` OpenSpec schema and templates moved in from the spike; OpenSpec pinned in `tools/` (exact version, lockfile), telemetry off; an empty `ceo` Python package; Apache 2.0 licence (code) / CC BY-SA 4.0 (docs, matching the essay). Published as github.com/critical-engineer/critical-engineer-orchestrator; `.github/CODEOWNERS` assigns review to the maintainer (C9).

### T2: Ledger schema
```yaml
depends_on: [T1]
blast_radius: medium
check: python -m pytest tests/test_ledger.py
```
Tables: plans (repo, approved_sha), tasks (id, plan_id, status, attempts, tombstoned), runs (task_id, worker, commit_sha, check_exit, check_output, cost_usd), questions (qid, task_id, text, answer, answered_at), events (append-only audit).

### T3: Plan parser
```yaml
depends_on: [T1]
blast_radius: low
check: python -m pytest tests/test_parser.py
```
Rescoped by T0. (a) Task-label parser: reads `X.Y` labels from `tasks.md` (or from `openspec instructions apply --json` descriptions), rejects duplicate or missing labels. (b) CE validator, run after `openspec validate --strict`: design has Constraints / Failure Modes / Decisions sections, every decision lists rejected alternatives, `ceo.yaml` keys match task labels one-to-one, every task has a `check`. Fixtures cover malformed changes.

### T4: Plan → ledger sync
```yaml
depends_on: [T2, T3]
blast_radius: medium
check: python -m pytest tests/test_sync.py
```
Inserts new tasks, tombstones removed ones, flags changed tasks that have already run (F9).

### Phase 1: human interface (the P1 that unblocks everything else)

### T5: GitHub approval gate
```yaml
depends_on: [T4]
blast_radius: high
check: python -m pytest tests/test_approval.py
```
Polls `main`; approved plan = PLAN.md at main HEAD; records approved_sha; halts on mismatch (F3). Tests use a local bare repo. Bot identity: a separate GitHub account or a GitHub App, with a token scoped to the plan repo.

### T6: Signal outbound
```yaml
depends_on: [T2]
blast_radius: low
check: python -m pytest tests/test_signal.py   # plus a manual check: message arrives on phone
```
`/v2/send` to the allowlisted human. Event types: critic-ready, blocked, question, done.

### T7: Signal inbound answers
```yaml
depends_on: [T6]
blast_radius: high
check: python -m pytest tests/test_signal_inbound.py
```
Polls receive; accepts only the allowlisted sender (F4); parses `Q<n> <answer>`; stores the answer in the ledger; unknown messages get a help reply. Answers that change the plan → the orchestrator opens a PR (C8).

### T8: Critic on draft plans
```yaml
depends_on: [T5, T6]
blast_radius: medium
check: python -m pytest tests/test_critic.py
```
When a plan PR is opened or updated: a strong model challenges constraints, failure modes and decisions; the report is posted as a PR comment and a Signal notification is sent.

### Phase 2: execution loop

### T9: Worker dispatch with privilege separation
```yaml
depends_on: [T4, T5]
blast_radius: high
check: python -m pytest tests/test_dispatch.py && sudo -u ceo-worker test ! -r /home/dries
```
`ceo` / `ceo-worker` users, sudoers rule, nftables rules (D5). Calls `opencode run` as ceo-worker in a per-task git worktree; CLI permission allowlist; captures output and cost.

### T10: Orchestrator-run checks and evidence
```yaml
depends_on: [T9]
blast_radius: high
check: python -m pytest tests/test_checks.py
```
`completed` only if `check` passes in a clean checkout; output and SHA are stored. Failing → retry until the attempt limit, then `blocked` → Signal question (Gate 2).

### T11: Elevated-action requests
```yaml
depends_on: [T7, T10]
blast_radius: high
check: python -m pytest tests/test_elevated.py
```
Merge to main, deploy, anything that needs sudo → a request row with `blocked:needs-approval` → approved via Signal `Q<n> approve` or a GitHub PR review.

### T12: Budget enforcement
```yaml
depends_on: [T9]
blast_radius: medium
check: python -m pytest tests/test_budget.py
```
Per-run cost in the ledger; monthly cap; pause plus Signal alert at 80% and 100%.

### Phase 3: dogfood
Remaining CEO improvements are run through CEO itself. Then the first external plan: media-box (VPN exit → one cycling stream → Data_Vault in Jellyfin).

## 8. Open questions

- OQ1. Bot Signal identity: a separate number (which number types does Signal accept for registration?) vs. a linked device on the personal account (gives the bot access to all personal messages, so not preferred).
- OQ2. Branch protection / rulesets on a **private** repo: available on the free GitHub plan? (The framework repo is public, so this is fine; the media-box plan repo may be private.)
- OQ3. Headless permission behaviour of `opencode run` and `claude -p` for actions not on the allowlist: refuse, hang, or allow? Verify before T9.
- OQ4. Signal receive in `MODE=native`: is polling `GET /v1/receive/{number}` enough, or should we switch to `json-rpc` mode (websocket)? Verify before T7.
- OQ5. Per-run cost: can OpenCode report cost per run, or do we query OpenRouter's usage data?
- OQ6. Workers that need Docker (media-box phase): rootless Docker for `ceo-worker`, or a VM? Can gluetun run rootless?
- OQ7. Name: "CEO" clashes with Paperclip's CEO agent; is that irony intended?
- OQ8. ~~Does OpenSpec's JSON output expose task-level detail?~~ Answered by T0: yes (`instructions apply --json`), but task IDs are positional (R4). Still open: is `docs/agent-contract.md` a stable interface for programs?
- OQ9. ~~Where status lives~~ Resolved with D10: the ledger holds execution status (C7); checkboxes are ticked only by the orchestrator at archive, so the archived change records what was done.
- OQ10. Research tasks (like T0) have checks that prove a file exists, not that its findings are right. Should they always require human sign-off regardless of blast_radius?
