"""The orchestrator loop (T15): everything else, put together and run on the box.

One cycle:
1. Tasks left ``running`` by a crash go back to ``pending``.
2. Chat answers are read and recorded (T7).
3. For each configured plan:
   a. Is the plan on ``main`` approved by the engineer (T5)? If its approval
      gate is open, the engineer is asked (once).
   b. The approved version is synced into the ledger (T4).
   c. Decided gates are applied (T10).
   d. The budget is checked (T12); at the cap nothing is dispatched.
   e. One ready task runs (pending, every dependency completed). Execution
      is sequential on purpose.
   f. When every task is completed, the engineer hears so once.

Nothing in a cycle needs an LLM except the worker itself (key design principle).

Configuration (environment, usually from the settings file):
  CEO_PLAN_REPO       owner/name of the plan repo on GitHub
  CEO_PLAN_REPO_DIR   CEO's own clone of it
  CEO_PLANS           comma-separated change names to run, e.g. "media-box"
  CEO_WORK_ROOT       where per-task workspaces go (shared with the worker user)
  CEO_INTERVAL        seconds between cycles in `loop` mode (default 60)
  CEO_MAX_ATTEMPTS    attempts before an escalation gate (default 2)
  CEO_ESCALATION_MODEL  stronger model an approved escalation uses (optional)
  plus the channel, engineer, budget, worker and GitHub settings of each module.

  python -m ceo.run once   one cycle
  python -m ceo.run loop   cycles until stopped (SIGTERM / Ctrl-C finish the cycle first)
"""

import fcntl
import os
import signal
import sqlite3
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from ceo.approval import GitHub, PlanRepo, check_plan
from ceo.budget import cap_from_env, check_budget
from ceo.execute import Executor, TaskSpec
from ceo.gates import Engineer
from ceo.ledger import ledger_path, open_ledger, record_event
from ceo.notify import Channel, ChannelError, channel_from_env, notify
from ceo.questions import ask_gate, poll_once
from ceo.sync import SyncError, sync_change
from ceo.tasks import load_tasks
from ceo.validate import load_ceo_entries
from ceo.workers import Sandbox, worker_from_env
from ceo.workspace import export


