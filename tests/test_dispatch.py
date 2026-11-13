import os
import subprocess
from pathlib import Path

import pytest

from ceo.workers import (
    OpencodeWorker,
    Sandbox,
    build_prompt,
    parse_assumptions,
    parse_opencode_events,
    worker_from_env,
)
from ceo.workspace import commit_result, export

FIXTURE = Path(__file__).parent / "fixtures" / "opencode-run.jsonl"


# OpenCode's event stream (fixture recorded from opencode 1.19.5 on the box)


def test_parse_real_opencode_run():
    events = parse_opencode_events(FIXTURE.read_text())
    assert events["text"] == "DONE"
    assert events["tools"] == ["write"]
    assert events["errors"] == []
    assert events["cost"] == 0.0  # a free model


def test_parse_sums_costs_and_skips_log_lines():
    stdout = "\n".join([
        "[07:52:20.669] INFO something not json",
        '{"type":"step_finish","part":{"cost":0.0012}}',
        '{"type":"step_finish","part":{"cost":0.0030}}',
        '{"type":"step_finish","part":{"cost":-5}}',          # ignored: negative
        '{"type":"step_finish","part":{"cost":"lots"}}',      # ignored: not a number
        '{"type":"text","part":{"text":"first"}}',
        '{"type":"text","part":{"text":"second"}}',
        "not json at all {",
        "[1, 2, 3]",
    ])
    events = parse_opencode_events(stdout)
    assert events["cost"] == pytest.approx(0.0042)
    assert events["text"] == "first\nsecond"


def test_parse_errors():
    stdout = '{"type":"error","error":{"name":"APIError","data":{"message":"OpenCode 1.18.0 or newer is required"}}}'
    assert parse_opencode_events(stdout)["errors"] == ["OpenCode 1.18.0 or newer is required"]


# the assumptions contract (F12)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Done.\nASSUMPTIONS: none", (True, [])),
        ("Done.\nAssumptions: None.", (True, [])),
        ("Done.\nASSUMPTIONS:\n- port 8096 is free\n- 2. Jellyfin runs as its own user", (True, ["port 8096 is free", "Jellyfin runs as its own user"])),
        ("ASSUMPTIONS: none\nlater...\nASSUMPTIONS:\n* the last section counts", (True, ["the last section counts"])),
        ("Done, nothing else to say.", (False, [])),
    ],
)
def test_parse_assumptions(text, expected):
    assert parse_assumptions(text) == expected


def test_prompt_carries_task_check_and_contract():
    prompt = build_prompt(task_id="1.2", description="Add the ledger", check="pytest tests/x.py", change_dir="openspec/changes/c")
    assert "Task 1.2: Add the ledger" in prompt and "pytest tests/x.py" in prompt
    assert "ASSUMPTIONS: none" in prompt and "Don't commit" in prompt


# sandbox and worker


def test_sandbox_runs_as_the_worker_user_without_prompting():
    assert Sandbox(user="ceo-worker").command(["opencode", "run", "x"]) == [
        "sudo", "-n", "-H", "-u", "ceo-worker", "--", "opencode", "run", "x",
    ]
    assert Sandbox(user=None).command(["opencode"]) == ["opencode"]


def test_sandbox_puts_the_tool_path_first_inside_sudo():
    command = Sandbox(user="ceo-worker", path="/opt/ceo/.venv/bin").command(["python", "-m", "pytest"])
    assert command[:6] == ["sudo", "-n", "-H", "-u", "ceo-worker", "--"]
    assert command[6:8] == ["env", "PATH=/opt/ceo/.venv/bin:/usr/local/bin:/usr/bin:/bin"]
    assert command[8:] == ["python", "-m", "pytest"]


def test_running_directly_keeps_the_inherited_path_after_the_tool_path(monkeypatch):
    monkeypatch.setenv("PATH", "/home/me/.opencode/bin:/usr/bin")
    command = Sandbox(user=None, path="/venv/bin").command(["opencode"])
    assert command == ["env", "PATH=/venv/bin:/home/me/.opencode/bin:/usr/bin", "opencode"]


def test_running_directly_keeps_an_explicit_environments_path():
    command = Sandbox(user=None, path="/venv/bin").command(["x"], env={"PATH": "/custom/bin"})
    assert command == ["env", "PATH=/venv/bin:/custom/bin", "x"]


