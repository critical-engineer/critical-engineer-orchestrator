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

    def run(self, argv, *, cwd, timeout, env=None, **kwargs):
        self.calls.append((argv, Path(cwd)))
        return super().run(argv, cwd=cwd, timeout=timeout, env=env, **kwargs)


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


def reapprove(setup, text="- [ ] 1.1 Write hello v2\n"):
    """A plan change committed on main and approved, not built on the old integration tip."""
    (setup["repo"] / "openspec" / "changes" / "demo" / "tasks.md").write_text(text)
    git(setup["repo"], "checkout", "-q", "main")
    git(setup["repo"], "commit", "-qam", "plan v2")
    new = git(setup["repo"], "rev-parse", "HEAD")
    setup["conn"].execute("UPDATE plans SET approved_sha = ?", (new,))
    setup["conn"].commit()
    return new


def test_re_approval_restarts_the_branch_and_reruns_completed_tasks(setup):
    # Production path: nothing resets the tasks by hand.
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    assert ex.run_task(plan(setup), TASK) == "completed"
    new = reapprove(setup)
    assert ex.ensure_integration(plan(setup)) is True
    assert task_row(setup)["status"] == "pending"  # completed work runs again on the new base
    assert git(setup["repo"], "rev-parse", "ceo/demo/integration") == new
    kinds = [r[0] for r in setup["conn"].execute("SELECT kind FROM events")]
    assert "integration.reset" in kinds and "task.rerun" in kinds
    assert ex.run_task(plan(setup), TASK) == "completed"
    tip = git(setup["repo"], "rev-parse", "ceo/demo/integration")
    assert git(setup["repo"], "rev-parse", f"{tip}^") == new


def test_a_task_never_runs_on_a_base_whose_dependencies_were_just_sent_back(setup):
    add_task(setup, "1.2")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), TASK)
    reapprove(setup)
    # The loop normally calls ensure_integration first; if it didn't, the
    # attempt defers instead of running without 1.1's work.
    assert ex.run_task(plan(setup), TaskSpec("1.2", "x", "true", "low")) == "deferred"
    assert setup["conn"].execute("SELECT count(*) FROM runs").fetchone()[0] == 1

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


# failures inside an attempt (CodeRabbit review on #15)


@dataclass
class RaisingWorker(FakeWorker):
    def run(self, prompt, workdir):
        raise RuntimeError("worker crashed")


def test_an_exception_in_the_attempt_is_a_failed_attempt(setup):
    ex = executor(setup, RaisingWorker(), max_attempts=1)
    assert ex.run_task(plan(setup), TASK) == "gate:escalation"
    assert task_row(setup)["status"] == "blocked"  # not left 'running'
    run = setup["conn"].execute("SELECT finished_at FROM runs").fetchone()
    assert run["finished_at"] is not None


def test_cost_is_recorded_even_if_a_later_step_fails(setup, monkeypatch):
    import ceo.execute

    def broken_commit(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ceo.execute, "commit_result", broken_commit)
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}, cost=0.37), max_attempts=2)
    assert ex.run_task(plan(setup), TASK) == "retry"
    assert setup["conn"].execute("SELECT cost_usd FROM runs").fetchone()[0] == 0.37
    reason = setup["conn"].execute(
        "SELECT json_extract(detail, '$.reason') FROM events WHERE kind = 'task.attempt_failed'").fetchone()[0]
    assert "disk full" in reason


# git errors are failures, never "no" (CodeRabbit review on #15)


def test_git_errors_raise_instead_of_answering_no(setup):
    from ceo.workspace import WorkspaceError, branch_tip, is_ancestor

    repo = setup["repo"]
    assert branch_tip(repo, "no/such/branch") is None          # a genuine "no"
    assert is_ancestor(repo, plan(setup)["approved_sha"], plan(setup)["approved_sha"])
    with pytest.raises(WorkspaceError):
        is_ancestor(repo, "f" * 40, plan(setup)["approved_sha"])  # unknown object: exit 128
    with pytest.raises(WorkspaceError):
        branch_tip(setup["tmp"] / "not-a-repo", "main")


def test_a_broken_repo_never_resets_the_integration_branch(setup, monkeypatch):
    import ceo.execute
    from ceo.workspace import WorkspaceError

    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), TASK)
    tip = git(setup["repo"], "rev-parse", "ceo/demo/integration")

    def broken(*args, **kwargs):
        raise WorkspaceError("git merge-base failed: fatal: bad object")

    monkeypatch.setattr(ceo.execute, "is_ancestor", broken)
    setup["conn"].execute("UPDATE tasks SET status = 'pending'")
    setup["conn"].commit()
    with pytest.raises(WorkspaceError):
        ex.run_task(plan(setup), TASK)
    assert git(setup["repo"], "rev-parse", "ceo/demo/integration") == tip  # not reset


