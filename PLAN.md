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

CEO makes the *plan* the unit of coordination. It's a small, auditable orchestrator. Agents carry out work against an approved plan, the orchestrator checks the results, and the Critical Engineer owns every decision that matters, without having to sit at a terminal.

## Key design principle: the Critical Engineer owns judgment; models express it

### The Critical Engineer
CEO is built for one persona, the one the essay names: the **Critical Engineer**, "the person who decides, validates and owns the consequences. The AI executes." For the decisions that matter, they are not just in the loop; they *are* the loop. In this plan, "the engineer" means that person: the one who drives the work from their own coding assistant and is accountable for the result.

### Judgment
Judgment is deciding what should be built and why, and whether what was built achieves it. It is knowing that a requirement is wrong, or seeing that a correct implementation of a flawed design will cause harm at scale. It is spotting the unsafe concurrency assumption, the misapplied consistency model, the business constraint nobody encoded. Checks don't catch these: tests can pass while the system is still wrong for reasons the tests never simulated.

**Models point; the engineer decides.** Models help judgment but don't exercise it. The Critic, adversarial tests and review prompts exist to put the risky places in front of the engineer, with the reasoning and the exact location, never to approve them. Accountability can't be delegated to a model.

**Judgment gates.** At these points CEO stops, shows the engineer the evidence and the models' pointers, and waits. Only the engineer can clear a gate (C10):

| Gate | When |
|------|------|
| Plan approval | Every plan and plan change (D1, F3) |
| High blast radius | A `high` task is not `completed` on its check alone: the Critic interrogates the result against the plan's constraints and failure modes, and the engineer signs off (T10) |
| Unsanctioned assumption | A worker reports an assumption the plan didn't sanction; the engineer decides whether the plan is updated or the implementation corrected (T9, F12) |
| Stale evidence | A task's definition changed after it ran, or its evidence predates its definition (T4) |
| Elevated action | Merge, deploy, anything needing sudo, shown as the exact action (T11) |
| Escalation | A task the cheap models and the retry ladder couldn't finish (T10) |

**Review effort is proportional to reasoning risk, not diff size.** `blast_radius` sets the depth: `low` proceeds on its check, with the evidence kept for later; `medium` notifies the engineer with the evidence and the Critic's pointers; `high` is a gate. Asking for judgment where none is needed is itself a failure (F11): an engineer who is asked everything learns to approve everything.

### The workflow
Each step is owned by whoever is best, and cheapest, at it. Expensive thinking happens once, in the plan; cheap execution happens many times against it.

| Step | Who | Model cost |
|------|-----|------------|
| 1. Research and plan | The engineer with their coding assistant, through the CEO skill (D13). Output: an OpenSpec change with constraints, failure modes, decisions and checkable tasks | The engineer's subscription, interactive |
| 2. Critique | The Critic, run from the CEO skill; it points, the engineer judges (T8) | The engineer's subscription, interactive |
| 3. Approve | The engineer merges the plan PR (gate) | None |
| 4. Execute | Workers on the box, one small task each; they report unsanctioned assumptions (D4, T9) | Cheap API models, capped (G5) |
| 5. Verify | The orchestrator runs each task's check, then applies the gates above (G3) | None: no LLM |
| 6. Escalate | A task that keeps failing goes to a stronger model within budget, then to the engineer via the CEO skill (T10) | API, then subscription |
| 7. Decide | Gates and questions: chat on the phone, or the CEO skill at the desk (T7, T11, T13) | None |

The coding assistant is the engineer's choice: Claude Code, Gemini CLI, Codex or another. CEO reaches it through a skill (D13), not a dependency on one vendor.

What follows from it:
- **The orchestrator never needs an LLM.** Routing, gating and verification are deterministic.
- **Plans are written for small models.** Each task is small, specific about where it works, and has a meaningful check. A task a small model can't execute is a planning failure, not an execution one; the Critic flags it.
- **Subscriptions are only ever used interactively,** by the engineer driving their own assistant. CEO never automates a subscription (D12).
- **The engineer reviews the reasoning, not every line of code.** A wrong line in a plan becomes many wrong lines of code; a wrong line of code that its check catches costs one retry.

