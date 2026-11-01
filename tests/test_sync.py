import json

import pytest

from ceo.ledger import open_ledger
from ceo.sync import SyncError, sync_change

REPO = "critical-engineer/example"

DESIGN = """\
# Design

## Constraints

- C1. The ledger is the only status store.

## Failure Modes

| ID | Failure | Mitigation |
|----|---------|------------|
| F1 | Status drifts into the plan | C1 |

## Decisions

- D1. Sync keys tasks on X.Y labels.
  Rejected: OpenSpec's positional ids (they shift).
"""


def entry(check="true", blast_radius="low", depends_on=()):
    deps = ", ".join(f'"{d}"' for d in depends_on)
    return f'{{depends_on: [{deps}], blast_radius: {blast_radius}, check: "{check}"}}'


def write_change(path, tasks, entries=None):
    """tasks: {label: description}; entries: {label: ceo.yaml flow mapping}."""
    entries = entries or {}
    path.mkdir(exist_ok=True)
    (path / "design.md").write_text(DESIGN, encoding="utf-8")
    lines = ["# Tasks", "", "## 1. Work", ""]
    lines += [f"- [ ] {label} {desc}" for label, desc in tasks.items()]
    (path / "tasks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    yaml_lines = ["tasks:"] + [
        f'  "{label}": {entries.get(label, entry())}' for label in tasks
    ]
    (path / "ceo.yaml").write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(tmp_path / "ledger.db")
    yield c
    c.close()


@pytest.fixture
def change(tmp_path):
    path = tmp_path / "bootstrap"
    write_change(path, {"1.1": "Create schema", "1.2": "Sync tasks"})
    return path


def tasks(conn):
    return {
        r["id"]: (r["status"], r["tombstoned"])
        for r in conn.execute("SELECT * FROM tasks ORDER BY id")
    }


def event_kinds(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]


def test_first_sync_adds_tasks(conn, change):
    result = sync_change(conn, REPO, change)
    assert result.added == ["1.1", "1.2"]
    assert (result.removed, result.changed, result.flagged) == ([], [], [])
    plan = conn.execute("SELECT * FROM plans").fetchone()
    assert (plan["id"], plan["repo"], plan["change"], plan["approved_sha"]) == (
        result.plan_id, REPO, "bootstrap", None,
    )
    assert tasks(conn) == {"1.1": ("pending", 0), "1.2": ("pending", 0)}
    assert event_kinds(conn) == ["task.added", "task.added", "plan.synced"]


def test_resync_without_changes_is_a_no_op(conn, change):
    first = sync_change(conn, REPO, change)
    second = sync_change(conn, REPO, change)
    assert second.plan_id == first.plan_id
    assert (second.added, second.removed, second.changed, second.flagged) == ([], [], [], [])


def test_ticking_a_checkbox_is_not_a_change(conn, change):
    sync_change(conn, REPO, change)
    path = change / "tasks.md"
    path.write_text(path.read_text(encoding="utf-8").replace("- [ ] 1.1", "- [x] 1.1"), encoding="utf-8")
    result = sync_change(conn, REPO, change)
    assert result.changed == [] and result.flagged == []
    assert tasks(conn)["1.1"] == ("pending", 0)  # the ledger, not the plan, holds status (C7)


def test_inserted_task_does_not_disturb_others(conn, change):
    sync_change(conn, REPO, change)
    write_change(change, {"1.1": "Create schema", "1.3": "New", "1.2": "Sync tasks"})
    result = sync_change(conn, REPO, change)
    assert result.added == ["1.3"]
    assert result.changed == []


def test_removed_task_is_tombstoned(conn, change):
    sync_change(conn, REPO, change)
    conn.execute("UPDATE tasks SET status = 'completed' WHERE id = '1.2'")
    conn.commit()
    write_change(change, {"1.1": "Create schema"})
    result = sync_change(conn, REPO, change)
    assert result.removed == ["1.2"]
    assert tasks(conn)["1.2"] == ("completed", 1)  # kept, with its history
    detail = conn.execute("SELECT detail FROM events WHERE kind = 'task.removed'").fetchone()[0]
    assert json.loads(detail) == {"previous_status": "completed"}
    assert sync_change(conn, REPO, change).removed == []  # only once


def test_removed_id_cannot_be_reused(conn, change):
    sync_change(conn, REPO, change)
    write_change(change, {"1.1": "Create schema"})
    sync_change(conn, REPO, change)
    events_before = event_kinds(conn)
    write_change(change, {"1.1": "Create schema", "1.2": "Something else", "1.3": "New"})
    with pytest.raises(SyncError, match="1.2 was removed earlier"):
        sync_change(conn, REPO, change)
    assert event_kinds(conn) == events_before  # nothing written
    assert "1.3" not in tasks(conn)


