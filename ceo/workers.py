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

    def command(self, argv: list[str]) -> list[str]:
        if not self.user:
            return list(argv)
        # -n: never prompt; the sudoers rule allows exactly this (D5).
        # -H: the worker's own HOME, so opencode uses the worker's config.
        return ["sudo", "-n", "-H", "-u", self.user, "--", *argv]

    def run(self, argv: list[str], *, cwd: Path, timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
        command = self.command(argv)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            self._kill_group(process.pid, signal.SIGTERM)  # sudo relays TERM to the command
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_group(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            code, stderr = 124, (stderr or "") + f"\ntimed out after {timeout}s"
        finally:
            self._clean_up(process.pid)
        return subprocess.CompletedProcess(command, code, stdout or "", stderr or "")

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
    return OpencodeWorker(
        sandbox=Sandbox(user=user or None),
        model=env.get("CEO_WORKER_MODEL", DEFAULT_MODEL),
        binary=env.get("CEO_OPENCODE_BIN", "opencode"),
        timeout=float(env.get("CEO_WORKER_TIMEOUT", "1800")),
    )
