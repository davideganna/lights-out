"""SQLite record of every ticket run (feeds the morning report and crash recovery)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    card_id TEXT NOT NULL,
    card_short_id TEXT NOT NULL,
    title TEXT NOT NULL,
    card_url TEXT NOT NULL,
    status TEXT NOT NULL,           -- running | merged | needs_info | rejected | failed | pr_open | interrupted
    stage TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    pr_url TEXT,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Run:
    id: int
    day: str
    card_id: str
    card_short_id: str
    title: str
    card_url: str
    status: str
    stage: str
    started_at: str
    finished_at: str | None
    cost_usd: float
    pr_url: str | None
    summary: str
    error: str


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def start(self, *, day: str, card_id: str, card_short_id: str, title: str, card_url: str) -> int:
        cur = self._db.execute(
            "INSERT INTO runs (day, card_id, card_short_id, title, card_url, status, started_at)"
            " VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (day, card_id, card_short_id, title, card_url, _now()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def stage(self, run_id: int, stage: str, cost_usd: float | None = None) -> None:
        if cost_usd is None:
            self._db.execute("UPDATE runs SET stage=? WHERE id=?", (stage, run_id))
        else:
            self._db.execute("UPDATE runs SET stage=?, cost_usd=? WHERE id=?", (stage, cost_usd, run_id))
        self._db.commit()

    def finish(
        self,
        run_id: int,
        *,
        status: str,
        cost_usd: float,
        summary: str = "",
        error: str = "",
        pr_url: str | None = None,
    ) -> None:
        self._db.execute(
            "UPDATE runs SET status=?, finished_at=?, cost_usd=?, summary=?, error=?, pr_url=? WHERE id=?",
            (status, _now(), cost_usd, summary, error, pr_url, run_id),
        )
        self._db.commit()

    def running(self) -> list[Run]:
        return self._rows("SELECT * FROM runs WHERE status='running'")

    def day_runs(self, day: str) -> list[Run]:
        return self._rows("SELECT * FROM runs WHERE day=? ORDER BY id", (day,))

    def days(self) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT DISTINCT day FROM runs ORDER BY day DESC")]

    def day_cost(self, day: str) -> float:
        return float(self._db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE day=?", (day,)).fetchone()[0])

    def attempts(self, card_id: str, day: str) -> int:
        return int(
            self._db.execute("SELECT COUNT(*) FROM runs WHERE card_id=? AND day=?", (card_id, day)).fetchone()[0]
        )

    def _rows(self, sql: str, params: tuple = ()) -> list[Run]:
        return [Run(**dict(r)) for r in self._db.execute(sql, params)]
