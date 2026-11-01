import pytest

from ceo.validate import main, validate_change

TASKS = """\
# Tasks

## 1. Ledger

- [ ] 1.1 Create the schema
- [ ] 1.2 Sync tasks
"""

DESIGN = """\
# Design

## Context

Some context.

## Constraints

- C1. Python 3.11+ stdlib plus PyYAML.
- C2. The ledger is the only status store.

## Failure Modes

| ID | Failure | Mitigation |
|----|---------|------------|
| F1 | Worker exits 0 without doing the task | Orchestrator-run check (D1) |
| F2 | Status drifts into the plan file | C2 |

## Decisions

- D1. Orchestrator runs each task's check.
  Rejected: trusting the worker's exit code (F1).

## Risks / Trade-offs

None yet.
"""

CEO_YAML = """\
tasks:
  "1.1": {depends_on: [], blast_radius: medium, check: "python -m pytest tests/test_ledger.py"}
  "1.2": {depends_on: ["1.1"], blast_radius: low, check: "python -m pytest tests/test_sync.py"}
"""


@pytest.fixture
def change(tmp_path):
    (tmp_path / "tasks.md").write_text(TASKS)
    (tmp_path / "design.md").write_text(DESIGN)
    (tmp_path / "ceo.yaml").write_text(CEO_YAML)
    return tmp_path


def messages(change):
    return [p.message for p in validate_change(change)]


def edit(change, name, old, new):
    path = change / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_valid_change(change):
    assert validate_change(change) == []


@pytest.mark.parametrize("name", ["tasks.md", "design.md", "ceo.yaml"])
def test_missing_file(change, name):
    (change / name).unlink()
    assert any("missing" in m for m in messages(change))


# design.md


@pytest.mark.parametrize("section", ["Constraints", "Failure Modes", "Decisions"])
def test_missing_design_section(change, section):
    edit(change, "design.md", f"## {section}\n", "## Renamed\n")
    assert f"missing section '## {section}'" in messages(change)


def test_decision_without_rejected_alternatives(change):
    edit(change, "design.md", "  Rejected: trusting the worker's exit code (F1).\n", "")
    assert "D1 lists no rejected alternatives" in messages(change)


def test_failure_mode_must_reference_a_mitigation(change):
    edit(change, "design.md", "| F2 | Status drifts into the plan file | C2 |", "| F2 | Status drifts | be careful |")
    assert "F2 mitigation references no constraint, decision or task" in messages(change)


def test_failure_mode_may_reference_a_task(change):
    edit(change, "design.md", "| F2 | Status drifts into the plan file | C2 |", "| F2 | Status drifts | task 1.2 |")
    assert validate_change(change) == []


def test_failure_mode_reference_must_exist(change):
    edit(change, "design.md", "| C2 |", "| C9 |")
    assert "F2 mitigation references no constraint, decision or task" in messages(change)


def test_duplicate_constraint(change):
    edit(change, "design.md", "- C2. The ledger", "- C1. The ledger")
    assert "duplicate C1" in messages(change)


def test_empty_constraints_section(change):
    edit(change, "design.md", "- C1. Python 3.11+ stdlib plus PyYAML.\n- C2. The ledger is the only status store.\n", "TBD\n")
    assert "no numbered constraints (e.g. '- C1. ...')" in messages(change)


# ceo.yaml


def test_ceo_yaml_missing_entry(change):
    edit(change, "tasks.md", "- [ ] 1.2 Sync tasks\n", "- [ ] 1.2 Sync tasks\n- [ ] 1.3 New\n")
    assert "no entry for task 1.3" in messages(change)


def test_ceo_yaml_extra_entry(change):
    edit(change, "tasks.md", "- [ ] 1.2 Sync tasks\n", "")
    assert "entry 1.2 matches no task in tasks.md" in messages(change)


def test_unquoted_task_id(change):
    edit(change, "ceo.yaml", '"1.2":', "1.2:")
    msgs = messages(change)
    assert 'task ID 1.2 must be a quoted string, e.g. "1.1"' in msgs


@pytest.mark.parametrize(
    "old, new, expected",
    [
        ('check: "python -m pytest tests/test_sync.py"', 'check: ""', "1.2: needs a non-empty 'check'"),
        ("blast_radius: low", "blast_radius: tiny", "1.2: blast_radius must be one of low, medium, high"),
        ('depends_on: ["1.1"]', 'depends_on: ["9.9"]', "1.2: depends on unknown task 9.9"),
        ('depends_on: ["1.1"]', 'depends_on: ["1.2"]', "1.2: depends on itself"),
        ('depends_on: ["1.1"]', "depends_on: [1.1]", "1.2: depends_on must be a list of quoted task IDs"),
        ("blast_radius: low,", "blast_radius: low, checks: x,", "1.2: unknown key 'checks'"),
    ],
)
def test_ceo_yaml_entry_problems(change, old, new, expected):
    edit(change, "ceo.yaml", old, new)
    assert expected in messages(change)


def test_dependency_cycle(change):
    edit(change, "ceo.yaml", '"1.1": {depends_on: []', '"1.1": {depends_on: ["1.2"]')
    assert "dependency cycle: 1.1 -> 1.2 -> 1.1" in messages(change)


def test_ceo_yaml_not_a_mapping(change):
    (change / "ceo.yaml").write_text("- just a list\n")
    assert messages(change) == ["expected a top-level 'tasks' mapping"]


def test_ceo_yaml_invalid_yaml(change):
    (change / "ceo.yaml").write_text("tasks: [unclosed\n")
    assert any(m.startswith("invalid YAML") for m in messages(change))


# CLI


def test_main_exit_codes(change, capsys):
    assert main([str(change)]) == 0
    assert capsys.readouterr().out.strip().endswith(": ok")
    edit(change, "ceo.yaml", "blast_radius: low", "blast_radius: tiny")
    assert main([str(change)]) == 1
    assert "blast_radius must be one of" in capsys.readouterr().out
    assert main([]) == 2
