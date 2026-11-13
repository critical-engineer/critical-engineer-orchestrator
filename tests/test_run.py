"""End to end: a merged plan goes through approval, sync and its tasks in order."""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fake_http import FakeHttpApi

from ceo.approval import GitHub, PlanRepo
from ceo.execute import Executor
from ceo.gates import Engineer, decide_gate
from ceo.ledger import open_ledger
from ceo.notify import InboundBatch
from ceo.run import Orchestrator
from ceo.workers import Sandbox, WorkResult, parse_assumptions

REPO = "critical-engineer/plans"
ME = "@me:x"
ENGINEER = Engineer(chat=frozenset({ME}), github=frozenset({"driessamyn"}))

DESIGN = """# Design

## Constraints

- C1. Keep it small.

## Failure Modes

| ID | Failure | Mitigation |
|----|---------|------------|
| F1 | Wrong greeting | C1 |

## Decisions

- D1. Write plain text files.
  Rejected: JSON (overkill).
"""
TASKS = "# Tasks\n\n## 1. Greet\n\n- [ ] 1.1 Write hello.txt\n- [ ] 1.2 Write bye.txt after hello\n"
CEO_YAML = """tasks:
  "1.1": {depends_on: [], blast_radius: low, check: "grep -q hello hello.txt"}
  "1.2": {depends_on: ["1.1"], blast_radius: low, check: "grep -q bye bye.txt"}
"""


@dataclass
class TaskWorker:
    """Writes the file each task asks for."""

    model: str = "opencode/free"
    done: list = field(default_factory=list)
    name = "fake"

    def run(self, prompt, workdir):
        task = re.search(r"Task (\d+\.\d+):", prompt).group(1)
        self.done.append(task)
        name, word = {"1.1": ("hello.txt", "hello"), "1.2": ("bye.txt", "bye")}[task]
        (Path(workdir) / name).write_text(word + "\n")
        text = "Done.\nASSUMPTIONS: none"
        return WorkResult(0, text, 0.0, self.model, assumptions=[], reported_assumptions=True)


class Chat:
    name = "fake"

    def __init__(self):
        self.sent, self.inbox = [], []

    def send(self, message):
        self.sent.append(message)

    def receive(self, cursor, *, wait=30):
        return self.inbox.pop(0) if self.inbox else InboundBatch([], cursor or "c0")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    origin, work, ceo = tmp_path / "origin.git", tmp_path / "work", tmp_path / "ceo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    for k, v in (("user.name", "E"), ("user.email", "e@x"), ("commit.gpgsign", "false")):
        git(work, "config", k, v)
    git(work, "checkout", "-q", "-b", "main")
    (work / "README.md").write_text("plans\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    git(work, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(ceo)], check=True, capture_output=True)

    # The engineer's plan PR, merged.
    git(work, "checkout", "-q", "-b", "greet")
    change = work / "openspec" / "changes" / "greet"
    change.mkdir(parents=True)
    (change / "design.md").write_text(DESIGN)
    (change / "tasks.md").write_text(TASKS)
    (change / "ceo.yaml").write_text(CEO_YAML)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "plan: greet")
    git(work, "checkout", "-q", "main")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge greet", "greet")
    git(work, "push", "-q", "origin", "main")
    merge = git(work, "rev-parse", "HEAD")

    api = FakeHttpApi()
    api.routes = {
        ("GET", f"/repos/{REPO}/commits/"): (200, [
            {"number": 1, "merged_at": "t", "merge_commit_sha": merge, "base": {"ref": "main"}}]),
        ("GET", f"/repos/{REPO}/pulls/1"): (200, {"merged_by": {"login": "driessamyn"}}),
    }
    conn = open_ledger(tmp_path / "ledger.db")
    chat, worker = Chat(), TaskWorker()
    executor = Executor(conn=conn, repo=ceo, worker=worker, check_sandbox=Sandbox(user=None),
                        work_root=tmp_path / "workspaces", channel=chat)
    orch = Orchestrator(conn=conn, plans=["greet"], repo=PlanRepo(ceo), github=GitHub(REPO, api=api.url),
                        engineer=ENGINEER, executor=executor, budget_usd=20.0, channel=chat)
    yield {"orch": orch, "conn": conn, "chat": chat, "worker": worker, "ceo": ceo, "work": work, "api": api}
    api.close()
    conn.close()


