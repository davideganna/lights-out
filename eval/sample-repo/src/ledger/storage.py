"""Keep expenses in a JSON file."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Expense


def load(path: str | Path) -> list[Expense]:
    """Read all expenses; a missing file means no expenses yet."""
    p = Path(path)
    if not p.exists():
        return []
    return [Expense.from_dict(item) for item in json.loads(p.read_text(encoding="utf-8"))]


def save(path: str | Path, expenses: list[Expense]) -> None:
    data = [e.to_dict() for e in expenses]
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