Credit: the research → plan → implement workflow and approvals bound to an exact action are ideas from [HumanLayer](https://github.com/humanlayer/humanlayer).

## 2. Goals / non-goals

**Goals (v1)**
- G1. A plan in a git repo is the single source of engineering intent. Approval is explicit, attributable and tied to a commit.
- G2. Async human interface. Plans are edited and approved on GitHub; questions and notifications go through a chat channel (Matrix for the POC, D11); deep work happens at the desk through a skill in the engineer's coding assistant (D13). No tmux babysitting.
- G3. Tasks complete only when an orchestrator-run check passes, with the evidence stored.
- G4. Workers are contained by the environment (OS user, network), not by prompt or blocklist.
- G5. API model spend ≤ $20/month, enforced: everything CEO pays per token (workers, escalations, any automated Critic pass). The engineer's own subscription is excluded: a fixed cost they already pay, used only interactively (D12).
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
- C4. Workers never hold the GitHub or chat-channel credentials, and can't write the ledger or the plan.
- C5. Workers have no sudo, no Docker group membership, no access to `/home/dries` or `/media`, no access to the home network, and no access to the chat channel (its API or credentials).
- C6. The approved plan is read-only during execution. Any change goes through a PR and re-approval.
- C7. The ledger is the only store of execution status. No status is kept in the plan file. Under D10, the `tasks.md` checkboxes are written only by the orchestrator, at archive time, after checks pass. Agents never tick them; an edit to the approved file on `main` counts as a plan change (F3).
- C8. Every decision a human makes through the chat channel is saved in the ledger. Decisions that change the plan become PRs.
- C9. Changes to the gate code (approval, check, permission logic) always need human review, including when CEO is building itself.
- C10. Judgment gates (see the key design principle) are cleared only by the Critical Engineer: never by a model, a worker or configuration. Each clearance is recorded with who, when and the evidence shown (C8).

## 4. Failure modes

| ID | Failure | Mitigation |
|----|---------|------------|
| F1 | Worker exits 0 without having done the task | G3: orchestrator runs the task's `check`; the exit code is ignored for completion |
| F2 | Worker edits the plan or self-approves | C4/C6: plan outside the worker's workspace; approval = protected-main SHA |
| F3 | Plan edited after approval | Orchestrator compares main HEAD with the approved SHA; on mismatch it halts and re-gates |
| F4 | Chat message spoofed or injected | Accept only from the allowlisted human account; answers are data, never instructions |
| F5 | Decisions lost in chat | C8 |
| F6 | Runaway spend | OpenRouter key limit, per-task attempt limit, cost per run in the ledger |
| F7 | Worker reads personal data or messages | C5; the bot has its own chat account, so it can't read the human's messages (D11) |
| F8 | Self-build weakens its own gates | C9: protected paths in CODEOWNERS / review rule |
| F9 | Task IDs shift when the plan is edited | Explicit `X.Y` labels parsed by CEO, never OpenSpec's positional JSON `id` (T0 R4); deleted IDs are tombstoned in the ledger, never reused |
| F10 | A cheap model's plan is wrong and nobody notices | Critic review of the plan, then human approval, before any execution |
| F11 | Approval fatigue: the engineer is asked too often and starts rubber-stamping | Gates only where blast radius, a deviation or stale evidence warrants; low-risk work proceeds on its check; gates per plan are counted in the ledger |
| F12 | A model's unsanctioned assumption is accepted silently | Workers must report assumptions the plan didn't sanction, and each one is a gate (T9); on `high` tasks the Critic looks for unreported ones (T10) |

## 5. Decisions

- D1. **Plan = Markdown in git; approval = merge to protected `main`.**
  Rejected: a YAML `status: approved` field, which anyone who can write the file can set; a hash recorded by a local `ceo approve` command, which needs a terminal.
- D2. **Ledger = SQLite**, owned by the `ceo` user. Rejected: Postgres (unneeded at this scale); graph-framework state (a second copy of reality).
- D3. **Orchestrator = a plain Python loop.** Rejected: LangGraph (extra abstraction that hides the state machine).
- D4. **Workers = existing CLIs, called headless.** Default `opencode run --format json` (OpenRouter). Optional: `claude -p`, with an API key only, never a subscription (D12).
  Rejected: our own coding agent (not the new part); OpenHands (strong sandbox, but centred on conversations and a heavier setup).
- D5. **Privilege separation:** orchestrator runs as `ceo`, workers as `ceo-worker` via a single sudoers rule. nftables blocks `ceo-worker` from home-network ranges and from local port 9922.
  Rejected: a single user (workers could tamper with the ledger and credentials); a VM (heavier; revisit if workers need Docker).
- D6. **Human interface:** GitHub for durable artefacts (plan, plan-change PRs, critic reports); a chat channel for notifications and short answers on the phone (D11); the CEO skill in the engineer's coding assistant for deep work at the desk (D13). CEO itself opens no inbound ports: the box only polls outbound. The engineer's existing SSH access to the box is administrative and outside CEO; if the skill uses it (OQ12), it must not widen it (e.g. a restricted key with a forced command).
- D7. **Elevated actions are ledger requests,** not interactive prompts. They sit as `blocked:needs-approval` until approved.
- D8. **Status comes from artefacts:** each task runs on a branch with one commit per task. The ledger stores the commit SHA, check output and cost.
- D9. **Paperclip isn't used,** because its unit of coordination is the ticket. Ideas borrowed from it: budgets, atomic task checkout, an audit log.
- D10. **(Accepted 2026-11-01 after the T0 spike; see docs/spikes/T0-openspec.md) OpenSpec is the plan format; CEO is the approval and execution layer.** CEO additionally owns: stable task IDs parsed from `X.Y` labels (OpenSpec's JSON IDs are positional), a CE validator on top of `openspec validate` (which doesn't check custom sections or `ceo.yaml`), and telemetry off. A custom `critical-engineer` schema forked from `spec-driven` adds constraints, failure modes and rejected alternatives, plus a CEO-owned per-task file (check / depends_on / blast_radius keyed by task ID). Archive merges deltas into living specs, so the record of how the system works and why outlives each change.
  Rejected: our own PLAN.md format. It works, but it goes stale after execution and has to reinvent validation.
  Risks: OpenSpec moves fast (its repo docs are marked partly legacy; custom schemas are partly experimental), so pin the version; there's a status-location conflict, resolved by C7.

- D11. **Chat channel = Matrix for the POC, behind a pluggable channel interface.** The bot is an ordinary Matrix account of its own; it talks to the human in one private, unencrypted room and refuses to send if the room holds anyone else or is encrypted. Outbound HTTPS only, stdlib only (C2).
  Rejected: Signal (the bot needs a second phone number to register and keep alive; kept as an optional channel); Telegram (owner and governance didn't sit right with the maintainer, and messages aren't end-to-end encrypted); Delta Chat (end-to-end encrypted by default, but bots need an extra native RPC binary); GitHub-only (the framework repo is public, and replying in the app is clunky; still the home for durable decisions, D6); ntfy (push and buttons only, no free-text answers).
  Risks: unencrypted, so the homeserver operator can read messages: keep secrets and personal data out of them; revisit encryption or a self-hosted homeserver after the POC.
- D12. **Judgment-support runs on the engineer's own subscription, interactively; execution runs on cheap API models.** See the key design principle. The orchestrator itself uses no model, and no model clears a gate (C10).
  Rejected: everything on paid APIs (a strong model for every task breaks the budget, G5); automating a subscription on the box, e.g. via `claude -p` or an agent SDK (providers' terms for automated use of consumer subscriptions are volatile: Anthropic's changed several times in 2026; and it would take the engineer out of the judgment it is meant to support); one cheap model for everything including planning (weak plans are exactly F10).
  Risks: cheap models may fail more often, causing more escalations; the ledger records attempts and escalations per task so the split can be tuned. Re-check the subscription terms before changing this (OQ13).
- D13. **The desk interface is a CEO skill for the engineer's coding assistant** (Claude Code first; the skill shouldn't depend on one vendor). It lets the engineer start a plan (scaffold an OpenSpec change), run the Critic, see status, answer questions, approve elevated actions and take over a blocked task. How the skill reaches CEO is open (OQ12): a CLI over SSH, an MCP server, or A2A. Whatever the transport, it acts as the engineer through the same gates, and every decision is recorded in the ledger (C8).
  Rejected: a web UI (a non-goal for v1); tmux sessions (the problem CEO exists to solve); building the human interface into the orchestrator loop (keeps the loop deterministic and small, G6).

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
check: python -m pytest tests/test_parser.py tests/test_validate.py
```
Rescoped by T0. (a) Task-label parser: reads `X.Y` labels from `tasks.md` (or from `openspec instructions apply --json` descriptions), rejects duplicate or missing labels. (b) CE validator, run after `openspec validate --strict`: design has Constraints / Failure Modes / Decisions sections, every decision lists rejected alternatives, `ceo.yaml` keys match task labels one-to-one, every task has a `check`. Fixtures cover malformed changes.

### T4: Plan → ledger sync
```yaml
depends_on: [T2, T3]
blast_radius: medium
check: python -m pytest tests/test_sync.py
```
Inserts new tasks, tombstones removed ones, flags changed tasks that have already run (F9). Implemented in `ceo/sync.py`: only a change that passes the CE validator is synced, in one transaction; a task that changed after running is set to `blocked` for a human decision; re-adding a removed label is refused.

### Phase 1: human interface (the P1 that unblocks everything else)

### T5: GitHub approval gate
```yaml
depends_on: [T4]
blast_radius: high
check: python -m pytest tests/test_approval.py
```
Polls `main`; approved plan = PLAN.md at main HEAD; records approved_sha; halts on mismatch (F3). Tests use a local bare repo. Bot identity: a separate GitHub account or a GitHub App, with a token scoped to the plan repo.

### T6: Chat notifications (outbound)
```yaml
depends_on: [T2]
blast_radius: low
check: python -m pytest tests/test_notify.py tests/test_matrix.py tests/test_signal.py   # plus a manual check: python -m ceo.channels.matrix check
```
Event types: critic-ready, blocked, question, done, sent only to the one configured human. `ceo/notify.py` is channel-agnostic; channels in `ceo/channels/` (Matrix, the POC default; Signal kept as an option). The Matrix bot refuses to send unless the room holds exactly the bot and the human and is unencrypted.

### T7: Chat answers (inbound)
```yaml
depends_on: [T6]
blast_radius: high
check: python -m pytest tests/test_answers.py
```
Polls receive; accepts only the allowlisted sender (F4); parses `Q<n> <answer>`; stores the answer in the ledger; unknown messages get a help reply. Answers that change the plan → the orchestrator opens a PR (C8).

### T8: Critic on draft plans
```yaml
depends_on: [T5, T13]
blast_radius: medium
check: python -m pytest tests/test_critic.py
```
Run from the CEO skill on the engineer's subscription (D12), on request. It points, the engineer judges: a strong model challenges the constraints, failure modes and decisions, names likely design flaws, concurrency and consistency risks and unencoded constraints with their locations, and flags tasks too vague or too large for a small model. It never approves. The report is posted as a PR comment. Optional: a cheap automated first pass when a plan PR is opened, with a chat notification.

### T13: CEO skill for the engineer's coding assistant
```yaml
depends_on: [T4, T7]
blast_radius: medium
check: python -m pytest tests/test_skill.py
```
The desk interface (D13): a skill (Claude Code first) plus the operations behind it: scaffold a plan, status, open questions, answer, approve, take over a blocked task. Starts with a short spike to choose the transport (OQ12). Decisions made through it are recorded in the ledger like chat answers (C8).

### Phase 2: execution loop

### T9: Worker dispatch with privilege separation
```yaml
depends_on: [T4, T5]
blast_radius: high
check: python -m pytest tests/test_dispatch.py && sudo -u ceo-worker test ! -r /home/dries
```
`ceo` / `ceo-worker` users, sudoers rule, nftables rules (D5). Calls `opencode run` as ceo-worker in a per-task git worktree; CLI permission allowlist; captures output and cost. Worker contract: the final output lists any assumption the plan didn't sanction; each one is a judgment gate (F12).

### T10: Orchestrator-run checks and evidence
```yaml
depends_on: [T9, T13]
blast_radius: high
check: python -m pytest tests/test_checks.py
```
`completed` only if `check` passes in a clean checkout; output and SHA are stored. Failing → retry until the attempt limit, then escalate: optionally one more attempt on a stronger model within budget (T12), then `blocked` → chat question, with a takeover path through the CEO skill (T13). Attempts and escalations are recorded per task (D12): every attempt is a `runs` row (worker, model, cost; adds `runs.model`) and increments `tasks.attempts`; each step up the ladder is an event, `task.retry`, `task.escalated` (`{from_model, to_model, reason}`) or `task.takeover` (`{by}`), so a task's full path can be rebuilt from the ledger. A `high` task whose check passes goes to a judgment gate: the Critic interrogates the result against the plan's constraints and failure modes, and it is `completed` only when the engineer signs off.

### T11: Elevated-action requests
```yaml
depends_on: [T7, T10, T13]
blast_radius: high
check: python -m pytest tests/test_elevated.py
```
Merge to main, deploy, anything that needs sudo → a request row with `blocked:needs-approval` → approved via chat `Q<n> approve`, the CEO skill, or a GitHub PR review. Approvals are structured (an idea from HumanLayer): each request shows the exact action (command, diff or target) and its arguments, and a denial carries a reason that goes back to the worker as input. Invariant: the request stores the exact action payload and its SHA-256 digest; approval fields are write-once; just before execution the digest is recomputed from what will actually run, and a mismatch is rejected and needs a new approval.

### T12: Budget enforcement
```yaml
depends_on: [T9]
blast_radius: medium
check: python -m pytest tests/test_budget.py
```
Per-run cost in the ledger; monthly cap; pause plus chat alert at 80% and 100%.

### Phase 3: dogfood
Remaining CEO improvements are run through CEO itself. Then the first external plan: media-box (VPN exit → one cycling stream → Data_Vault in Jellyfin).

## 8. Open questions

- OQ1. ~~Bot chat identity~~ Resolved 2026-11-01 by D11: the bot has its own Matrix account (no phone number needed).
- OQ2. Branch protection / rulesets on a **private** repo: available on the free GitHub plan? (The framework repo is public, so this is fine; the media-box plan repo may be private.)
- OQ3. Headless permission behaviour of `opencode run` and `claude -p` for actions not on the allowlist: refuse, hang, or allow? Verify before T9.
- OQ4. ~~Signal receive mode~~ Superseded by D11; for Matrix, T7 long-polls `/sync`.
- OQ5. Per-run cost: can OpenCode report cost per run, or do we query OpenRouter's usage data?
- OQ6. Workers that need Docker (media-box phase): rootless Docker for `ceo-worker`, or a VM? Can gluetun run rootless?
- OQ7. Name: "CEO" clashes with Paperclip's CEO agent; is that irony intended?
- OQ8. ~~Does OpenSpec's JSON output expose task-level detail?~~ Answered by T0: yes (`instructions apply --json`), but task IDs are positional (R4). Still open: is `docs/agent-contract.md` a stable interface for programs?
- OQ9. ~~Where status lives~~ Resolved with D10: the ledger holds execution status (C7); checkboxes are ticked only by the orchestrator at archive, so the archived change records what was done.
- OQ10. Research tasks (like T0) have checks that prove a file exists, not that its findings are right. Should they always require human sign-off regardless of blast_radius?
- OQ11. Matrix end-to-end encryption, after the POC: a vodozemac-backed Python library or an encrypting proxy (pantalaimon)? libolm is deprecated (July 2024) and python-olm wheels reportedly stop at CPython 3.12. Deferred by D11; needs a dependency decision (C2) and a backed-up key store.
- OQ12. Transport for the CEO skill (D13), keeping D6's boundary (no new inbound ports): a CLI run over the engineer's existing SSH access, with a restricted key and forced command (simplest), an MCP server (typed tools; where does it run, and how does it reach the box's ledger?), or A2A (if agents, not only the human, will talk to CEO). Decide with a short spike at the start of T13.
- OQ13. Subscription terms: before any change to D12, re-check the provider's current terms for automated and third-party use of consumer subscriptions (Anthropic's changed several times in 2026).
