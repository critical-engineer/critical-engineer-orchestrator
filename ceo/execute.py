"""Running one task, checking it, and applying the engineer's decisions (T10).

One attempt:
1. Record the run; the task goes ``running``.
2. Export the approved commit for the worker (no ``.git``, see workspace.py).
3. The worker does the task, starting from the plan's integration branch
   ``ceo/<change>/integration`` (the approved commit plus every task completed so far), so
   a task sees the work of the tasks it depends on. CEO commits the result on
   ``ceo/<change>/<task>``.
4. The task's ``check`` runs **as the worker user**, in a fresh export of that
   commit: it executes code the worker wrote, so it must never run as ``ceo``.
5. Decide:
   - the worker reported assumptions the plan didn't sanction: gate (F12);
   - the check passed: ``completed``, or a ``high_radius`` gate if the task's
     blast radius is high (G3, key design principle);
   - otherwise the attempt failed: retry, and after the attempt limit an
     ``escalation`` gate (T10, D12).

``resolve_gates`` applies the engineer's decisions once gates are decided. A
denial's reason goes into the next attempt's prompt.
"""

import dataclasses
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ceo.gates import open_gate
from ceo.ledger import append_event, record_event
from ceo.notify import Channel, ChannelError, notify
from ceo.questions import ask_gate
from ceo.workers import Sandbox, build_prompt
from ceo.workspace import branch_tip, commit_result, export, fast_forward, is_ancestor, set_branch

_OUTPUT_LIMIT = 20_000  # characters of check output kept as evidence


def integration_branch(change: str) -> str:
    """Where completed tasks accumulate. A sibling of the task branches
    ``ceo/<change>/<task>`` (git can't have both ``ceo/x`` and ``ceo/x/1.1``)."""
    return f"ceo/{change}/integration"


@dataclass(frozen=True)
class TaskSpec:
    id: str
    description: str
    check: str
    blast_radius: str  # low | medium | high