def test_each_run_records_the_check_it_ran(setup):
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), TASK)
    assert setup["conn"].execute("SELECT check_command FROM runs").fetchone()[0] == TASK.check



# review round 3 (CodeRabbit on #15)


def test_check_output_is_bounded_while_it_runs(setup):
    # `yes` prints without end; only the tail is kept, so memory stays bounded.
    endless = TaskSpec("1.1", "x", "yes 'spam spam' | head -c 50000000; true", "low")  # 50 MB
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), endless)
    output = setup["conn"].execute("SELECT check_output FROM runs").fetchone()[0]
    assert len(output) <= 20_000 and output.endswith("spam spam\n")


def test_sandbox_keeps_only_the_tail_of_large_output(tmp_path):
    result = Sandbox(user=None).run(["bash", "-c", "head -c 5000000 /dev/zero | tr '\\0' a; echo END"],
                                    cwd=tmp_path, timeout=30, max_output=1000)
    assert result.stdout.startswith("[... earlier output truncated ...]") and result.stdout.endswith("END\n")
    assert len(result.stdout) < 1100


def test_a_gate_decided_under_an_older_approval_completes_nothing(setup):
    high = TaskSpec("1.1", "Write hello", "grep -q hello hello.txt", "high")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    assert ex.run_task(plan(setup), high) == "gate:high_radius"
    gate_id = gates(setup)[0]["id"]
    reapprove(setup)                       # the plan moved on before the decision
    decide_gate(setup["conn"], gate_id, "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert ex.resolve_gates(plan(setup), {"1.1": high}) == ["1.1:high_radius:approved:superseded"]
    assert task_row(setup)["status"] == "pending"
    assert git(setup["repo"], "rev-parse", "--verify", "-q", "ceo/demo/integration") != gates(setup)[0]["payload"]


def test_a_gate_decided_under_an_older_task_definition_completes_nothing(setup):
    high = TaskSpec("1.1", "Write hello", "grep -q hello hello.txt", "high")
    setup["conn"].execute("UPDATE tasks SET definition_hash = 'v1'")
    setup["conn"].commit()
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), high)
    setup["conn"].execute("UPDATE tasks SET definition_hash = 'v2'")   # sync saw a new definition
    setup["conn"].commit()
    decide_gate(setup["conn"], gates(setup)[0]["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert ex.resolve_gates(plan(setup), {"1.1": high}) == ["1.1:high_radius:approved:superseded"]
    assert task_row(setup)["status"] == "pending"


def test_reapplying_a_gate_after_a_crash_does_not_duplicate_its_successor(setup):
    high = TaskSpec("1.1", "Write hello", "grep -q hello hello.txt", "high")
    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}, text="ASSUMPTIONS:\n- x"))
    ex.run_task(plan(setup), high)                      # assumption gate
    assumption = gates(setup)[0]
    decide_gate(setup["conn"], assumption["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    gate_row = setup["conn"].execute("SELECT * FROM gates WHERE id = ?", (assumption["id"],)).fetchone()
    # Apply twice, as if CEO crashed before recording that it had applied it.
    for _ in range(2):
        ex._after_gate = assumption["id"]
        try:
            assert ex._apply(plan(setup), high, gate_row) == "gate:high_radius"
        finally:
            ex._after_gate = None
    assert [g["kind"] for g in gates(setup)] == ["unsanctioned_assumption", "high_radius"]


# review round 4 (CodeRabbit on #15)


def test_reset_records_the_reruns_before_moving_the_branch(setup, monkeypatch):
    # If CEO stops between the two steps, the next call must still find work to do.
    import ceo.execute

    ex = executor(setup, FakeWorker(files={"hello.txt": "hello\n"}))
    ex.run_task(plan(setup), TASK)
    reapprove(setup)

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(ceo.execute, "set_branch", crash)
    with pytest.raises(KeyboardInterrupt):
        ex.ensure_integration(plan(setup))
    assert task_row(setup)["status"] == "pending"   # recorded before the branch moved
    monkeypatch.undo()
    ex.ensure_integration(plan(setup))               # the branch move is retried
    assert git(setup["repo"], "rev-parse", "ceo/demo/integration") == plan(setup)["approved_sha"]


def test_a_superseded_escalation_does_not_choose_the_model(setup):
    worker = FakeWorker(files={"hello.txt": "nope\n"})
    ex = executor(setup, worker, max_attempts=1, escalation_model="openrouter/strong")
    ex.run_task(plan(setup), TASK)                   # escalation gate
    decide_gate(setup["conn"], gates(setup)[0]["id"], "approved", route="chat", identity=ME, engineer=ENGINEER)
    reapprove(setup)                                 # the plan moved on
    ex.ensure_integration(plan(setup))
    setup["conn"].execute("UPDATE tasks SET status = 'pending'")
    setup["conn"].commit()
    ex.run_task(plan(setup), TASK)
    assert worker.models_used[-1] == "opencode/free"  # not the superseded escalation's model
