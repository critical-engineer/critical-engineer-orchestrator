# Critical Engineer Orchestrator (CEO)

> **Proof of concept.** CEO exists to test a workflow, not to be a production
> orchestrator. Robustness, scale and enterprise features come after the
> workflow has proven itself, and are deliberately left out until then.

## The idea

AI has made writing code cheap. Judgment hasn't become cheaper: deciding what to
build, why, and whether the result actually does it. Most agent frameworks make
the **ticket** the unit of coordination, and the reasoning behind the work has no
first-class home.

CEO makes the **plan** the unit of coordination:

1. **A plan holds the reasoning.** Each plan is an [OpenSpec](https://github.com/Fission-AI/OpenSpec)
   change using the `critical-engineer` schema: constraints, failure modes, and
   decisions with the alternatives that were rejected.
2. **Approval is explicit.** A plan is approved by merging it to protected `main`.
   If the approved plan changes, execution stops until it is approved again.
3. **Agents execute; the orchestrator verifies.** Existing coding agents run each
   task. A task is done only when a check run by the orchestrator passes, and the
   evidence is kept.
4. **The human owns judgment, asynchronously.** Plans are reviewed on GitHub;
   questions and approvals arrive by Signal. Nobody babysits a terminal.

Background: [criticalengineer.ai](https://criticalengineer.ai/).

## What the POC sets out to prove

- The workflow above works end to end on a real project, with one human and cheap
  models (budget: $20/month).
- **Dogfooding:** once the core loop runs, CEO's remaining features are built
  through CEO itself.
- **A real external plan:** then it runs a plan for a different project (a home
  media server) to completion.

If that holds, the workflow has earned hardening. If it doesn't, we've learned
which part of the idea is wrong cheaply.

## Deliberately simple for now

Each of these is a conscious POC shortcut, not a design limit:

| POC choice | Why it's enough for now |
|------------|-------------------------|
| One machine, one human | Proves the workflow without multi-user concerns |
| Sequential task execution | Easier to observe and reason about than parallel runs |
| SQLite ledger | The only status store; no service to run |
| Plain Python loop, stdlib plus PyYAML | Small enough to read in one sitting |
| GitHub and Signal, polled | No inbound ports; uses tools the human already has |
| Existing coding agents, called headless | The new part is the workflow, not the agent |

The full reasoning, including the failure modes each choice guards against, is in
[PLAN.md](PLAN.md).

## Status

Early. The plan format (OpenSpec plus the `critical-engineer` schema), the ledger
and the plan validator are in place; the human interface (GitHub approval, Signal)
is next. Progress is tracked task by task in [PLAN.md](PLAN.md).

## Try it

```sh
export OPENSPEC_TELEMETRY=0          # OpenSpec telemetry is on by default
(cd tools && npm ci --ignore-scripts) # pinned OpenSpec CLI
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
tools/node_modules/.bin/openspec schema validate critical-engineer
.venv/bin/python -m ceo.validate openspec/changes/<change>   # check a plan
```

## Layout

- `openspec/schemas/critical-engineer/`: the plan schema (OpenSpec `spec-driven` plus
  constraints, failure modes, decisions with rejected alternatives, and `ceo.yaml`
  task metadata)
- `ceo/`: the orchestrator (Python 3.11+)
- `tools/`: pinned Node CLIs
- `docs/spikes/`: spike findings behind decisions
- `.github/`: CI, Dependabot, CODEOWNERS

## Licence

Code: Apache 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). Docs and specs: CC BY-SA 4.0 ([LICENSE-docs](LICENSE-docs)).
