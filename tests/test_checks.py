import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ceo.execute import Executor, TaskSpec
from ceo.gates import Engineer, decide_gate
from ceo.ledger import open_ledger
from ceo.workers import Sandbox, WorkResult

ME = "@me:x"
ENGINEER = Engineer(chat=frozenset({ME}))


@dataclass
class FakeWorker:
    """Writes files into the workdir and reports like the OpenCode worker."""

    files: dict = field(default_factory=dict)
    text: str = "Done.\nASSUMPTIONS: none"
    ok: bool = True
    cost: float = 0.01
    model: str = "opencode/free"
    prompts: list = field(default_factory=list)
    models_used: list = field(default_factory=list)

    name = "fake"

    def run(self, prompt, workdir):
        self.prompts.append(prompt)
        self.models_used.append(self.model)
        for name, content in self.files.items():
            path = Path(workdir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        from ceo.workers import parse_assumptions

        reported, assumptions = parse_assumptions(self.text)
        return WorkResult(
            exit_code=0 if self.ok else 1, text=self.text, cost_usd=self.cost, model=self.model,
            errors=[] if self.ok else ["boom"], assumptions=assumptions, reported_assumptions=reported,
        )


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class RecordingSandbox(Sandbox):
    def __init__(self):
        super().__init__(user=None)
        object.__setattr__(self, "calls", [])

    def run(self, argv, *, cwd, timeout, env=None):
        self.calls.append((argv, Path(cwd)))
        return super().run(argv, cwd=cwd, timeout=timeout, env=env)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "plans"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.name", "E"), ("user.email", "e@x"), ("commit.gpgsign", "false")):
        git(repo, "config", k, v)
    (repo / "openspec" / "changes" / "demo").mkdir(parents=True)
    (repo / "openspec" / "changes" / "demo" / "tasks.md").write_text("- [ ] 1.1 Write hello\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "plan")
    sha = git(repo, "rev-parse", "HEAD")
    conn = open_ledger(tmp_path / "ledger.db")
    conn.execute("INSERT INTO plans (repo, change, approved_sha) VALUES ('r', 'demo', ?)", (sha,))
    conn.execute("INSERT INTO tasks (plan_id, id) VALUES (1, '1.1')")
    conn.commit()
    return {"repo": repo, "conn": conn, "tmp": tmp_path}


def executor(setup, worker, **kw):
    kw.setdefault("check_sandbox", RecordingSandbox())
    return Executor(conn=setup["conn"], repo=setup["repo"], worker=worker,
                    work_root=setup["tmp"] / "work", **kw)


def plan(setup):
    return setup["conn"].execute("SELECT * FROM plans WHERE id = 1").fetchone()


def task_row(setup):
    return setup["conn"].execute("SELECT status, attempts FROM tasks WHERE id = '1.1'").fetchone()


def gates(setup):
    return setup["conn"].execute("SELECT id, kind, status, payload FROM gates ORDER BY id").fetchall()


TASK = TaskSpec("1.1", "Write hello", "grep -q hello hello.txt", "low")


def test_passing_task_completes_with_evidence(setup):
    worker = FakeWorker(files={"hello.txt": "hello\n"}, cost=0.02)
    ex = executor(setup, worker)
    assert ex.run_task(plan(setup), TASK) == "completed"
    assert tuple(task_row(setup)) == ("completed", 1)
    run = setup["conn"].execute("SELECT * FROM runs").fetchone()
    assert run["check_exit"] == 0 and run["cost_usd"] == 0.02 and run["model"] == "opencode/free"
    assert git(setup["repo"], "rev-parse", "ceo/demo/1.1") == run["commit_sha"]
    assert git(setup["repo"], "show", f"{run['commit_sha']}:hello.txt") == "hello"


def test_check_runs_in_the_sandbox_in_a_clean_export_of_the_commit(setup):
    sandbox = RecordingSandbox()
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}), check_sandbox=sandbox)
    ex.run_task(plan(setup), TASK)
    [(argv, cwd)] = sandbox.calls
    assert argv == ["bash", "-c", TASK.check]
    assert cwd.name == "1.1-1-check" and not (cwd / ".git").exists()


def test_failed_check_retries_then_escalates(setup):
    channel = FakeChannel()
    worker = FakeWorker(files={"hello.txt": "nope\n"})
    ex = executor(setup, worker, max_attempts=2, escalation_model="openrouter/strong", channel=channel)
    assert ex.run_task(plan(setup), TASK) == "retry"
    assert tuple(task_row(setup)) == ("pending", 1)
    assert ex.run_task(plan(setup), TASK) == "gate:escalation"
    assert tuple(task_row(setup)) == ("blocked", 2)
    [gate] = gates(setup)
    payload = json.loads(gate["payload"])
    assert (payload["from_model"], payload["to_model"], payload["attempts"]) == ("opencode/free", "openrouter/strong", 2)
    assert "Approval needed (escalation)" in channel.sent[-1]
    # The second prompt carried the first failure.
    assert "The last attempt failed: check failed" in worker.prompts[1]