@dataclass
class Orchestrator:
    conn: sqlite3.Connection
    plans: list[str]
    repo: PlanRepo
    github: GitHub
    engineer: Engineer
    executor: Executor
    budget_usd: float
    channel: Channel | None = None
    log: list[str] = field(default_factory=list)
    failed: bool = False  # did anything in the last cycle fail?

    def cycle(self) -> list[str]:
        self.log, self.failed = [], False
        self._recover_interrupted()
        self._read_answers()
        for change in self.plans:
            try:
                self._plan_cycle(change)
            except Exception as exc:  # one plan's failure mustn't stop the others
                self._error("cycle.error", {"plan": change}, exc)
                self.log.append(f"{change}: error {exc!r}")
        return self.log

    def _error(self, kind: str, detail: dict, exc: BaseException) -> None:
        """Record a failure with its traceback, print it, and remember it for the exit code."""
        self.failed = True
        trace = traceback.format_exc()
        print(trace, file=sys.stderr, flush=True)
        try:
            record_event(self.conn, kind, detail={**detail, "error": repr(exc)[:2000], "traceback": trace[-4000:]})
        except sqlite3.Error:
            pass  # the ledger itself is failing; the printed traceback is all we have

    def _recover_interrupted(self) -> None:
        rows = self.conn.execute("SELECT plan_id, id FROM tasks WHERE status = 'running'").fetchall()
        for row in rows:
            with self.conn:
                self.conn.execute(
                    "UPDATE tasks SET status = 'pending' WHERE plan_id = ? AND id = ?", (row[0], row[1])
                )
            record_event(self.conn, "task.interrupted", plan_id=row[0], task_id=row[1])
            self.log.append(f"recovered interrupted task {row[1]}")

    def _read_answers(self) -> None:
        if self.channel is None or not hasattr(self.channel, "receive"):
            return
        try:
            handled = poll_once(self.conn, self.channel, self.engineer, wait=0)
            if handled:
                self.log.append(f"read {handled} chat message(s)")
        except ChannelError as exc:
            record_event(self.conn, "cycle.chat_error", detail={"error": str(exc)})
            self.log.append(f"chat error: {exc}")
        except Exception as exc:  # e.g. a malformed answer: record it and still run the plans
            self._error("cycle.chat_error", {}, exc)
            self.log.append(f"chat error: {exc!r}")

    def _plan_id(self, change: str) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT INTO plans (repo, change) VALUES (?, ?) ON CONFLICT (repo, change) DO NOTHING",
                (self.github.repo, change),
            )
        return self.conn.execute(
            "SELECT id FROM plans WHERE repo = ? AND change = ?", (self.github.repo, change)
        ).fetchone()[0]

    def _plan_cycle(self, change: str) -> None:
        plan_id = self._plan_id(change)
        approval = check_plan(self.conn, plan_id, self.repo, self.github, self.engineer)
        if approval.status != "approved":
            if approval.status == "pending" and approval.gate_id is not None:
                self._ask_once(approval.gate_id, f"Plan '{change}' is on main but its approval isn't verified "
                               f"(merged by {approval.merger or 'unknown'}). Approve it to run.")
            self.log.append(f"{change}: {approval.status}")
            return

        plan = self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        specs, deps = self._sync(plan)
        for line in self.executor.resolve_gates(plan, specs):
            self.log.append(f"{change}: applied {line}")

        # A re-approval the integration branch isn't built on sends completed
        # tasks back to run again; do that before choosing what's ready.
        if self.executor.ensure_integration(plan):
            self.log.append(f"{change}: re-approved; completed tasks will run again")

        # Every cycle, so threshold alerts go out even when nothing is ready;
        # it only stops dispatch.
        budget = check_budget(self.conn, self.budget_usd, alert=self._alert)

        task_id = self._next_ready(plan_id, deps)
        if task_id is None:
            # Completion costs nothing, so it's handled even at the budget cap:
            # the last task can be the one that reaches it.
            self._maybe_completed(plan, specs)
            return
        if not budget.allowed:
            self.log.append(f"{change}: budget reached (${budget.spent_usd:.2f} of ${budget.cap_usd:.2f})")
            return
        outcome = self.executor.run_task(plan, specs[task_id])
        self.log.append(f"{change}: task {task_id} -> {outcome}")

    def _sync(self, plan: sqlite3.Row) -> tuple[dict[str, TaskSpec], dict[str, list[str]]]:
        """Sync the approved version and return its task specs and dependencies."""
        with tempfile.TemporaryDirectory(prefix="ceo-plan-") as tmp:
            export(self.repo.path, plan["approved_sha"], Path(tmp) / "tree", group_writable=False)
            change_dir = Path(tmp) / "tree" / "openspec" / "changes" / plan["change"]
            try:
                sync_change(self.conn, plan["repo"], change_dir)
            except SyncError as exc:
                record_event(self.conn, "plan.sync_refused", plan_id=plan["id"],
                             detail={"problems": [str(p) for p in exc.problems][:50]})
                raise
            tasks, _ = load_tasks(change_dir / "tasks.md")
            entries = load_ceo_entries(change_dir / "ceo.yaml")
        specs = {
            t.id: TaskSpec(t.id, t.description, entries[t.id]["check"], entries[t.id]["blast_radius"])
            for t in tasks
        }
        deps = {t.id: list(entries[t.id].get("depends_on", [])) for t in tasks}
        return specs, deps

    def _next_ready(self, plan_id: int, deps: dict[str, list[str]]) -> str | None:
        rows = self.conn.execute(
            "SELECT id, status FROM tasks WHERE plan_id = ? AND tombstoned = 0", (plan_id,)
        ).fetchall()
        status = {r["id"]: r["status"] for r in rows}
        for task_id in sorted(deps, key=lambda t: tuple(int(x) for x in t.split("."))):
            if status.get(task_id) == "pending" and all(status.get(d) == "completed" for d in deps[task_id]):
                return task_id
        return None

    def _maybe_completed(self, plan: sqlite3.Row, specs: dict[str, TaskSpec]) -> None:
        statuses = [
            r[0] for r in self.conn.execute(
                "SELECT status FROM tasks WHERE plan_id = ? AND tombstoned = 0", (plan["id"],)
            )
        ]
        if not statuses or any(s != "completed" for s in statuses):
            self.log.append(f"{plan['change']}: nothing ready")
            return
        done = self.conn.execute(
            "SELECT 1 FROM events WHERE kind = 'plan.completed' AND plan_id = ?"
            " AND json_extract(detail, '$.sha') = ?",
            (plan["id"], plan["approved_sha"]),
        ).fetchone()
        if done:
            return
        # Tell the engineer first; record completion only once they've been told,
        # so a failed send is retried on the next cycle.
        if self.channel is not None:
            try:
                notify(self.conn, self.channel, "done",
                       f"All {len(statuses)} tasks of {plan['change']} are completed. "
                       f"Integration branch: ceo/{plan['change']}/integration.",
                       plan_id=plan["id"], plan=plan["change"])
            except ChannelError:
                self.log.append(f"{plan['change']}: all {len(statuses)} tasks completed (announcement failed; will retry)")
                return
        record_event(self.conn, "plan.completed", plan_id=plan["id"], detail={"sha": plan["approved_sha"]})
        self.log.append(f"{plan['change']}: all {len(statuses)} tasks completed")

    def _ask_once(self, gate_id: int, summary: str) -> None:
        asked = self.conn.execute("SELECT 1 FROM questions WHERE gate_id = ?", (gate_id,)).fetchone()
        if asked or self.channel is None:
            return
        try:
            ask_gate(self.conn, self.channel, gate_id, summary)
        except ChannelError:
            pass

    def _alert(self, text: str) -> None:
        """Send a budget alert. A ChannelError propagates on purpose: check_budget
        then records the alert as not delivered and retries it next cycle."""
        if self.channel is None:
            return
        notify(self.conn, self.channel, "blocked", text)


