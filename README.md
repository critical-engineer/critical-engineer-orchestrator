# Critical Engineer Orchestrator (CEO)

A small orchestrator where the **plan**, not the ticket, is the unit of coordination.
Plans are OpenSpec changes using the `critical-engineer` schema; approval is a merge to
protected `main`; a task is done only when an orchestrator-run check passes.
Background: [criticalengineer.ai](https://criticalengineer.ai/). Bootstrap plan: [PLAN.md](PLAN.md).

Status: pre-alpha (T1 of the bootstrap plan).

## Setup

```sh
export OPENSPEC_TELEMETRY=0          # OpenSpec telemetry is on by default
(cd tools && npm ci --ignore-scripts) # pinned OpenSpec CLI
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
tools/node_modules/.bin/openspec schema validate critical-engineer
```

## Layout

- `openspec/schemas/critical-engineer/`: plan schema (OpenSpec `spec-driven` plus constraints,
  failure modes, decisions with rejected alternatives, and `ceo.yaml` task metadata)
- `ceo/`: the orchestrator (Python 3.11+, stdlib plus PyYAML)
- `tools/`: pinned Node CLIs
- `docs/spikes/`: spike findings behind decisions (T0 → D10)
- `.github/`: CI (the T1 checks), Dependabot, CODEOWNERS

## Licence

Code: Apache 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). Docs and specs: CC BY-SA 4.0 ([LICENSE-docs](LICENSE-docs)).
