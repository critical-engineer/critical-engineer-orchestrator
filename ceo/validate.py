"""CE validator: checks what ``openspec validate --strict`` does not (T0 R5).

Run after OpenSpec's own validation. For a change directory it checks that:

- ``tasks.md`` has unique ``X.Y`` labels;
- ``design.md`` exists with Constraints (C<n>), Failure Modes (F<n> table rows
  whose mitigation names a constraint, decision or task) and Decisions (D<n>,
  each listing rejected alternatives);
- ``ceo.yaml`` has exactly one entry per task, each with a ``check``, a valid
  ``blast_radius`` and ``depends_on`` naming known tasks, without cycles.

Usage: python -m ceo.validate <change-dir> [<change-dir> ...]
"""

import re
import sys
from pathlib import Path

import yaml

from ceo.tasks import Problem, load_tasks

BLAST_RADII = ("low", "medium", "high")
_ENTRY_KEYS = {"depends_on", "blast_radius", "check"}

_SECTION = re.compile(r"^##\s+(.+?)\s*$")
_ITEM = re.compile(r"^- ([CD]\d+)\.(.*)$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CELL_SEPARATOR = re.compile(r"(?<!\\)\|")
_FAILURE_ID = re.compile(r"^F\d+$")
_REJECTED = re.compile(r"rejected[\s:]*[^\s:]", re.IGNORECASE)
_REFERENCE = re.compile(r"\b(?:[CD]\d+|\d+\.\d+)\b")


def validate_change(change_dir: Path) -> list[Problem]:
    tasks_path = change_dir / "tasks.md"
    if not tasks_path.is_file():
        return [Problem("tasks.md", None, "missing")]
    tasks, problems = load_tasks(tasks_path)
    task_ids = {t.id for t in tasks}
    problems += _validate_design(change_dir / "design.md", task_ids)
    problems += _validate_ceo_yaml(change_dir / "ceo.yaml", task_ids)
    return problems


def _sections(lines: list[str]) -> dict[str, list[tuple[int, str]]]:
    sections: dict[str, list[tuple[int, str]]] = {}
    current = None
    for lineno, line in enumerate(lines, start=1):
        heading = _SECTION.match(line)
        if heading:
            current = heading.group(1).lower()
            sections[current] = []
        elif current is not None:
            sections[current].append((lineno, line))
    return sections


def _items(body: list[tuple[int, str]], prefix: str) -> list[tuple[int, str, str]]:
    """Numbered list items (``- C1. ...``) with their continuation lines."""
    items: list[tuple[int, str, str]] = []
    for lineno, line in body:
        match = _ITEM.match(line)
        if match and match.group(1).startswith(prefix):
            items.append((lineno, match.group(1), match.group(2)))
        elif items and line.startswith("  "):
            start, item_id, text = items[-1]
            items[-1] = (start, item_id, text + "\n" + line)
    return items


def _validate_design(path: Path, task_ids: set[str]) -> list[Problem]:
    file = path.name
    if not path.is_file():
        return [Problem(file, None, "missing (required by the critical-engineer schema)")]
    # Template placeholders are HTML comments; blank them, keeping line numbers.
    text = _COMMENT.sub(lambda m: "\n" * m.group().count("\n"), path.read_text(encoding="utf-8"))
    sections = _sections(text.splitlines())
    problems: list[Problem] = []
    for name in ("constraints", "failure modes", "decisions"):
        if name not in sections:
            problems.append(Problem(file, None, f"missing section '## {name.title()}'"))

    defined: set[str] = set(task_ids)

    constraints = _items(sections.get("constraints", []), "C")
    decisions = _items(sections.get("decisions", []), "D")
    for kind, items in (("constraints", constraints), ("decisions", decisions)):
        if kind in sections and not items:
            problems.append(Problem(file, None, f"no numbered {kind} (e.g. '- {kind[0].upper()}1. ...')"))
        for lineno, item_id, body in items:
            if item_id in defined:
                problems.append(Problem(file, lineno, f"duplicate {item_id}"))
            elif not body.split("\n", 1)[0].strip():
                problems.append(Problem(file, lineno, f"{item_id} is empty"))
            else:
                defined.add(item_id)

    for lineno, item_id, body in decisions:
        if body.strip() and not _REJECTED.search(body):
            problems.append(Problem(file, lineno, f"{item_id} lists no rejected alternatives"))

    rows = []
    for lineno, line in sections.get("failure modes", []):
        cells = _table_cells(line)
        if cells and _FAILURE_ID.match(cells[0]):
            if len(cells) == 3:
                rows.append((lineno, cells))
            else:
                problems.append(
                    Problem(file, lineno, f"{cells[0]} row has {len(cells)} cells, expected | ID | failure | mitigation |")
                )
    if "failure modes" in sections and not rows:
        problems.append(Problem(file, None, "no failure mode rows (| F1 | failure | mitigation |)"))
    seen_f: set[str] = set()
    for lineno, (f_id, _, mitigation) in rows:
        if f_id in seen_f:
            problems.append(Problem(file, lineno, f"duplicate {f_id}"))
        seen_f.add(f_id)
        refs = set(_REFERENCE.findall(mitigation))
        if not refs & defined:
            problems.append(
                Problem(file, lineno, f"{f_id} mitigation references no constraint, decision or task")
            )
    return problems


def _table_cells(line: str) -> list[str] | None:
    """Cells of a Markdown table row, honouring escaped pipes (``\\|``)."""
    line = line.strip()
    if not line.startswith("|"):
        return None
    cells = _CELL_SEPARATOR.split(line[1:])
    if cells and not cells[-1].strip():
        cells.pop()  # trailing pipe
    return [c.strip() for c in cells]


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last."""


def _construct_unique_mapping(loader, node, deep=False):
    # Only explicit keys count: a key that overrides one pulled in through a
    # YAML merge (<<: *defaults) is allowed. construct_mapping does the merge.
    seen = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:  # unhashable; construct_mapping reports it
            continue
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _validate_ceo_yaml(path: Path, task_ids: set[str]) -> list[Problem]:
    file = path.name
    if not path.is_file():
        return [Problem(file, None, "missing")]
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        return [Problem(file, None, f"invalid YAML: {exc}")]
    entries = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return [Problem(file, None, "expected a top-level 'tasks' mapping")]

    problems: list[Problem] = []
    for key in entries:
        if not isinstance(key, str):
            problems.append(Problem(file, None, f"task ID {key!r} must be a quoted string, e.g. \"1.1\""))
    keys = {k for k in entries if isinstance(k, str)}
    for missing in sorted(task_ids - keys):
        problems.append(Problem(file, None, f"no entry for task {missing}"))
    for extra in sorted(keys - task_ids):
        problems.append(Problem(file, None, f"entry {extra} matches no task in tasks.md"))

    graph: dict[str, list[str]] = {}
    for task_id in sorted(keys & task_ids):
        entry = entries[task_id]
        if not isinstance(entry, dict):
            problems.append(Problem(file, None, f"{task_id}: expected a mapping"))
            continue
        for unknown in sorted(set(entry) - _ENTRY_KEYS):
            problems.append(Problem(file, None, f"{task_id}: unknown key '{unknown}'"))
        check = entry.get("check")
        if not isinstance(check, str) or not check.strip():
            problems.append(Problem(file, None, f"{task_id}: needs a non-empty 'check'"))
        if entry.get("blast_radius") not in BLAST_RADII:
            problems.append(
                Problem(file, None, f"{task_id}: blast_radius must be one of {', '.join(BLAST_RADII)}")
            )
        deps = entry.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            problems.append(Problem(file, None, f"{task_id}: depends_on must be a list of quoted task IDs"))
            continue
        for dep in deps:
            if dep == task_id:
                problems.append(Problem(file, None, f"{task_id}: depends on itself"))
            elif dep not in task_ids:
                problems.append(Problem(file, None, f"{task_id}: depends on unknown task {dep}"))
        graph[task_id] = [d for d in deps if d in task_ids and d != task_id]

    cycle = _find_cycle(graph)
    if cycle:
        problems.append(Problem(file, None, f"dependency cycle: {' -> '.join(cycle)}"))
    return problems


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in visiting:
            return visiting[visiting.index(node):] + [node]
        if node in done:
            return None
        visiting.append(node)
        for dep in graph.get(node, []):
            cycle = visit(dep)
            if cycle:
                return cycle
        visiting.pop()
        done.add(node)
        return None

    for node in sorted(graph):
        cycle = visit(node)
        if cycle:
            return cycle
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    failed = False
    for arg in argv:
        problems = validate_change(Path(arg))
        for problem in problems:
            print(f"{arg}/{problem}")
        failed = failed or bool(problems)
        if not problems:
            print(f"{arg}: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