@dataclass
class Executor:
    conn: sqlite3.Connection
    repo: Path  # CEO's clone of the plan repo
    worker: Any  # has .run(prompt, workdir) -> WorkResult and .model
    check_sandbox: Sandbox
    work_root: Path
    max_attempts: int = 2
    escalation_model: str | None = None
    check_timeout: float = 900
    channel: Channel | None = None

    # -- one attempt -------------------------------------------------------

    def run_task(self, plan: sqlite3.Row, task: TaskSpec) -> str:
        """Run one attempt of ``task``. Returns the outcome."""
        conn, change = self.conn, plan["change"]
        if not plan["approved_sha"]:
            raise ValueError(f"plan {change} isn't approved")
        base = self._integration_base(plan)
        model = self._model_for(plan["id"], task.id)
        worker = dataclasses.replace(self.worker, model=model) if model != self.worker.model else self.worker
        with conn:
            conn.execute(
                "UPDATE tasks SET status = 'running', attempts = attempts + 1 WHERE plan_id = ? AND id = ?",
                (plan["id"], task.id),
            )
            attempt = conn.execute(
                "SELECT attempts FROM tasks WHERE plan_id = ? AND id = ?", (plan["id"], task.id)
            ).fetchone()[0]
            run_id = conn.execute(
                "INSERT INTO runs (plan_id, task_id, worker, model, check_command) VALUES (?, ?, ?, ?, ?)",
                (plan["id"], task.id, worker.name, model, task.check),
            ).lastrowid
            append_event(conn, "task.started", plan_id=plan["id"], task_id=task.id,
                         detail={"run": run_id, "attempt": attempt, "model": model})

        result, sha, check_exit, output, unchanged = None, None, None, "", False
        try:
            workdir = self.work_root / change / f"{task.id}-{attempt}"
            shutil.rmtree(workdir, ignore_errors=True)
            workdir.parent.mkdir(parents=True, exist_ok=True)
            export(self.repo, base, workdir)
            prompt = build_prompt(
                task_id=task.id,
                description=task.description,
                check=task.check,
                change_dir=f"openspec/changes/{change}",
                feedback=self._feedback(plan["id"], task.id),
            )
            result = worker.run(prompt, workdir)
            # The cost is known now; record it before anything else can fail,
            # so the budget always counts it.
            with conn:
                conn.execute("UPDATE runs SET cost_usd = ? WHERE id = ?", (result.cost_usd, run_id))
            if result.ok:
                sha = commit_result(
                    self.repo, base, workdir,
                    branch=f"ceo/{change}/{task.id}",
                    message=f"{change} {task.id}: {task.description[:60]}",
                )
            # No changes can be right (the work is already there, e.g. after a
            # crash or done by an earlier task): the check on the starting point
            # decides. A worker that does nothing still can't pass a check the
            # code doesn't satisfy.
            unchanged = result.ok and sha is None
            if unchanged:
                sha = base
            if sha:
                check_exit, output = self._check(change, task, attempt, sha)
        except Exception as exc:
            # Any failure inside the attempt ends the attempt, so it counts
            # towards the attempt limit and escalation instead of leaving the
            # task 'running'.
            self._finish_run(run_id, sha, check_exit, output)
            return self._failed(plan, task, f"the attempt raised {exc!r}"[:500])

        self._finish_run(run_id, sha, check_exit, output)

        if not result.ok:
            return self._failed(plan, task, f"worker failed: {'; '.join(result.errors)[:500]}")
        if not result.reported_assumptions:
            return self._failed(plan, task, "the worker didn't end with an ASSUMPTIONS: section")
        if unchanged and check_exit != 0:
            return self._failed(plan, task, f"the worker changed nothing and the check fails (exit {check_exit})")
        if result.assumptions:
            return self._gate(
                plan, task, "unsanctioned_assumption",
                {"task": task.id, "commit": sha, "assumptions": result.assumptions, "check_exit": check_exit},
                "The worker made assumptions the plan didn't sanction:\n"
                + "\n".join(f"- {a}" for a in result.assumptions)
                + "\nApprove to accept them, or deny with what it should do instead.",
            )
        if check_exit == 0:
            return self._passed(plan, task, sha, output)
        return self._failed(plan, task, f"check failed (exit {check_exit}): {output[-300:]}")

    def _finish_run(self, run_id: int, sha: str | None, check_exit: int | None, output: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), commit_sha = ?,"
                " check_exit = ?, check_output = ? WHERE id = ?",
                (sha, check_exit, output, run_id),
            )

    def _integration_base(self, plan: sqlite3.Row) -> str:
        """The tip of the integration branch, started (or restarted) at the approved commit.

        If the plan was re-approved at a commit the branch doesn't build on,
        the branch starts again from the new approved commit.
        """
        branch, approved = integration_branch(plan["change"]), plan["approved_sha"]
        tip = branch_tip(self.repo, branch)
        if tip is None or not is_ancestor(self.repo, approved, tip):
            if tip is not None:
                record_event(self.conn, "integration.reset", plan_id=plan["id"],
                             detail={"branch": branch, "old_tip": tip, "approved_sha": approved})
            set_branch(self.repo, branch, approved)
            return approved
        return tip

    def _integrate(self, plan: sqlite3.Row, task: TaskSpec, sha: str) -> bool:
        """Add a finished task's commit to the integration branch (fast-forward only)."""
        if fast_forward(self.repo, integration_branch(plan["change"]), sha):
            return True
        record_event(self.conn, "task.integration_stale", plan_id=plan["id"], task_id=task.id,
                     detail={"commit": sha})
        return False

    def _check(self, change: str, task: TaskSpec, attempt: int, sha: str) -> tuple[int, str]:
        checkdir = self.work_root / change / f"{task.id}-{attempt}-check"
        shutil.rmtree(checkdir, ignore_errors=True)
        export(self.repo, sha, checkdir)
        result = self.check_sandbox.run(["bash", "-c", task.check], cwd=checkdir, timeout=self.check_timeout)
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output[-_OUTPUT_LIMIT:]

    # -- outcomes ----------------------------------------------------------

    def _set_status(self, plan_id: int, task_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET status = ? WHERE plan_id = ? AND id = ?", (status, plan_id, task_id)
            )

    def _passed(self, plan: sqlite3.Row, task: TaskSpec, sha: str, output: str) -> str:
        if task.blast_radius == "high":
            return self._gate(
                plan, task, "high_radius",
                {"task": task.id, "commit": sha, "check": task.check, "check_output_tail": output[-1000:]},
                f"High blast radius: the check passed, but the result needs your sign-off. "
                f"Review branch ceo/{plan['change']}/{task.id} ({sha[:12]}) against the plan's "
                "constraints and failure modes.",
            )
        if not self._integrate(plan, task, sha):
            self._set_status(plan["id"], task.id, "pending")
            return "retry"  # other work landed first; redo it on top so its check covers both
        self._set_status(plan["id"], task.id, "completed")
        record_event(self.conn, "task.completed", plan_id=plan["id"], task_id=task.id, detail={"commit": sha})
        self._notify("done", f"Task {task.id} passed its check ({sha[:12]}).", plan, task.id)
        return "completed"

    def _failed(self, plan: sqlite3.Row, task: TaskSpec, reason: str) -> str:
        record_event(self.conn, "task.attempt_failed", plan_id=plan["id"], task_id=task.id, detail={"reason": reason})
        attempts = self.conn.execute(
            "SELECT attempts FROM tasks WHERE plan_id = ? AND id = ?", (plan["id"], task.id)
        ).fetchone()[0]
        if attempts < self.max_attempts:
            self._set_status(plan["id"], task.id, "pending")
            return "retry"
        current = self._model_for(plan["id"], task.id)
        return self._gate(
            plan, task, "escalation",
            {"task": task.id, "from_model": current, "to_model": self.escalation_model,
             "attempts": attempts, "reason": reason},
            f"Task {task.id} failed {attempts} attempts. Last: {reason[:300]}\n"
            + (f"Approve one more attempt on {self.escalation_model}, " if self.escalation_model
               else "Approve one more attempt, ")
            + "or deny with a reason (e.g. the plan needs changing).",
        )

    def _gate(self, plan: sqlite3.Row, task: TaskSpec, kind: str, payload: dict, summary: str) -> str:
        gate_id = open_gate(self.conn, plan["id"], kind, payload, task_id=task.id)
        self._set_status(plan["id"], task.id, "blocked")
        if self.channel is not None:
            try:
                ask_gate(self.conn, self.channel, gate_id, summary, plan=plan["change"])
            except ChannelError:
                pass  # recorded as notify.failed; the gate stays open and is listed by the skill
        return f"gate:{kind}"

    def _notify(self, kind: str, summary: str, plan: sqlite3.Row, task_id: str) -> None:
        if self.channel is None:
            return
        try:
            notify(self.conn, self.channel, kind, summary, plan_id=plan["id"], plan=plan["change"], task_id=task_id)
        except ChannelError:
            pass

    # -- decisions ---------------------------------------------------------

    def resolve_gates(self, plan: sqlite3.Row, tasks: dict[str, TaskSpec]) -> list[str]:
        """Apply decided task gates that haven't been applied yet. Returns what happened."""
        applied = []
        rows = self.conn.execute(
            "SELECT * FROM gates WHERE plan_id = ? AND task_id IS NOT NULL AND status <> 'open'"
            " AND id NOT IN (SELECT json_extract(detail, '$.gate') FROM events WHERE kind = 'task.gate_applied')"
            " ORDER BY id",
            (plan["id"],),
        ).fetchall()
        for gate in rows:
            task = tasks.get(gate["task_id"])
            status = self.conn.execute(
                "SELECT status FROM tasks WHERE plan_id = ? AND id = ?", (plan["id"], gate["task_id"])
            ).fetchone()
            outcome = "ignored"
            if task is not None and status is not None and status["status"] == "blocked":
                outcome = self._apply(plan, task, gate)
            record_event(self.conn, "task.gate_applied", plan_id=plan["id"], task_id=gate["task_id"],
                         detail={"gate": gate["id"], "outcome": outcome})
            applied.append(f"{gate['task_id']}:{gate['kind']}:{gate['status']}:{outcome}")
        return applied

    def _apply(self, plan: sqlite3.Row, task: TaskSpec, gate: sqlite3.Row) -> str:
        payload = json.loads(gate["payload"])
        approved = gate["status"] == "approved"
        if gate["kind"] == "high_radius":
            if approved:
                if not self._integrate(plan, task, payload["commit"]):
                    self._set_status(plan["id"], task.id, "pending")
                    return "retry"
                self._set_status(plan["id"], task.id, "completed")
                record_event(self.conn, "task.completed", plan_id=plan["id"], task_id=task.id,
                             detail={"commit": payload.get("commit"), "gate": gate["id"]})
                self._notify("done", f"Task {task.id} signed off and completed.", plan, task.id)
                return "completed"
            self._set_status(plan["id"], task.id, "pending")
            return "retry"
        if gate["kind"] == "unsanctioned_assumption":
            if not approved:
                self._set_status(plan["id"], task.id, "pending")
                return "retry"
            if payload.get("check_exit") == 0:
                return self._passed(plan, task, payload["commit"], "")
            return self._failed(plan, task, f"check failed (exit {payload.get('check_exit')})")
        if gate["kind"] == "escalation":
            if approved:
                self._set_status(plan["id"], task.id, "pending")
                return "retry"
            return "blocked"
        return "ignored"

    def _feedback(self, plan_id: int, task_id: str) -> list[str]:
        """Why earlier attempts weren't accepted: denial reasons and the latest failure."""
        reasons = [
            f"The engineer said: {r['reason']}"
            for r in self.conn.execute(
                "SELECT reason FROM gates WHERE plan_id = ? AND task_id = ? AND status = 'denied' ORDER BY id",
                (plan_id, task_id),
            )
        ]
        last_failure = self.conn.execute(
            "SELECT json_extract(detail, '$.reason') FROM events WHERE kind = 'task.attempt_failed'"
            " AND plan_id = ? AND task_id = ? ORDER BY id DESC LIMIT 1",
            (plan_id, task_id),
        ).fetchone()
        if last_failure and last_failure[0]:
            reasons.append(f"The last attempt failed: {last_failure[0]}")
        return reasons

    def _model_for(self, plan_id: int, task_id: str) -> str:
        """The default model, or the stronger one an approved escalation allowed."""
        row = self.conn.execute(
            "SELECT payload FROM gates WHERE plan_id = ? AND task_id = ? AND kind = 'escalation'"
            " AND status = 'approved' ORDER BY id DESC LIMIT 1",
            (plan_id, task_id),
        ).fetchone()
        if row:
            to_model = json.loads(row["payload"]).get("to_model")
            if to_model:
                return to_model
        return self.worker.model
