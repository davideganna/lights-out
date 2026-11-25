"""SQLite record of every ticket run and its stage timeline (feeds the dashboard and crash recovery)."""

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
    error TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT ''
);

-- One row per stage change: when the stage started and the run's cumulative cost at that moment.
-- The final row of a finished run has stage 'end'. Durations and per-stage cost are differences
-- between consecutive rows.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    stage TEXT NOT NULL,
    at TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);

-- Approval commands (PR comments or reviews) already acted on, so each is handled once.
CREATE TABLE IF NOT EXISTS handled_commands (
    repo TEXT,
    kind TEXT,      -- comment | review
    item_id TEXT,
    run_id INTEGER,
    at TEXT,
    PRIMARY KEY (repo, kind, item_id)
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
    repo: str = ""
    settings: str = ""


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
        if "repo" not in columns:  # databases created before repository routing
            self._db.execute("ALTER TABLE runs ADD COLUMN repo TEXT NOT NULL DEFAULT ''")
            self._db.commit()
        if "settings" not in columns:  # databases created before per-run settings
            self._db.execute("ALTER TABLE runs ADD COLUMN settings TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    def start(
        self, *, day: str, card_id: str, card_short_id: str, title: str, card_url: str, settings: str = ""
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO runs (day, card_id, card_short_id, title, card_url, status, started_at, settings)"
            " VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (day, card_id, card_short_id, title, card_url, _now(), settings),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def set_repo(self, run_id: int, repo: str) -> None:
        self._db.execute("UPDATE runs SET repo=? WHERE id=?", (repo, run_id))
        self._db.commit()

    def stage(self, run_id: int, stage: str, cost_usd: float | None = None) -> None:
        if cost_usd is None:
            self._db.execute("UPDATE runs SET stage=? WHERE id=?", (stage, run_id))
            cost_usd = float(self._db.execute("SELECT cost_usd FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        else:
            self._db.execute("UPDATE runs SET stage=?, cost_usd=? WHERE id=?", (stage, cost_usd, run_id))
        self._event(run_id, stage, cost_usd)
        self._db.commit()

    def _event(self, run_id: int, stage: str, cost_usd: float) -> None:
        self._db.execute(
            "INSERT INTO events (run_id, stage, at, cost_usd) VALUES (?, ?, ?, ?)", (run_id, stage, _now(), cost_usd)
        )

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
        self._event(run_id, "end", cost_usd)
        self._db.commit()

    def resolve_pr(self, run_id: int, *, status: str, event: str, error: str | None = None) -> None:
        """Settle a run left as pr_open. Cost, summary, pr_url and finished_at stay the agent's own."""
        cost = float(self._db.execute("SELECT cost_usd FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        if error is None:
            self._db.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        else:
            self._db.execute("UPDATE runs SET status=?, error=? WHERE id=?", (status, error, run_id))
        self._event(run_id, event, cost)
        self._db.commit()

    def open_prs(self) -> list[Run]:
        """The latest run of each card, kept only if it is pr_open with a PR url."""
        return self._rows(
            "SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY card_id)"
            " AND status='pr_open' AND COALESCE(pr_url, '') != '' ORDER BY id"
        )

    def is_handled(self, repo: str, kind: str, item_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM handled_commands WHERE repo=? AND kind=? AND item_id=?", (repo, kind, str(item_id))
        ).fetchone()
        return row is not None

    def mark_handled(self, repo: str, kind: str, item_id: str, run_id: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO handled_commands (repo, kind, item_id, run_id, at) VALUES (?, ?, ?, ?, ?)",
            (repo, kind, str(item_id), run_id, _now()),
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

    def all_runs(self) -> list[Run]:
        return self._rows("SELECT * FROM runs ORDER BY id")

    def all_events(self) -> list[dict]:
        rows = self._db.execute("SELECT run_id, stage, at, cost_usd FROM events ORDER BY run_id, id")
        return [dict(r) for r in rows]

    def _rows(self, sql: str, params: tuple = ()) -> list[Run]:
        return [Run(**dict(r)) for r in self._db.execute(sql, params)]
