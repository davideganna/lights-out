"""Evaluation task format: `eval/tasks/<id>/task.toml` plus hidden acceptance tests.

A task directory may also hold `setup.patch` (applied to the sample repository before the agents start) and
`solution.patch` (a reference solution on top of it). The solution is only used by this repository's own tests to
prove the task is solvable; the runner never copies it anywhere the agents can see it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import Comment, Ticket

CATEGORIES = ("feature", "bug", "refactor", "docs", "ambiguous", "too_large", "not_actionable")
EXPECTED = ("proceed", "needs_info")
DEFAULT_ACCEPTANCE_COMMAND = "python -m pytest -q tests/acceptance"
_KEYS = {"id", "title", "description", "comments", "category", "expected", "acceptance_command"}


class TaskError(ValueError):
    pass


@dataclass
class Task:
    id: str
    title: str
    description: str
    category: str
    expected: str
    dir: Path
    comments: list[dict[str, str]] = field(default_factory=list)
    acceptance_command: str = DEFAULT_ACCEPTANCE_COMMAND

    @property
    def acceptance_dir(self) -> Path:
        return self.dir / "acceptance"

    @property
    def setup_patch(self) -> Path | None:
        p = self.dir / "setup.patch"
        return p if p.is_file() else None

    @property
    def solution_patch(self) -> Path | None:
        """Reference solution, for tests only; never shown to agents."""
        p = self.dir / "solution.patch"
        return p if p.is_file() else None

    def ticket(self) -> Ticket:
        return Ticket(
            id=self.id,
            short_id=self.id,
            title=self.title,
            description=self.description,
            url=f"eval://{self.id}",
            labels=[],
            comments=[Comment(c["author"], c["text"], date="", username=c["author"]) for c in self.comments],
        )


def _string(data: dict, key: str, task_id: str, *, required: bool = True, default: str = "") -> str:
    if key not in data:
        if required:
            raise TaskError(f"task {task_id!r}: missing field {key!r}")
        return default
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"task {task_id!r}: field {key!r} must be a non-empty string")
    return value


def load_task(directory: Path) -> Task:
    directory = Path(directory)
    task_id = directory.name
    file = directory / "task.toml"
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TaskError(f"task {task_id!r}: {file} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise TaskError(f"task {task_id!r}: invalid TOML: {e}") from None
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise TaskError(f"task {task_id!r}: unknown field(s) {', '.join(unknown)}")
    declared = _string(data, "id", task_id)
    if declared != task_id:
        raise TaskError(f"task {task_id!r}: field 'id' is {declared!r} but the directory is named {task_id!r}")
    category = _string(data, "category", task_id)
    if category not in CATEGORIES:
        raise TaskError(f"task {task_id!r}: field 'category' must be one of {', '.join(CATEGORIES)}, got {category!r}")
    expected = _string(data, "expected", task_id)
    if expected not in EXPECTED:
        raise TaskError(f"task {task_id!r}: field 'expected' must be one of {', '.join(EXPECTED)}, got {expected!r}")
    comments = data.get("comments", [])
    if not isinstance(comments, list):
        raise TaskError(f"task {task_id!r}: field 'comments' must be a list")
    for c in comments:
        if (
            not isinstance(c, dict)
            or not isinstance(c.get("author"), str)
            or not isinstance(c.get("text"), str)
            or set(c) - {"author", "text"}
        ):
            raise TaskError(f"task {task_id!r}: each entry of 'comments' must be a table with string author and text")
    task = Task(
        id=task_id,
        title=_string(data, "title", task_id),
        description=_string(data, "description", task_id),
        category=category,
        expected=expected,
        dir=directory,
        comments=[{"author": c["author"], "text": c["text"]} for c in comments],
        acceptance_command=_string(
            data, "acceptance_command", task_id, required=False, default=DEFAULT_ACCEPTANCE_COMMAND
        ),
    )
    if expected == "proceed" and not (task.acceptance_dir.is_dir() and any(task.acceptance_dir.iterdir())):
        raise TaskError(f"task {task_id!r}: 'proceed' tasks need a non-empty acceptance/ directory")
    return task


def load_tasks(tasks_dir: Path, ids: list[str] | None = None) -> list[Task]:
    """Load every task (sorted by id), or just `ids` in the given order. Unknown ids are a TaskError."""
    tasks_dir = Path(tasks_dir)
    if ids is None:
        dirs = sorted(p for p in tasks_dir.iterdir() if (p / "task.toml").is_file()) if tasks_dir.is_dir() else []
        return [load_task(d) for d in dirs]
    out = []
    for task_id in ids:
        d = tasks_dir / task_id
        if not (d / "task.toml").is_file():
            raise TaskError(f"unknown task {task_id!r} (no {d / 'task.toml'})")
        out.append(load_task(d))
    return out
