"""Workers: existing coding agents run headless on one task each (T9, D4).

A worker never holds CEO's credentials and can't write the ledger or the plan
(C4): it runs as a separate OS user (``ceo-worker``) through a sandbox runner,
in a per-task workspace, and only CEO commits what it produced (D8).

The default worker is OpenCode (``opencode run --format json``). Its event
stream gives the final text, the tools it used and the cost of each step
(OQ5). The worker contract: end with an ``ASSUMPTIONS:`` section listing any
assumption the plan didn't sanction, or ``ASSUMPTIONS: none``. Each listed
assumption becomes a judgment gate (F12).
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"

_ASSUMPTIONS_LINE = re.compile(r"\s*ASSUMPTIONS:\s*(.*)", re.IGNORECASE)
_LIST_MARKER = re.compile(r"^\s*(?:(?:[-*]|\d+[.)])\s+)+")


@dataclass(frozen=True)
class Sandbox:
    """Runs commands as the worker user. ``user=None`` runs them directly (tests, dev).

    A command runs in its own session. When it finishes or times out, nothing it
    started is left running: as the worker user, every process of that user is
    killed (tasks run one at a time, so the worker user runs nothing else);
    without a worker user, the command's process group is killed.
    """

    user: str | None = "ceo-worker"
    # Put first on PATH for every command: sudo resets PATH, and checks need
    # tools such as pytest (from CEO's own venv on the box).
    path: str | None = None

    def command(self, argv: list[str], env: dict | None = None) -> list[str]:
        if self.path:
            # Under sudo the environment is reset, so the rest is the system
            # path; running directly (dev, tests), the inherited PATH follows,
            # so tools like a user-installed opencode are still found.
            inherited = (os.environ if env is None else env).get("PATH", "/usr/bin:/bin")
            rest = "/usr/local/bin:/usr/bin:/bin" if self.user else inherited
            argv = ["env", f"PATH={self.path}:{rest}", *argv]
        if not self.user:
            return list(argv)
        # -n: never prompt; the sudoers rule allows exactly this (D5).
        # -H: the worker's own HOME, so opencode uses the worker's config.
        return ["sudo", "-n", "-H", "-u", self.user, "--", *argv]

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict | None = None,
        max_output: int = 10_000_000,
    ) -> subprocess.CompletedProcess:
        """Run a command; keep at most the last ``max_output`` bytes of each stream.

        Output is read while the command runs, so a command that prints without
        end (``yes``) can't grow CEO's memory: only the tail is kept.
        """
        command = self.command(argv, env)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        out, err = _Tail(max_output), _Tail(max_output)
        readers = [
            threading.Thread(target=out.drain, args=(process.stdout,), daemon=True),
            threading.Thread(target=err.drain, args=(process.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()
        note = ""
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(process.pid, signal.SIGTERM)  # sudo relays TERM to the command
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_group(process.pid, signal.SIGKILL)
                # As CEO, SIGKILL reaches only sudo: the worker's own processes
                # can only be killed as the worker user. Do that before waiting
                # again, or a process holding the output pipe keeps it open.
                self._clean_up(process.pid)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            code, note = 124, f"\ntimed out after {timeout}s"
        finally:
            self._clean_up(process.pid)
        for reader in readers:
            reader.join(timeout=10)
        if any(reader.is_alive() for reader in readers):
            # Something still holds the pipes: stop reading rather than block.
            note += "\noutput may be incomplete: a process kept the output open"
        return subprocess.CompletedProcess(command, code, out.text(), err.text() + note)

    @staticmethod
    def _kill_group(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _clean_up(self, pgid: int) -> None:
        """Leave nothing running that this command started."""
        if self.user:
            subprocess.run(
                ["sudo", "-n", "-u", self.user, "--", "pkill", "-KILL", "-u", self.user],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            )
        else:
            self._kill_group(pgid, signal.SIGKILL)


class _Tail:
    """Collects a stream's output, keeping only the last ``limit`` bytes."""

    def __init__(self, limit: int):
        self.limit, self.data, self.truncated = limit, bytearray(), False

    def drain(self, pipe) -> None:
        try:
            while chunk := pipe.read1(65536):
                self.data += chunk
                if len(self.data) > self.limit:
                    del self.data[: len(self.data) - self.limit]
                    self.truncated = True
        except (OSError, ValueError):
            pass  # the pipe was closed under us

    def text(self) -> str:
        text = self.data.decode("utf-8", "replace")
        return ("[... earlier output truncated ...]\n" + text) if self.truncated else text


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


