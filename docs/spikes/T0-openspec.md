# T0 spike: OpenSpec as CEO's plan format

Date: 2026-11-01 · OpenSpec 1.16.0 (pinned, local install in `tools/`) · Node 26.13 (macOS)
Artefacts: the schema now lives in `openspec/schemas/critical-engineer/`. The throwaway `ceo-bootstrap` change and raw `--json` output were not kept; rerun the commands below against any change to reproduce them.

## What was tested

1. Fork `spec-driven` → `critical-engineer`; add Constraints / Failure Modes / Decisions-with-rejected-alternatives to the design instruction; add a `ceo` artifact generating `ceo.yaml`.
2. Express a condensed CEO plan as change `ceo-bootstrap` (proposal, 2 capability specs, design, 7 tasks, ceo.yaml).
3. Run `validate --strict`, `show`, `list`, `status`, `instructions apply` with `--json`.
4. Probe what `validate` does and doesn't enforce.

## Results

| # | Finding | Evidence | Impact on CEO |
|---|---------|----------|---------------|
| R1 | Custom schema works, including a non-markdown artifact (`ceo.yaml`) in the dependency graph | `schema validate` ✓; `status` lists `ceo` artifact; `apply.requires: [tasks, ceo]` honoured | Plan format covered without forking OpenSpec itself |
| R2 | All tested commands emit stable-looking JSON (`validate` carries `"version": "1.0"`) | `--json` output of each command | CEO can consume OpenSpec only via subprocess + JSON, as C2 intends |
| R3 | `instructions apply --json` returns context-file paths per artifact, progress counts, task list with line numbers, and a `state` | `instructions apply --json` | Good basis for building the worker's context payload |
| R4 | **Task `id` in JSON is positional** (`"1"`…`"7"`). The `1.1` label is only text in `description` | `instructions apply --json` tasks: `id: "1"`, `description: "1.1 Create…"` | **F9 confirmed.** Inserting a task renumbers every later task. CEO must parse the `X.Y` label itself and key the ledger and `ceo.yaml` on it |
| R5 | `validate --strict` doesn't check custom artifact content: an invalid `ceo.yaml` passes, and so does a design with no Constraints section | probe runs | CEO needs its own **CE validator** (required sections present; `ceo.yaml` keys ↔ task labels 1:1; every task has a check) layered on `openspec validate` |
| R6 | `show --json` gives requirements and scenarios structured, but the scenario body is `rawText` (WHEN/THEN not split into fields) | `show --json` | Fine for Critic and worker context; deriving checks automatically from scenarios would need our own parsing (not v1) |
| R7 | **Telemetry is on by default** | notice printed on `init` | Set `OPENSPEC_TELEMETRY=0` for `ceo` and `ceo-worker`, and `telemetry.enabled false` in config |
| R8 | Progress = checkbox count in `tasks.md` (`list --json`: completedTasks/totalTasks) | `list --json` | Consistent with the C7 resolution: only the orchestrator ticks boxes, at archive |
| R9 | CLI offers `openspec install` for a **standalone build that runs without Node.js** (untested) | `openspec --help` | Could remove the Node dependency on the box. The Homebrew Node breakage during this spike shows why that's attractive |

Not tested: `archive` (merging deltas into living specs), multi-repo stores, the standalone binary, behaviour under concurrent edits.

## Recommendation

**Adopt D10, with CEO owning four things OpenSpec doesn't do:**

1. **Stable task IDs:** parse the `X.Y` label from the task description; reject duplicates; never use OpenSpec's positional `id` (R4).
2. **CE validator:** run after `openspec validate --strict`. Checks required design sections (C/F/D), every F row references a mitigation, and `ceo.yaml` ↔ tasks are consistent (R5).
3. **Approval, execution, verification and the human interface:** unchanged from PLAN.md.
4. **Operational hygiene:** pinned version (or the standalone binary), telemetry off (R7, R9).

Effect on the plan: T1 shrinks to "schema + templates" (done in this spike, to be moved into the repo). T3 shrinks from "plan parser" to "task-label parser + CE validator".

## Still open

- Is the JSON shape a documented, stable contract? `docs/agent-contract.md` was not read. Pin the version and keep contract tests on the fields CEO uses.
- Does `archive` behave well when the orchestrator, not an agent, ticks the boxes? (Test in T1.)
