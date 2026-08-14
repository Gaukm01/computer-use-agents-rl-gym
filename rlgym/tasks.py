"""Loading task definitions.

tasks.json stores each expected answer as a JSON *string* rather than a nested object, so
it is double-encoded and needs json.loads() before comparison. Task.expected_output()
handles that; nothing else should parse it.
"""

from __future__ import annotations

import json
import pathlib

from . import vendor
from .schemas import Task


def load_tasks(path: pathlib.Path | str | None = None) -> list[Task]:
    """Load every task, preserving file order."""
    path = pathlib.Path(path) if path else vendor.TASKS_FILE
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array of tasks, got {type(raw)}")
    return [Task.model_validate(entry) for entry in raw]


def load_task_map(path: pathlib.Path | str | None = None) -> dict[str, Task]:
    tasks = load_tasks(path)
    seen: dict[str, Task] = {}
    for task in tasks:
        if task.id in seen:
            raise ValueError(
                f"duplicate task id {task.id!r} in {path or vendor.TASKS_FILE}"
            )
        seen[task.id] = task
    return seen


def select_tasks(
    task_ids: list[str] | None = None, path: pathlib.Path | str | None = None
) -> list[Task]:
    """Load all tasks, or just the named ones (order follows task_ids)."""
    task_map = load_task_map(path)
    if not task_ids:
        return list(task_map.values())
    missing = [t for t in task_ids if t not in task_map]
    if missing:
        raise KeyError(
            f"unknown task id(s): {', '.join(missing)}. "
            f"Available: {', '.join(task_map)}"
        )
    return [task_map[t] for t in task_ids]