@dataclass
class WorkResult:
    exit_code: int
    text: str  # the worker's final message
    cost_usd: float
    model: str
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    reported_assumptions: bool = False  # did it follow the contract at all?

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.errors


def parse_opencode_events(stdout: str) -> dict:
    """Read ``opencode run --format json`` output. Non-JSON lines (logs) are skipped."""
    texts, tools, errors, cost = [], [], [], 0.0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        kind = event.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "tool_use" and isinstance(part.get("tool"), str):
            tools.append(part["tool"])
        elif kind == "step_finish":
            step_cost = part.get("cost")
            if isinstance(step_cost, (int, float)) and step_cost >= 0:
                cost += float(step_cost)
        elif kind == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            errors.append(str(data.get("message") or error.get("name") or "unknown error"))
    return {"text": "\n".join(texts).strip(), "tools": tools, "errors": errors, "cost": cost}


def parse_assumptions(text: str) -> tuple[bool, list[str]]:
    """(followed the contract?, assumptions) from the worker's final message."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _ASSUMPTIONS_LINE.fullmatch(line)]
    if not starts:
        return False, []
    start = starts[-1]  # the last ASSUMPTIONS: section counts
    body = [_ASSUMPTIONS_LINE.fullmatch(lines[start]).group(1), *lines[start + 1 :]]
    items = [_LIST_MARKER.sub("", line).strip() for line in body]
    items = [item for item in items if item]
    if not items or (len(items) == 1 and items[0].lower().rstrip(".") in {"none", "no", "n/a"}):
        return True, []
    return True, items


def build_prompt(
    *, task_id: str, description: str, check: str, change_dir: str, feedback: list[str] = ()
) -> str:
    earlier = ""
    if feedback:
        lines = "\n".join(f"- {item}" for item in feedback)
        earlier = f"\nEarlier attempts at this task were not accepted. Take this into account:\n{lines}\n"
    return f"""You are a worker in the Critical Engineer Orchestrator. Do exactly one task from an approved plan.

Task {task_id}: {description}
{earlier}
The plan is in {change_dir}/ (proposal.md, design.md, specs/, tasks.md). Read what you need.
Respect its constraints (C<n>) and decisions (D<n>); don't change the plan itself.

When you are done, this check will be run in a clean checkout of your work, and the task
only counts as done if it passes:
    {check}

Rules:
- Work only inside the current directory. Don't commit; CEO commits your changes.
- Don't touch other tasks, CI configuration or files the task doesn't need.
- If something is ambiguous, choose the simplest reading and say so below.

End your final message with an ASSUMPTIONS: section listing every assumption you made
that the plan didn't sanction, one per line, or exactly "ASSUMPTIONS: none".
"""


@dataclass
class OpencodeWorker:
    sandbox: Sandbox
    model: str = DEFAULT_MODEL
    binary: str = "opencode"
    timeout: float = 1800

    name = "opencode"

    def run(self, prompt: str, workdir: Path) -> WorkResult:
        argv = [self.binary, "run", "--pure", "--format", "json", "-m", self.model, "--dir", str(workdir), prompt]
        result = self.sandbox.run(argv, cwd=workdir, timeout=self.timeout)
        events = parse_opencode_events(result.stdout)
        errors = list(events["errors"])
        if result.returncode != 0 and not errors:
            errors.append(f"exit {result.returncode}: {result.stderr.strip()[-500:]}")
        reported, assumptions = parse_assumptions(events["text"])
        return WorkResult(
            exit_code=result.returncode,
            text=events["text"],
            cost_usd=events["cost"],
            model=self.model,
            tools=events["tools"],
            errors=errors,
            assumptions=assumptions,
            reported_assumptions=reported,
        )


def worker_from_env(env: dict[str, str] | None = None) -> OpencodeWorker:
    env = os.environ if env is None else env
    user = env.get("CEO_WORKER_USER", "ceo-worker")
    # By default the worker (and every check) sees CEO's own venv first, which
    # on the box has pytest; under `python -m` the task's exported code still
    # shadows the installed copy, because the current directory comes first.
    path = env.get("CEO_WORKER_PATH", os.path.dirname(sys.executable))
    return OpencodeWorker(
        sandbox=Sandbox(user=user or None, path=path or None),
        model=env.get("CEO_WORKER_MODEL", DEFAULT_MODEL),
        binary=env.get("CEO_OPENCODE_BIN", "opencode"),
        timeout=float(env.get("CEO_WORKER_TIMEOUT", "1800")),
    )