def test_checks_find_tools_on_the_configured_path(tmp_path):
    tool = tmp_path / "bin"
    tool.mkdir()
    (tool / "mytool").write_text("#!/bin/sh\necho found\n")
    (tool / "mytool").chmod(0o755)
    result = Sandbox(user=None, path=str(tool)).run(["sh", "-c", "mytool"], cwd=tmp_path, timeout=10)
    assert result.stdout == "found\n"


def test_sandbox_run_returns_output_and_exit_code(tmp_path):
    result = Sandbox(user=None).run(["sh", "-c", "echo out; echo err >&2; exit 3"], cwd=tmp_path, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); treat it as gone.
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(status) and not status.startswith("Z")


def test_timeout_kills_the_whole_process_tree(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pidfile}; sleep 60"
    result = Sandbox(user=None).run(["sh", "-c", script], cwd=tmp_path, timeout=0.5)
    assert result.returncode == 124
    child = int(pidfile.read_text())
    import time
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child)


def test_background_children_are_cleaned_up_after_a_normal_exit(tmp_path):
    pidfile = tmp_path / "child.pid"
    # The child doesn't hold the output pipe, so the command exits normally.
    script = f"sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}"
    result = Sandbox(user=None).run(["sh", "-c", script], cwd=tmp_path, timeout=10)
    assert result.returncode == 0
    child = int(pidfile.read_text())
    import time
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child)


def test_sandbox_timeout_is_a_result_not_an_exception(tmp_path):
    result = Sandbox(user=None).run(["sleep", "5"], cwd=tmp_path, timeout=0.2)
    assert result.returncode == 124 and "timed out" in result.stderr


def fake_opencode(tmp_path, body: str) -> str:
    script = tmp_path / "opencode"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script)


def test_worker_runs_opencode_and_reports(tmp_path):
    fixture = FIXTURE.read_text().replace("DONE", "DONE\\nASSUMPTIONS: none")
    (tmp_path / "events.jsonl").write_text(fixture)
    binary = fake_opencode(tmp_path, f'echo "$@" > "{tmp_path}/args"; cat "{tmp_path}/events.jsonl"\n')
    worker = OpencodeWorker(Sandbox(user=None), model="opencode/x-free", binary=binary, timeout=10)
    result = worker.run("do the task", tmp_path)
    assert result.ok and result.text.startswith("DONE") and result.reported_assumptions
    assert result.tools == ["write"] and result.model == "opencode/x-free"
    args = (tmp_path / "args").read_text()
    assert "run --pure --format json -m opencode/x-free --dir" in args and "do the task" in args
    assert "--dangerously-skip-permissions" not in args


def test_worker_failure_is_reported(tmp_path):
    binary = fake_opencode(tmp_path, 'echo "boom" >&2; exit 3\n')
    result = OpencodeWorker(Sandbox(user=None), binary=binary, timeout=10).run("x", tmp_path)
    assert not result.ok and result.errors == ["exit 3: boom"]


def test_worker_from_env():
    worker = worker_from_env({"CEO_WORKER_MODEL": "openrouter/x", "CEO_WORKER_USER": "w", "CEO_WORKER_TIMEOUT": "60",
                              "CEO_WORKER_PATH": "/opt/ceo/.venv/bin"})
    assert (worker.model, worker.sandbox.user, worker.timeout) == ("openrouter/x", "w", 60.0)
    assert worker.sandbox.path == "/opt/ceo/.venv/bin"
    assert worker_from_env({"CEO_WORKER_USER": ""}).sandbox.user is None