def test_approved_escalation_retries_on_the_stronger_model(setup):
    worker = FakeWorker(files={"hello.txt": "nope\n"})
    ex = executor(setup, worker, max_attempts=1, escalation_model="openrouter/strong")
    assert ex.run_task(plan(setup), TASK) == "gate:escalation"
    decide_gate(setup["conn"], gates(setup)[0]["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert ex.resolve_gates(plan(setup), {"1.1": TASK}) == ["1.1:escalation:approved:retry"]
    assert task_row(setup)["status"] == "pending"
    worker.files = {"hello.txt": "hello\n"}
    assert ex.run_task(plan(setup), TASK) == "completed"
    assert worker.models_used == ["opencode/free", "openrouter/strong"]
    assert ex.resolve_gates(plan(setup), {"1.1": TASK}) == []  # applied once


def test_denied_escalation_stays_blocked(setup):
    ex = executor(setup, FakeWorker(files={"hello.txt": "nope\n"}), max_attempts=1)
    ex.run_task(plan(setup), TASK)
    decide_gate(setup["conn"], gates(setup)[0]["id"], "denied", route="chat", identity=ME,
                engineer=ENGINEER, reason="the plan needs a different check")
    assert ex.resolve_gates(plan(setup), {"1.1": TASK}) == ["1.1:escalation:denied:blocked"]
    assert task_row(setup)["status"] == "blocked"


def test_unsanctioned_assumptions_open_a_gate(setup):
    channel = FakeChannel()
    worker = FakeWorker(files={"hello.txt": "hello\n"}, text="Done.\nASSUMPTIONS:\n- port 8096 is free")
    ex = executor(setup, worker, channel=channel)
    assert ex.run_task(plan(setup), TASK) == "gate:unsanctioned_assumption"
    payload = json.loads(gates(setup)[0]["payload"])
    assert payload["assumptions"] == ["port 8096 is free"] and payload["check_exit"] == 0
    assert "port 8096 is free" in channel.sent[-1]


def test_approved_assumption_with_passing_check_completes(setup):
    worker = FakeWorker(files={"hello.txt": "hello\n"}, text="ASSUMPTIONS:\n- x")
    ex = executor(setup, worker)
    ex.run_task(plan(setup), TASK)
    decide_gate(setup["conn"], gates(setup)[0]["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert ex.resolve_gates(plan(setup), {"1.1": TASK}) == ["1.1:unsanctioned_assumption:approved:completed"]
    assert task_row(setup)["status"] == "completed"


def test_denied_assumption_retries_with_the_reason_in_the_prompt(setup):
    worker = FakeWorker(files={"hello.txt": "hello\n"}, text="ASSUMPTIONS:\n- use port 80")
    ex = executor(setup, worker)
    ex.run_task(plan(setup), TASK)
    decide_gate(setup["conn"], gates(setup)[0]["id"], "denied", route="chat", identity=ME,
                engineer=ENGINEER, reason="use port 8096, not 80")
    ex.resolve_gates(plan(setup), {"1.1": TASK})
    assert task_row(setup)["status"] == "pending"
    worker.text = "ASSUMPTIONS: none"
    assert ex.run_task(plan(setup), TASK) == "completed"
    assert "The engineer said: use port 8096, not 80" in worker.prompts[-1]


def test_high_blast_radius_needs_sign_off(setup):
    high = TaskSpec("1.1", "Write hello", "grep -q hello hello.txt", "high")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    assert ex.run_task(plan(setup), high) == "gate:high_radius"
    assert task_row(setup)["status"] == "blocked"
    decide_gate(setup["conn"], gates(setup)[0]["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    ex.resolve_gates(plan(setup), {"1.1": high})
    assert task_row(setup)["status"] == "completed"


def test_high_radius_denial_retries(setup):
    high = TaskSpec("1.1", "Write hello", "grep -q hello hello.txt", "high")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), high)
    decide_gate(setup["conn"], gates(setup)[0]["id"], "denied", route="chat", identity=ME,
                engineer=ENGINEER, reason="race condition in the retry loop")
    ex.resolve_gates(plan(setup), {"1.1": high})
    assert task_row(setup)["status"] == "pending"


@pytest.mark.parametrize(
    "worker, reason",
    [
        (FakeWorker(ok=False), "worker failed"),
        (FakeWorker(files={"hello.txt": "hello\n"}, text="Done, no section"), "ASSUMPTIONS"),
        (FakeWorker(files={}), "changed nothing and the check fails"),
    ],
)
def test_failed_attempts(setup, worker, reason):
    ex = executor(setup, worker, max_attempts=3)
    assert ex.run_task(plan(setup), TASK) == "retry"
    last = setup["conn"].execute(
        "SELECT json_extract(detail, '$.reason') FROM events WHERE kind = 'task.attempt_failed'").fetchone()[0]
    assert reason in last


def test_check_output_is_capped(setup):
    loud = TaskSpec("1.1", "x", "yes long-line-of-output | head -c 100000; true", "low")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), loud)
    assert len(setup["conn"].execute("SELECT check_output FROM runs").fetchone()[0]) == 20_000


def test_unapproved_plan_is_refused(setup):
    setup["conn"].execute("UPDATE plans SET approved_sha = NULL")
    with pytest.raises(ValueError, match="isn't approved"):
        executor(setup, FakeWorker()).run_task(plan(setup), TASK)


def test_decided_gate_on_a_task_that_isnt_blocked_is_ignored(setup):
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}, text="ASSUMPTIONS:\n- x"))
    ex.run_task(plan(setup), TASK)
    setup["conn"].execute("UPDATE tasks SET status = 'completed'")
    setup["conn"].commit()
    decide_gate(setup["conn"], gates(setup)[0]["id"], "denied", route="chat", identity=ME, engineer=ENGINEER, reason="no")
    assert ex.resolve_gates(plan(setup), {"1.1": TASK}) == ["1.1:unsanctioned_assumption:denied:ignored"]
    assert task_row(setup)["status"] == "completed"


