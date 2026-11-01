"""Task-label parser for an OpenSpec change's ``tasks.md``.

OpenSpec's JSON gives tasks positional ids ("1", "2", ...) that shift when a
task is inserted (T0 finding R4, F9). CEO keys everything on the ``X.Y``
label written in the task line instead.
"""

import re
from dataclasses import dataclass
from pathlib import Path

_CHECKBOX = re.compile(r"^\s*- \[([ xX])\]\s+(.*)$")
_LABEL = re.compile(r"^(\d+\.\d+)\s+(\S.*)$")


@dataclass(frozen=True)
class Problem:
    file: str
    line: int | None
    message: str

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{where}: {self.message}"


@dataclass(frozen=True)
class Task:
    id: str
    description: str
    done: bool
    line: int


def parse_tasks(text: str, file: str = "tasks.md") -> tuple[list[Task], list[Problem]]:
    tasks: list[Task] = []
    problems: list[Problem] = []
    seen: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        box = _CHECKBOX.match(line)
        if not box:
            continue
        label = _LABEL.match(box.group(2).strip())
        if not label:
            problems.append(Problem(file, lineno, "task has no X.Y label"))
            continue
        task_id, description = label.groups()
        if task_id in seen:
            problems.append(
                Problem(file, lineno, f"duplicate task {task_id} (first on line {seen[task_id]})")
            )
            continue
        seen[task_id] = lineno
        tasks.append(Task(task_id, description, box.group(1) != " ", lineno))
    if not tasks and not problems:
        problems.append(Problem(file, None, "no tasks found"))
    return tasks, problems


def load_tasks(path: Path) -> tuple[list[Task], list[Problem]]:
    return parse_tasks(path.read_text(encoding="utf-8"), file=path.name)