# workspaces: the worker never touches git, and can't smuggle files in


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "plans"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    for k, v in (("user.name", "E"), ("user.email", "e@x"), ("commit.gpgsign", "false")):
        git(path, "config", k, v)
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('v1')\n")
    (path / "README.md").write_text("plans\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def test_export_has_files_but_no_git(repo, tmp_path):
    work = export(repo, "HEAD", tmp_path / "work")
    assert (work / "src" / "app.py").read_text() == "print('v1')\n"
    assert not (work / ".git").exists()
    assert (work / "src" / "app.py").stat().st_mode & 0o020  # group-writable for the worker


def test_commit_result_is_one_commit_on_a_task_branch(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    work = export(repo, base, tmp_path / "work")
    (work / "src" / "app.py").write_text("print('v2')\n")
    (work / "NEW.md").write_text("new\n")
    (work / "README.md").unlink()
    sha = commit_result(repo, base, work, branch="ceo/plan/1.1", message="1.1: do it")
    assert git(repo, "rev-parse", "ceo/plan/1.1") == sha
    assert git(repo, "rev-parse", f"{sha}^") == base
    assert git(repo, "show", f"{sha}:src/app.py") == "print('v2')"
    files = git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    assert sorted(files) == ["NEW.md", "src/app.py"]
    assert git(repo, "log", "-1", "--format=%an %ae %s", sha) == "CEO ceo@localhost 1.1: do it"
    assert git(repo, "worktree", "list").count("\n") == 0  # temporary worktree cleaned up


def test_no_changes_means_no_commit(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    work = export(repo, base, tmp_path / "work")
    assert commit_result(repo, base, work, branch="ceo/plan/1.1", message="x") is None


def test_worker_planted_git_and_hooks_are_ignored(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    work = export(repo, base, tmp_path / "work")
    # A worker plants git metadata hoping CEO will use it (and run its hooks).
    (work / ".git").mkdir()
    hook = work / ".git" / "hooks"
    hook.mkdir()
    marker = tmp_path / "pwned"
    (hook / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hook / "pre-commit").chmod(0o755)
    (work / "sub").mkdir()
    (work / "sub" / ".git").write_text("gitdir: /tmp/elsewhere\n")
    (work / "sub" / "ok.txt").write_text("ok\n")
    sha = commit_result(repo, base, work, branch="ceo/plan/1.1", message="x")
    files = git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    assert "sub/ok.txt" in files and not any(".git" in f.split("/") for f in files)
    assert not marker.exists()


def test_symlinks_are_kept_as_links_not_followed(repo, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    base = git(repo, "rev-parse", "HEAD")
    work = export(repo, base, tmp_path / "work")
    os.symlink(secret, work / "leak")
    os.symlink(tmp_path, work / "leakdir")
    sha = commit_result(repo, base, work, branch="ceo/plan/1.1", message="x")
    # The links are committed as links (git mode 120000), never as copied content.
    entries = git(repo, "ls-tree", sha).splitlines()
    assert any(e.startswith("120000") and e.endswith("\tleak") for e in entries)
    assert any(e.startswith("120000") and e.endswith("\tleakdir") for e in entries)
    assert "TOP SECRET" not in git(repo, "show", f"{sha}:leak")


def test_special_files_are_dropped(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    work = export(repo, base, tmp_path / "work")
    os.mkfifo(work / "pipe")
    (work / "real.txt").write_text("x\n")
    sha = commit_result(repo, base, work, branch="ceo/plan/1.1", message="x")
    files = git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    assert "real.txt" in files and "pipe" not in files


def test_hooks_in_ceos_own_repo_never_run(repo, tmp_path):
    # --no-verify skips pre-commit but not post-commit; hooksPath=/dev/null skips all.
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    base = git(repo, "rev-parse", "HEAD")
    work = export(repo, base, tmp_path / "work")
    (work / "x.txt").write_text("x\n")
    assert commit_result(repo, base, work, branch="ceo/plan/1.1", message="x")
    assert not marker.exists()


def test_timeout_returns_even_if_group_signals_cannot_reach_the_worker(tmp_path):
    # Under sudo, CEO's signals reach only sudo; the worker's processes die
    # only through the worker-user cleanup. Simulate that: group signals do
    # nothing, cleanup kills. A process that ignores TERM and holds the
    # output pipe must not hang the run.
    import threading

    class SudoLike(Sandbox):
        def _kill_group(self, pgid, sig):
            pass  # CEO can't signal the worker's processes

        def _clean_up(self, pgid):
            try:
                os.killpg(pgid, 9)  # what `pkill -KILL -u ceo-worker` achieves
            except ProcessLookupError:
                pass

    result = {}
    sandbox = SudoLike(user=None)
    thread = threading.Thread(target=lambda: result.update(
        r=sandbox.run(["sh", "-c", "trap '' TERM; sleep 60"], cwd=tmp_path, timeout=0.5)), daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "Sandbox.run hung after the timeout"
    assert result["r"].returncode == 124
