"""Sync an OpenSpec change's tasks into the ledger (T4).

Only a change that passes the CE validator is synced. In one transaction:

- new task labels are inserted as ``pending``;
- labels gone from ``tasks.md`` are tombstoned (never deleted, never reused);
- a task whose definition changed is updated; if it had already run, it is
  set to ``blocked`` so a human decides what its earlier evidence is worth (F9);
- a task with no stored definition (migrated ledger) gets a baseline, recorded
  as ``task.baselined``; if it had already run it is also set to ``blocked``,
  because its evidence was never checked against this definition (G3).

A definition is the task's description plus its ``ceo.yaml`` entry. Checkbox
state in ``tasks.md`` is ignored: the ledger, not the plan, holds status (C7).
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ceo.ledger import append_event
from ceo.tasks import Problem, Task, load_tasks
from ceo.validate import load_ceo_entries, validate_change


class SyncError(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("; ".join(str(p) for p in problems))


@dataclass
class SyncResult:
    plan_id: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)  # changed after running
    baselined: list[str] = field(default_factory=list)  # migrated without a definition


def definition_hash(task: Task, entry: dict) -> str:
    definition = {
        "description": task.description,
        "check": entry["check"],
        "blast_radius": entry["blast_radius"],
        "depends_on": sorted(entry.get("depends_on", [])),
    }
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


def sync_change(conn: sqlite3.Connection, repo: str, change_dir: Path) -> SyncResult:
    problems = validate_change(change_dir)
    if problems:
        raise SyncError(problems)
    tasks, _ = load_tasks(change_dir / "tasks.md")
    entries = load_ceo_entries(change_dir / "ceo.yaml")
    change = change_dir.name

    with conn:  # one transaction: commits on success, rolls back on any error
        conn.execute(
            "INSERT INTO plans (repo, change) VALUES (?, ?) ON CONFLICT (repo, change) DO NOTHING",
            (repo, change),
        )
        plan_id = conn.execute(
            "SELECT id FROM plans WHERE repo = ? AND change = ?", (repo, change)
        ).fetchone()[0]
        result = SyncResult(plan_id)
        existing = {
            row["id"]: row
            for row in conn.execute("SELECT * FROM tasks WHERE plan_id = ?", (plan_id,))
        }

        reused = [t for t in tasks if t.id in existing and existing[t.id]["tombstoned"]]
        if reused:
            raise SyncError(
                [
                    Problem("tasks.md", t.line, f"task {t.id} was removed earlier; IDs are never reused (F9)")
                    for t in reused
                ]
            )

        for task in tasks:
            new_hash = definition_hash(task, entries[task.id])
            row = existing.get(task.id)
            if row is None:
                conn.execute(
                    "INSERT INTO tasks (plan_id, id, definition_hash) VALUES (?, ?, ?)",
                    (plan_id, task.id, new_hash),
                )
                append_event(conn, "task.added", plan_id=plan_id, task_id=task.id)
                result.added.append(task.id)
            elif row["definition_hash"] is None:
                # Migrated from a ledger without hashes: there is no earlier
                # definition to compare with. Not evidence of a change, but a
                # task that already ran was never verified against *this*
                # definition, so it goes to a human like a changed one (G3).
                has_run = row["attempts"] > 0 or row["status"] != "pending"
                conn.execute(
                    "UPDATE tasks SET definition_hash = ?, status = ? WHERE plan_id = ? AND id = ?",
                    (new_hash, "blocked" if has_run else row["status"], plan_id, task.id),
                )
                append_event(
                    conn,
                    "task.baselined",
                    plan_id=plan_id,
                    task_id=task.id,
                    detail={"needs_review": has_run, "previous_status": row["status"]},
                )
                result.baselined.append(task.id)
            elif row["definition_hash"] != new_hash:
                has_run = row["attempts"] > 0 or row["status"] != "pending"
                new_status = "blocked" if has_run else row["status"]
                conn.execute(
                    "UPDATE tasks SET definition_hash = ?, status = ? WHERE plan_id = ? AND id = ?",
                    (new_hash, new_status, plan_id, task.id),
                )
                if has_run:
                    append_event(
                        conn,
                        "task.changed_after_run",
                        plan_id=plan_id,
                        task_id=task.id,
                        detail={"previous_status": row["status"], "attempts": row["attempts"]},
                    )
                    result.flagged.append(task.id)
                else:
                    append_event(conn, "task.changed", plan_id=plan_id, task_id=task.id)
                    result.changed.append(task.id)

        current = {t.id for t in tasks}
        for task_id, row in sorted(existing.items()):
            if task_id not in current and not row["tombstoned"]:
                conn.execute(
                    "UPDATE tasks SET tombstoned = 1 WHERE plan_id = ? AND id = ?",
                    (plan_id, task_id),
                )
                append_event(
                    conn,
                    "task.removed",
                    plan_id=plan_id,
                    task_id=task_id,
                    detail={"previous_status": row["status"]},
                )
                result.removed.append(task_id)

        append_event(
            conn,
            "plan.synced",
            plan_id=plan_id,
            detail={
                "added": result.added,
                "removed": result.removed,
                "changed": result.changed,
                "flagged": result.flagged,
                "baselined": result.baselined,
            },
        )
    return result