def statuses(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT id, status FROM tasks ORDER BY id")}


def test_a_merged_plan_runs_its_tasks_in_order(world):
    orch, conn = world["orch"], world["conn"]
    log = orch.cycle()
    assert "greet: task 1.1 -> completed" in log
    assert statuses(conn) == {"1.1": "completed", "1.2": "pending"}
    assert "greet: task 1.2 -> completed" in orch.cycle()
    assert "greet: all 2 tasks completed" in orch.cycle()
    assert orch.cycle() == []  # completed once; nothing more to do
    assert world["worker"].done == ["1.1", "1.2"]
    assert git(world["ceo"], "show", "ceo/greet/1.2:bye.txt") == "bye"
    assert sum("All 2 tasks of greet are completed" in m for m in world["chat"].sent) == 1


def test_dependencies_are_respected(world):
    conn = world["orch"].conn
    world["orch"].cycle()
    # Pretend 1.1 regressed to pending: 1.2 must not run before it.
    conn.execute("UPDATE tasks SET status = 'pending' WHERE id = '1.1'")
    conn.commit()
    assert "greet: task 1.1 -> completed" in world["orch"].cycle()


def test_unverified_merge_asks_the_engineer_once_then_runs_after_approval(world):
    world["api"].routes[("GET", f"/repos/{REPO}/pulls/1")] = (200, {"merged_by": {"login": "someone-else"}})
    orch, conn, chat = world["orch"], world["conn"], world["chat"]
    assert orch.cycle() == ["greet: pending"]
    orch.cycle()
    asks = [m for m in chat.sent if "Approval needed (plan_approval)" in m]
    assert len(asks) == 1 and "someone-else" in asks[0]
    gate_id = conn.execute("SELECT id FROM gates WHERE kind = 'plan_approval'").fetchone()[0]
    decide_gate(conn, gate_id, "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert "greet: task 1.1 -> completed" in orch.cycle()


def test_budget_reached_dispatches_nothing(world):
    world["orch"].budget_usd = 0.0
    assert any("budget reached" in line for line in world["orch"].cycle())
    assert world["worker"].done == []


def test_interrupted_task_is_recovered(world):
    orch, conn = world["orch"], world["conn"]
    orch.cycle()
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = '1.2'")
    conn.commit()
    log = orch.cycle()
    assert "recovered interrupted task 1.2" in log and "greet: task 1.2 -> completed" in log


def test_an_invalid_plan_is_refused_and_reported(world):
    work = world["work"]
    (work / "openspec" / "changes" / "greet" / "design.md").write_text("# Design\n")
    git(work, "commit", "-q", "-am", "break the design")
    git(work, "push", "-q", "origin", "main")
    merge = git(work, "rev-parse", "HEAD")
    world["api"].routes[("GET", f"/repos/{REPO}/commits/")] = (200, [
        {"number": 2, "merged_at": "t", "merge_commit_sha": merge, "base": {"ref": "main"}}])
    world["api"].routes[("GET", f"/repos/{REPO}/pulls/2")] = (200, {"merged_by": {"login": "driessamyn"}})
    log = world["orch"].cycle()
    assert any(line.startswith("greet: error") for line in log)
    assert world["conn"].execute("SELECT count(*) FROM events WHERE kind = 'plan.sync_refused'").fetchone()[0] == 1
    assert world["worker"].done == []


def test_chat_answers_are_read_each_cycle(world):
    from ceo.notify import Inbound

    world["chat"].inbox = [InboundBatch([Inbound("$1", ME, "hello")], "c1")]
    log = world["orch"].cycle()
    assert "read 1 chat message(s)" in log


def test_next_ready_follows_dependencies_not_label_order(world):
    orch, conn = world["orch"], world["conn"]
    conn.execute("INSERT INTO plans (repo, change) VALUES ('r', 'other')")
    plan_id = conn.execute("SELECT id FROM plans WHERE change = 'other'").fetchone()[0]
    for task_id, status in (("1.1", "pending"), ("1.2", "pending"), ("2.1", "pending")):
        conn.execute("INSERT INTO tasks (plan_id, id, status) VALUES (?, ?, ?)", (plan_id, task_id, status))
    conn.commit()
    deps = {"1.1": ["1.2"], "1.2": [], "2.1": ["1.1", "1.2"]}
    assert orch._next_ready(plan_id, deps) == "1.2"   # 1.1 waits for 1.2
    conn.execute("UPDATE tasks SET status = 'completed' WHERE plan_id = ? AND id = '1.2'", (plan_id,))
    assert orch._next_ready(plan_id, deps) == "1.1"
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE plan_id = ? AND id = '1.1'", (plan_id,))
    assert orch._next_ready(plan_id, deps) is None     # 2.1 needs 1.1 completed


# review round (CodeRabbit on #16)


def test_one_failing_plan_does_not_stop_the_others(world, capsys):
    orch = world["orch"]
    orch.plans = ["does-not-exist", "greet"]
    import ceo.run

    real = ceo.run.check_plan

    def broken_for_one(conn, plan_id, repo, github, engineer):
        change = conn.execute("SELECT change FROM plans WHERE id = ?", (plan_id,)).fetchone()[0]
        if change == "does-not-exist":
            raise RuntimeError("boom")
        return real(conn, plan_id, repo, github, engineer)

    ceo.run.check_plan = broken_for_one
    try:
        log = orch.cycle()
    finally:
        ceo.run.check_plan = real
    assert any(line.startswith("does-not-exist: error") for line in log)
    assert "greet: task 1.1 -> completed" in log
    assert orch.failed
    detail = world["conn"].execute("SELECT detail FROM events WHERE kind = 'cycle.error'").fetchone()[0]
    assert "Traceback" in detail and "boom" in detail
    assert "Traceback" in capsys.readouterr().err


def test_a_chat_failure_does_not_stop_the_plans(world):
    class BrokenChat(Chat):
        def receive(self, cursor, *, wait=30):
            raise ValueError("malformed batch")

    world["orch"].channel = BrokenChat()
    log = world["orch"].cycle()
    assert any(line.startswith("chat error") for line in log)
    assert "greet: task 1.1 -> completed" in log


def test_a_failed_completion_announcement_is_retried(world):
    from ceo.notify import ChannelError

    orch, conn = world["orch"], world["conn"]
    orch.cycle()
    orch.cycle()

    class DownChat(Chat):
        def send(self, message):
            if "are completed" in message:
                raise ChannelError("chat is down")
            super().send(message)

    down = DownChat()
    orch.channel = orch.executor.channel = down
    assert any("announcement failed" in line for line in orch.cycle())
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'plan.completed'").fetchone()[0] == 0
    orch.channel = orch.executor.channel = world["chat"]
    assert "greet: all 2 tasks completed" in orch.cycle()
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'plan.completed'").fetchone()[0] == 1


# review round 2 (CodeRabbit on #16, and on the plan in #18)


def test_completion_is_handled_even_at_the_budget_cap(world):
    orch, conn = world["orch"], world["conn"]
    orch.cycle()
    orch.cycle()                      # both tasks done
    conn.execute("UPDATE runs SET cost_usd = 25.0")
    conn.commit()                     # the last task took spend past the cap
    assert "greet: all 2 tasks completed" in orch.cycle()


def test_a_budget_alert_that_fails_to_send_is_retried(world):
    from ceo.notify import ChannelError

    orch, conn = world["orch"], world["conn"]
    other = conn.execute("INSERT INTO plans (repo, change) VALUES ('x', 'y')").lastrowid
    conn.execute("INSERT INTO tasks (plan_id, id) VALUES (?, '1.1')", (other,))
    conn.execute("INSERT INTO runs (plan_id, task_id, worker, cost_usd) VALUES (?, '1.1', 'w', 19.0)", (other,))
    conn.commit()                     # 95% of the budget spent

    class DownChat(Chat):
        def send(self, message):
            if "monthly budget" in message:
                raise ChannelError("chat is down")
            super().send(message)

    orch.channel = orch.executor.channel = DownChat()
    orch.cycle()
    delivered = conn.execute("SELECT count(*) FROM events WHERE kind = 'budget.alert'"
                             " AND json_extract(detail, '$.delivered') = 1").fetchone()[0]
    assert delivered == 0
    orch.channel = orch.executor.channel = world["chat"]
    orch.cycle()
    assert any("passed 80% of the $20.00 monthly budget" in m for m in world["chat"].sent)


# review round 3 (CodeRabbit on #16)


def test_only_one_orchestrator_per_ledger(tmp_path):
    from ceo.run import acquire_lock

    ledger = tmp_path / "ledger.db"
    first = acquire_lock(ledger)
    try:
        with pytest.raises(SystemExit, match="another orchestrator is running"):
            acquire_lock(ledger)
    finally:
        first.close()
    acquire_lock(ledger).close()  # free again once the first one is gone


def test_second_orchestrator_process_refuses_to_start(tmp_path):
    import os
    import subprocess
    import sys

    from ceo.run import acquire_lock

    ledger = tmp_path / "ledger.db"
    held = acquire_lock(ledger)
    try:
        env = {**os.environ, "CEO_LEDGER": str(ledger), "CEO_PLAN_REPO": "o/r",
               "CEO_PLAN_REPO_DIR": str(tmp_path), "CEO_WORK_ROOT": str(tmp_path)}
        result = subprocess.run([sys.executable, "-m", "ceo.run", "once"], env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode != 0 and "another orchestrator is running" in result.stderr
    finally:
        held.close()


def test_budget_alerts_go_out_even_when_no_task_is_ready(world):
    orch, conn = world["orch"], world["conn"]
    orch.cycle()
    orch.cycle()
    orch.cycle()                                   # plan completed; nothing ready from now on
    conn.execute("UPDATE runs SET cost_usd = 17.0")
    conn.commit()                                  # the last task crossed 80%
    orch.cycle()
    assert any("passed 80% of the $20.00 monthly budget" in m for m in world["chat"].sent)


def test_re_approval_reruns_completed_tasks_in_the_loop(world):
    orch, conn, work = world["orch"], world["conn"], world["work"]
    orch.cycle()
    orch.cycle()                                   # 1.1 and 1.2 completed
    # The engineer changes the plan (README only, so no task definition changes)
    # and merges it: approved again at a commit the branch isn't built on.
    git(work, "checkout", "-q", "-b", "tweak", "main")
    (work / "openspec" / "changes" / "greet" / "proposal.md").write_text("# tweak\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "tweak plan")
    git(work, "checkout", "-q", "main")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge tweak", "tweak")
    git(work, "push", "-q", "origin", "main")
    merge = git(work, "rev-parse", "HEAD")
    world["api"].routes[("GET", f"/repos/{REPO}/commits/")] = (200, [
        {"number": 3, "merged_at": "t", "merge_commit_sha": merge, "base": {"ref": "main"}}])
    world["api"].routes[("GET", f"/repos/{REPO}/pulls/3")] = (200, {"merged_by": {"login": "driessamyn"}})
    log = orch.cycle()
    assert "greet: re-approved; completed tasks will run again" in log
    assert "greet: task 1.1 -> completed" in log
    assert "greet: task 1.2 -> completed" in orch.cycle()
    tip = git(world["ceo"], "rev-parse", "ceo/greet/integration")
    assert git(world["ceo"], "merge-base", "--is-ancestor", merge, tip) == ""  # built on the new plan


def test_an_undelivered_approval_question_is_sent_again(world):
    from ceo.notify import ChannelError

    world["api"].routes[("GET", f"/repos/{REPO}/pulls/1")] = (200, {"merged_by": {"login": "someone-else"}})
    orch, conn = world["orch"], world["conn"]

    class DownChat(Chat):
        def send(self, message):
            raise ChannelError("chat is down")

    orch.channel = orch.executor.channel = DownChat()
    orch.cycle()                                   # the question is recorded, but not delivered
    assert conn.execute("SELECT count(*) FROM questions").fetchone()[0] == 1
    orch.channel = orch.executor.channel = world["chat"]
    orch.cycle()                                   # sent again: same question, not a new one
    asks = [m for m in world["chat"].sent if "Approval needed (plan_approval)" in m]
    assert len(asks) == 1 and "Reply: Q1 " in asks[0]
    assert conn.execute("SELECT count(*) FROM questions").fetchone()[0] == 1
    orch.cycle()
    assert len([m for m in world["chat"].sent if "Approval needed (plan_approval)" in m]) == 1  # once delivered