def test_changed_pending_task_is_updated(conn, change):
    sync_change(conn, REPO, change)
    write_change(change, {"1.1": "Create schema", "1.2": "Sync tasks"}, {"1.2": entry(check="pytest")})
    result = sync_change(conn, REPO, change)
    assert result.changed == ["1.2"] and result.flagged == []
    assert tasks(conn)["1.2"] == ("pending", 0)


@pytest.mark.parametrize(
    "status, attempts", [("completed", 1), ("running", 1), ("blocked", 2), ("pending", 1)]
)
def test_changed_task_that_has_run_is_flagged_and_blocked(conn, change, status, attempts):
    sync_change(conn, REPO, change)
    conn.execute(
        "UPDATE tasks SET status = ?, attempts = ? WHERE id = '1.1'", (status, attempts)
    )
    conn.commit()
    write_change(change, {"1.1": "Create schema differently", "1.2": "Sync tasks"})
    result = sync_change(conn, REPO, change)
    assert result.flagged == ["1.1"] and result.changed == []
    assert tasks(conn)["1.1"] == ("blocked", 0)
    detail = conn.execute(
        "SELECT detail FROM events WHERE kind = 'task.changed_after_run'"
    ).fetchone()[0]
    assert json.loads(detail) == {"previous_status": status, "attempts": attempts}


@pytest.mark.parametrize(
    "new_entry",
    [entry(check="pytest"), entry(blast_radius="high"), entry(depends_on=["1.2"])],
)
def test_any_part_of_the_ceo_yaml_entry_counts_as_a_change(conn, change, new_entry):
    sync_change(conn, REPO, change)
    write_change(change, {"1.1": "Create schema", "1.2": "Sync tasks"}, {"1.1": new_entry})
    assert sync_change(conn, REPO, change).changed == ["1.1"]


def test_invalid_change_is_not_synced(conn, change):
    (change / "design.md").write_text("# Design\n", encoding="utf-8")
    with pytest.raises(SyncError) as exc:
        sync_change(conn, REPO, change)
    assert any("Constraints" in p.message for p in exc.value.problems)
    assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 0


def test_same_change_name_in_two_repos_are_separate_plans(conn, change):
    a = sync_change(conn, "org/a", change)
    b = sync_change(conn, "org/b", change)
    assert a.plan_id != b.plan_id
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 4


def test_failure_mid_sync_rolls_everything_back(conn, change, monkeypatch):
    sync_change(conn, REPO, change)
    before = (tasks(conn), event_kinds(conn))
    write_change(change, {"1.1": "Create schema v2", "1.3": "New"})  # change, add, remove

    import ceo.sync

    real = ceo.sync.append_event

    def fail_on_removal(conn, kind, **kwargs):
        if kind == "task.removed":
            raise RuntimeError("boom")
        return real(conn, kind, **kwargs)

    monkeypatch.setattr(ceo.sync, "append_event", fail_on_removal)
    with pytest.raises(RuntimeError):
        sync_change(conn, REPO, change)
    assert (tasks(conn), event_kinds(conn)) == before


def test_task_without_stored_definition_gets_a_baseline(conn, change):
    sync_change(conn, REPO, change)
    # As if migrated from a v1 ledger: 1.1 already ran, 1.2 never did.
    conn.execute("UPDATE tasks SET definition_hash = NULL")
    conn.execute("UPDATE tasks SET status = 'completed', attempts = 1 WHERE id = '1.1'")
    conn.commit()
    result = sync_change(conn, REPO, change)
    assert (result.baselined, result.changed, result.flagged) == (["1.1", "1.2"], [], [])
    # A task that ran was never verified against this definition: review it (G3).
    assert tasks(conn) == {"1.1": ("blocked", 0), "1.2": ("pending", 0)}
    details = [
        json.loads(r["detail"])
        for r in conn.execute("SELECT detail FROM events WHERE kind = 'task.baselined' ORDER BY id")
    ]
    assert details == [
        {"needs_review": True, "previous_status": "completed"},
        {"needs_review": False, "previous_status": "pending"},
    ]
    write_change(change, {"1.1": "Create schema", "1.2": "Changed after baseline"})
    assert sync_change(conn, REPO, change).changed == ["1.2"]  # changes are caught from here on
