"""Queue watcher: as soon as a card lands in the Queued column, process it.

Trello webhooks need a public HTTPS endpoint, so instead the watcher polls the
Queued column every POLL_SECONDS (default 15 s): one cheap API call per poll,
nothing exposed to the internet. Cards are processed one at a time, top first.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import logging
import re
import signal
import threading
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..integrations.trello import TrelloClient
from ..reporting.report import TRANSCRIPT_RE, write_reports
from ..reporting.store import Store
from .pipeline import Pipeline

log = logging.getLogger(__name__)

# A card that fails is moved out of Queued; if a human queues it again the same
# day it is retried, but not more than this many times (protects the budget).
MAX_ATTEMPTS_PER_DAY = 3


def today() -> str:
    return datetime.now().date().isoformat()


class Scheduler:
    def __init__(self, cfg: Config, trello: TrelloClient, store: Store, pipeline: Pipeline):
        self.cfg = cfg
        self.trello = trello
        self.store = store
        self.pipeline = pipeline
        self.stop = threading.Event()  # set by run_until_signal: finish the current card, take no more
        self._skipped: set[str] = set()  # cards already reported as over the attempt limit

    def recover(self) -> None:
        """Runs still marked 'running' were cut off by a crash or restart: put their cards back."""
        for run in self.store.running():
            log.warning("Recovering interrupted run #%s (card #%s)", run.id, run.card_short_id)
            self.store.finish(
                run.id,
                status="interrupted",
                cost_usd=run.cost_usd,
                error=f"Interrupted during stage '{run.stage}' (process restarted).",
            )
            try:
                self.trello.comment(
                    run.card_id, "🤖 **lights-out** was interrupted while working on this; it will retry."
                )
                self.trello.move(run.card_id, self.cfg.lists.ready)
            except Exception:  # noqa: BLE001
                log.exception("Could not move card %s back", run.card_id)
            write_reports(self.store, self.cfg.reports_dir, run.day, runs_dir=self.cfg.runs_dir)

    def next_card(self, day: str):
        for card in self.trello.cards_in(self.cfg.lists.ready):
            if self.store.attempts(card.id, day) < MAX_ATTEMPTS_PER_DAY:
                return card
            if card.id not in self._skipped:
                self._skipped.add(card.id)
                log.warning("Card #%s already attempted %d times today; skipping", card.short_id, MAX_ATTEMPTS_PER_DAY)
                self.trello.comment(
                    card.id,
                    f"🤖 **lights-out** already tried this card {MAX_ATTEMPTS_PER_DAY} "
                    "times today, so it won't pick it up again until tomorrow.",
                )
        return None

    def budget_left(self, day: str) -> float:
        return self.cfg.max_day_usd - self.store.day_cost(day)

    async def work_once(self) -> bool:
        """Process the next queued card. Returns False when there is nothing (affordable) to do."""
        day = today()
        left = self.budget_left(day)
        if left < 0.5:
            log.info("Daily budget of $%.2f reached; queued cards wait until tomorrow", self.cfg.max_day_usd)
            return False
        card = self.next_card(day)
        if card is None:
            return False
        log.info("Picked up card #%s: %s", card.short_id, card.title)
        outcome = await self.pipeline.process(card, day, budget_usd=min(self.cfg.max_ticket_usd, left))
        log.info("Card #%s → %s", card.short_id, outcome.status)
        write_reports(self.store, self.cfg.reports_dir, day, runs_dir=self.cfg.runs_dir)
        return True

    async def run_now(self) -> None:
        """Process everything currently queued, then exit."""
        self.recover()
        while not self.stop.is_set() and await self.work_once():
            pass
        write_reports(self.store, self.cfg.reports_dir, today(), runs_dir=self.cfg.runs_dir)

    async def run_forever(self) -> None:
        self.recover()
        write_reports(self.store, self.cfg.reports_dir, today(), runs_dir=self.cfg.runs_dir)
        log.info("Watching '%s' every %ds", self.cfg.lists.ready, self.cfg.poll_seconds)
        while not self.stop.is_set():
            try:
                worked = await self.work_once()
            except Exception:  # noqa: BLE001 - keep the watcher alive (e.g. a Trello outage)
                log.exception("Watcher error")
                worked = False
            if not worked and not self.stop.is_set():  # after finishing a card, check the queue again straight away
                await asyncio.sleep(self.cfg.poll_seconds)


async def run_until_signal(
    coro: Coroutine[Any, Any, Any],
    signals=(signal.SIGTERM, signal.SIGINT),
    stop: threading.Event | None = None,
) -> bool:
    """Run `coro`; on SIGTERM/SIGINT set `stop`, cancel it and return True.

    Returns False if the coroutine finished without a stop request. The plain Python
    signal handler sets `stop` immediately, even while the loop is blocked in a synchronous
    stage (checks, delivery), so the coroutine can see the request as soon as that stage
    returns; the cancellation itself lands at the next await.
    """
    stop = stop if stop is not None else threading.Event()
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)

    def on_signal(signum, frame) -> None:
        stop.set()
        loop.call_soon_threadsafe(task.cancel)

    previous = {sig: signal.signal(sig, on_signal) for sig in signals}
    try:
        await task
    except asyncio.CancelledError:
        log.info("Shutdown requested; stopped cleanly")
        return True
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if stop.is_set():
        log.info("Shutdown requested; stopped cleanly")
    return stop.is_set()


_TRANSCRIPT_PATH = re.compile(r"^/runs/([0-9]+)/([^/]+)$")


def resolve_transcript(runs_dir: Path, raw_path: str) -> Path | None:
    """Map a raw (still percent-encoded) request path to a transcript file, or None (-> 404)."""
    path = raw_path.split("#", 1)[0].split("?", 1)[0]
    m = _TRANSCRIPT_PATH.fullmatch(path)
    if not m or not TRANSCRIPT_RE.fullmatch(m.group(2)):
        return None
    root = runs_dir.resolve()
    target = (runs_dir / m.group(1) / m.group(2)).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


class _DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the dashboard from `directory` and agent transcripts from `runs_dir` under /runs/."""

    def __init__(self, *args, runs_dir: Path, **kwargs) -> None:
        self.runs_dir = runs_dir
        super().__init__(*args, **kwargs)

    def log_message(self, *args) -> None:
        pass

    def send_head(self):
        path = self.path
        if not (path == "/runs" or path.startswith("/runs/")):
            return super().send_head()
        target = resolve_transcript(self.runs_dir, path)
        if target is None:
            self.send_error(404)
            return None
        try:
            f = target.open("rb")
        except OSError:
            self.send_error(404)
            return None
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        return f


def build_server(cfg: Config, host: str = "0.0.0.0") -> http.server.ThreadingHTTPServer:
    # 0.0.0.0 is for the container; the compose port mapping restricts it to localhost on the host.
    handler = functools.partial(_DashboardHandler, directory=str(cfg.reports_dir), runs_dir=cfg.runs_dir)
    return http.server.ThreadingHTTPServer((host, cfg.report_port), handler)


def serve_reports(cfg: Config) -> threading.Thread:
    server = build_server(cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Reports at http://localhost:%d/", cfg.report_port)
    return thread
