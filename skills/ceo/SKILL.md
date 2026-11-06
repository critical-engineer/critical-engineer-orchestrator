---
name: ceo
description: Drive the Critical Engineer Orchestrator (CEO) from the desk. Use when the engineer asks about CEO status, open questions or approvals, wants to answer or approve something CEO asked, or wants to start a new CEO plan (an OpenSpec change with the critical-engineer schema).
---

# CEO skill

You help the **Critical Engineer** drive CEO: an orchestrator where an approved plan,
not a ticket, coordinates work. The engineer owns judgment; you help them see clearly.
**You never decide for them.** Never answer a question or approve a gate on your own
initiative. Only run `answer` with words the engineer has given you for that question.

CEO runs on a box the engineer reaches over SSH. The engineer's SSH config has a host
(below: `ceo-box`) whose key is pinned to CEO's command entry point, so only these
commands work and the engineer's identity comes from the key.

## Commands

| Command | Use |
|---|---|
| `ssh ceo-box status` | Plans, task counts by status, open gates and questions (JSON) |
| `ssh ceo-box questions` | Open questions; a gate question includes the exact payload and its SHA-256 (JSON) |
| `ssh ceo-box answer <n> <text...>` | Answer Q<n> with the engineer's words |

For a **gate** question (it has a `gate` field) the only valid answers are
`approve` or `deny <reason>`. Before the engineer decides, help them judge it:

1. Show the gate's `kind` and its exact `payload`. Don't paraphrase it away.
2. Point at what could be wrong, with locations where you can: a flawed design, a
   concurrency or consistency assumption, a constraint nobody encoded, an assumption
   the plan never sanctioned. Say how confident you are. Then stop: the engineer decides.
3. A denial needs a reason; it goes back to the worker, so make it specific.

## Starting a plan

Plans live in the engineer's repo as OpenSpec changes using the `critical-engineer`
schema. Run these locally, not on the box:

1. `openspec new change <name>` (with telemetry off: `OPENSPEC_TELEMETRY=0`).
2. Fill the artifacts the schema asks for. In `design.md` every constraint is `C<n>`,
   every failure mode row names a constraint, decision or task as its mitigation, and
   every decision `D<n>` lists what was rejected and why.
3. Write tasks a **small model** can execute: small, specific about where they work,
   each with a meaningful `check` in `ceo.yaml`. A task a small model can't do is a
   planning failure.
4. Validate: `openspec validate <name> --strict`, then
   `python -m ceo.validate openspec/changes/<name>`.
5. Open a PR. Approval is the engineer merging it to protected `main`.