def from_env(env: dict[str, str] | None = None) -> Orchestrator:
    env = dict(os.environ if env is None else env)
    missing = [k for k in ("CEO_PLAN_REPO", "CEO_PLAN_REPO_DIR", "CEO_PLANS", "CEO_WORK_ROOT") if not env.get(k)]
    if missing:
        raise SystemExit(f"not set: {', '.join(missing)}")
    conn = open_ledger(ledger_path(env))
    try:
        channel = channel_from_env(env)
    except ChannelError as exc:
        print(f"warning: no chat channel ({exc}); gates are only visible through the skill", file=sys.stderr)
        channel = None
    worker = worker_from_env(env)
    executor = Executor(
        conn=conn,
        repo=Path(env["CEO_PLAN_REPO_DIR"]),
        worker=worker,
        check_sandbox=worker.sandbox,
        work_root=Path(env["CEO_WORK_ROOT"]),
        max_attempts=int(env.get("CEO_MAX_ATTEMPTS", "2")),
        escalation_model=env.get("CEO_ESCALATION_MODEL") or None,
        channel=channel,
    )
    return Orchestrator(
        conn=conn,
        plans=[p.strip() for p in env["CEO_PLANS"].split(",") if p.strip()],
        repo=PlanRepo(Path(env["CEO_PLAN_REPO_DIR"])),
        github=GitHub(env["CEO_PLAN_REPO"], token=env.get("CEO_GITHUB_TOKEN") or None),
        engineer=Engineer.from_env(env),
        executor=executor,
        budget_usd=cap_from_env(env),
        channel=channel,
    )


def acquire_lock(ledger: Path):
    """One orchestrator per ledger. Returns the open lock file; keep it open.

    A second orchestrator (e.g. `once` while `loop` runs) would see the first
    one's running task as interrupted and dispatch it again, so it refuses.
    """
    lock = open(f"{ledger}.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f"another orchestrator is running on {ledger}") from None
    return lock


def main(argv: list[str]) -> int:
    if argv not in (["once"], ["loop"]):
        print("usage: python -m ceo.run once|loop", file=sys.stderr)
        return 2
    lock = acquire_lock(ledger_path())  # held until the process exits  # noqa: F841
    orchestrator = from_env()
    if argv == ["once"]:
        for line in orchestrator.cycle():
            print(line)
        return 1 if orchestrator.failed else 0
    stop = False

    def request_stop(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    interval = float(os.environ.get("CEO_INTERVAL", "60"))
    while not stop:
        try:
            for line in orchestrator.cycle():
                print(line, flush=True)
        except Exception:
            traceback.print_exc()
        deadline = time.monotonic() + interval
        while not stop and time.monotonic() < deadline:
            time.sleep(1)
    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
