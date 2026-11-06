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