def test_approving_an_assumption_does_not_pass_a_failed_check(setup):
    worker = FakeWorker(files={"hello.txt": "nope\n"}, text="ASSUMPTIONS:\n- x")
    ex = executor(setup, worker, max_attempts=3)
    assert ex.run_task(plan(setup), TASK) == "gate:unsanctioned_assumption"
    assert json.loads(gates(setup)[0]["payload"])["check_exit"] != 0
    decide_gate(setup["conn"], gates(setup)[0]["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert ex.resolve_gates(plan(setup), {"1.1": TASK}) == ["1.1:unsanctioned_assumption:approved:retry"]
    assert task_row(setup)["status"] == "pending"


# the integration branch: tasks build on each other's work


def add_task(setup, task_id):
    setup["conn"].execute("INSERT INTO tasks (plan_id, id) VALUES (1, ?)", (task_id,))
    setup["conn"].commit()


def test_a_later_task_starts_from_the_work_of_earlier_ones(setup):
    add_task(setup, "1.2")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    assert ex.run_task(plan(setup), TASK) == "completed"
    second = TaskSpec("1.2", "Add bye", "grep -q hello hello.txt && grep -q bye bye.txt", "low")
    ex.worker = FakeWorker(files={"bye.txt": "bye\n"})  # doesn't rewrite hello.txt
    assert ex.run_task(plan(setup), second) == "completed"
    tip = git(setup["repo"], "rev-parse", "ceo/demo/integration")
    assert git(setup["repo"], "show", f"{tip}:hello.txt") == "hello"
    assert git(setup["repo"], "show", f"{tip}:bye.txt") == "bye"
    assert git(setup["repo"], "rev-list", "--count", f"{plan(setup)['approved_sha']}..{tip}") == "2"


def test_a_result_built_on_older_work_is_redone_not_merged(setup):
    # 1.1 waits for sign-off while 1.2 completes; 1.1's commit no longer
    # fast-forwards, so it's redone on top instead of merged unchecked.
    add_task(setup, "1.2")
    high = TaskSpec("1.1", "Write hello", "grep -q hello hello.txt", "high")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    assert ex.run_task(plan(setup), high) == "gate:high_radius"
    ex.worker = FakeWorker(files={"bye.txt": "bye\n"})
    assert ex.run_task(plan(setup), TaskSpec("1.2", "Add bye", "grep -q bye bye.txt", "low")) == "completed"
    decide_gate(setup["conn"], gates(setup)[0]["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert ex.resolve_gates(plan(setup), {"1.1": high}) == ["1.1:high_radius:approved:retry"]
    assert task_row(setup)["status"] == "pending"
    kinds = [r[0] for r in setup["conn"].execute("SELECT kind FROM events")]
    assert "task.integration_stale" in kinds


def test_re_approval_restarts_the_integration_branch(setup):
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), TASK)
    # The plan is re-approved at a new commit that isn't built on the old work.
    (setup["repo"] / "openspec" / "changes" / "demo" / "tasks.md").write_text("- [ ] 1.1 Write hello v2\n")
    git(setup["repo"], "checkout", "-q", "main")
    git(setup["repo"], "commit", "-qam", "plan v2")
    new = git(setup["repo"], "rev-parse", "HEAD")
    setup["conn"].execute("UPDATE plans SET approved_sha = ?", (new,))
    setup["conn"].execute("UPDATE tasks SET status = 'pending'")
    setup["conn"].commit()
    ex.run_task(plan(setup), TASK)
    tip = git(setup["repo"], "rev-parse", "ceo/demo/integration")
    assert git(setup["repo"], "rev-parse", f"{tip}^") == new
    assert "integration.reset" in [r[0] for r in setup["conn"].execute("SELECT kind FROM events")]



def test_no_changes_but_the_check_passes_completes_without_a_commit(setup):
    # The work is already on the integration branch (an earlier task did it).
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), TASK)
    tip = git(setup["repo"], "rev-parse", "ceo/demo/integration")
    setup["conn"].execute("UPDATE tasks SET status = 'pending'")
    setup["conn"].commit()
    ex.worker = FakeWorker(files={})
    assert ex.run_task(plan(setup), TASK) == "completed"
    assert git(setup["repo"], "rev-parse", "ceo/demo/integration") == tip  # nothing new
