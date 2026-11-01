from ceo.tasks import parse_tasks

TASKS = """\
# Tasks

## 1. Ledger

- [ ] 1.1 Create the schema
- [x] 1.2 Sync tasks

## 2. Interface

- [ ] 2.1 Approval gate
"""


def test_parses_labels_descriptions_and_state():
    tasks, problems = parse_tasks(TASKS)
    assert problems == []
    assert [(t.id, t.description, t.done) for t in tasks] == [
        ("1.1", "Create the schema", False),
        ("1.2", "Sync tasks", True),
        ("2.1", "Approval gate", False),
    ]
    assert tasks[0].line == 5


def test_ids_are_labels_not_positions():
    # Inserting a task must not change the IDs of the tasks after it (F9).
    inserted = TASKS.replace("- [x] 1.2", "- [ ] 1.3 New task\n- [x] 1.2")
    tasks, _ = parse_tasks(inserted)
    assert [t.id for t in tasks] == ["1.1", "1.3", "1.2", "2.1"]


def test_uppercase_x_counts_as_done():
    tasks, _ = parse_tasks("- [X] 1.1 Done\n")
    assert tasks[0].done


def test_unlabelled_task_is_a_problem():
    tasks, problems = parse_tasks("- [ ] 1.1 ok\n- [ ] no label here\n")
    assert [t.id for t in tasks] == ["1.1"]
    assert [(p.line, p.message) for p in problems] == [(2, "task has no X.Y label")]


def test_duplicate_label_is_a_problem():
    _, problems = parse_tasks("- [ ] 1.1 a\n- [ ] 1.1 b\n")
    assert [(p.line, p.message) for p in problems] == [
        (2, "duplicate task 1.1 (first on line 1)")
    ]


def test_no_tasks_is_a_problem():
    _, problems = parse_tasks("# Tasks\n\nNothing yet.\n")
    assert [p.message for p in problems] == ["no tasks found"]


def test_ignores_non_checkbox_lists():
    tasks, problems = parse_tasks("- 1.1 not a task\n- [ ] 1.2 a task\n")
    assert problems == []
    assert [t.id for t in tasks] == ["1.2"]
