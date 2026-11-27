"""Queue watcher: as soon as a card lands in the Queued column, process it.

Trello webhooks need a public HTTPS endpoint, so instead the watcher polls the
Queued column every POLL_SECONDS (default 15 s): one cheap API call per poll,
nothing exposed to the internet. Cards are processed one at a time, top first.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import io
import json
import logging
import re
import signal
import sys
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx

from ..core.config import Config
from ..integrations.alerts import NullAlerter
from ..integrations.github import GitHubError
from ..integrations.trello import TrelloClient, TrelloError
from ..reporting.report import TRANSCRIPT_RE, write_reports
from ..reporting.store import Store
from .followup import FOLLOWUP_ERRORS, PRWatcher
from .health import Heartbeat
from .pipeline import MAX_ATTEMPTS_PER_DAY, Pipeline, today

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        trello: TrelloClient,
        store: Store,
        pipeline: Pipeline,
        heartbeat: Heartbeat | None = None,
        alerter=None,
        *,
        pr_watcher: PRWatcher | None = None,
    ):
        self.alerter = alerter or NullAlerter()
        self._budget_alerted_day: str | None = None
        self.cfg = cfg
        self.pr_watcher = pr_watcher
        self.trello = trello
        self.store = store
        self.pipeline = pipeline
        self.heartbeat = heartbeat if heartbeat is not None else Heartbeat()
        self.stop = threading.Event()  # set by run_until_signal: finish the current card, take no more
        self._skipped: set[str] = set()  # cards already reported as over the attempt limit

    def recover(self) -> None:
        """Runs still marked 'running' were cut off by a crash or restart: put their cards back."""
        for run in self.store.running():
            log.warning("Recovering interrupted run #%s (card #%s)", run.id, run.card_short_id)
            revision = self.store.revision_of(run.id)
            if revision is not None:
                self._recover_revision(run, *revision)
                continue
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

    def _recover_revision(self, run, repo: str, number: int) -> None:
        """A revision of an open PR was cut off: the PR stays open (the card must not be re-run from scratch)."""
        pr_url = f"https://github.com/{repo}/pull/{number}"
        error = f"Interrupted during stage '{run.stage}' (process restarted)."
        self.store.finish(run.id, status="pr_open", cost_usd=run.cost_usd, pr_url=pr_url, error=error)
        text = (
            "🤖 **lights-out** was interrupted while revising this PR (run "
            f"#{run.id}); the branch may already have been updated. A human needs to take a look."
        )
        try:
            self.trello.comment(run.card_id, text)
            self.trello.move(run.card_id, self.cfg.lists.pr_opened)
            self.pipeline.github_factory(repo).comment_pr(number, text)
        except (TrelloError, GitHubError, httpx.HTTPError):
            log.exception("Could not report the interrupted revision of %s", pr_url)
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
        self.heartbeat.beat()
        day = today()
        left = self.budget_left(day)
        if left < 0.5:
            log.info("Daily budget of $%.2f reached; queued cards wait until tomorrow", self.cfg.max_day_usd)
            if self._budget_alerted_day != day:
                self._budget_alerted_day = day
                self.alerter.send(
                    "budget",
                    f"lights-out: daily budget of ${self.cfg.max_day_usd:.2f} (MAX_DAY_USD) reached; "
                    "queued cards wait until tomorrow.",
                )
            return False
        card = self.next_card(day)
        if card is None:
            return False
        log.info("Picked up card #%s: %s", card.short_id, card.title)
        outcome = await self.pipeline.process(card, day, budget_usd=min(self.cfg.max_ticket_usd, left))
        log.info("Card #%s → %s", card.short_id, outcome.status)
        write_reports(self.store, self.cfg.reports_dir, day, runs_dir=self.cfg.runs_dir)
        return True

    async def _startup_pr_check(self) -> None:
        if self.pr_watcher is None:
            return
        try:
            await self.pr_watcher.check_now()
        except (*FOLLOWUP_ERRORS, OSError):  # a follow-up problem must not stop the service
            log.exception("PR follow-up error")

    async def run_now(self) -> None:
        """Process everything currently queued, then exit."""
        self.recover()
        await self._startup_pr_check()
        while not self.stop.is_set() and await self.work_once():
            pass
        write_reports(self.store, self.cfg.reports_dir, today(), runs_dir=self.cfg.runs_dir)

    async def run_forever(self) -> None:
        self.recover()
        await self._startup_pr_check()
        write_reports(self.store, self.cfg.reports_dir, today(), runs_dir=self.cfg.runs_dir)
        log.info("Watching '%s' every %ds", self.cfg.lists.ready, self.cfg.poll_seconds)
        self.alerter.send("lifecycle", f"lights-out: service started, watching '{self.cfg.lists.ready}'.")
        try:
            while not self.stop.is_set():
                try:
                    worked = await self.work_once()
                except Exception:  # noqa: BLE001 - keep the watcher alive (e.g. a Trello outage)
                    log.exception("Watcher error")
                    e = sys.exc_info()[1]
                    lines = str(e).strip().splitlines()
                    self.alerter.send(
                        "error",
                        f"lights-out: unexpected error in the scheduler loop: {type(e).__name__}: "
                        f"{lines[0] if lines else ''}",
                    )
                    worked = False
                if not worked and self.pr_watcher is not None and not self.stop.is_set():
                    try:
                        await self.pr_watcher.maybe_check()  # no card in progress: follow up on held PRs
                    except Exception:  # noqa: BLE001 - a follow-up problem must not stop the queue watcher
                        log.exception("PR follow-up error")
                if not worked and not self.stop.is_set():  # after a card, check the queue again straight away
                    await asyncio.sleep(self.cfg.poll_seconds)
        finally:
            if self.stop.is_set():
                self.alerter.send("lifecycle", "lights-out: service stopping (shutdown requested).")


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

    def __init__(self, *args, runs_dir: Path, heartbeat: Heartbeat, stale_s: int, **kwargs) -> None:
        self.runs_dir = runs_dir
        self.heartbeat = heartbeat
        self.stale_s = stale_s
        super().__init__(*args, **kwargs)

    def log_message(self, *args) -> None:
        pass

    def send_head(self):
        path = self.path
        if path.split("#", 1)[0].split("?", 1)[0] == "/healthz":
            status, body = self.heartbeat.snapshot(self.stale_s)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return io.BytesIO(data)
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


def build_server(
    cfg: Config, host: str = "0.0.0.0", heartbeat: Heartbeat | None = None
) -> http.server.ThreadingHTTPServer:
    # 0.0.0.0 is for the container; the compose port mapping restricts it to localhost on the host.
    handler = functools.partial(
        _DashboardHandler,
        directory=str(cfg.reports_dir),
        runs_dir=cfg.runs_dir,
        heartbeat=heartbeat or Heartbeat(),
        stale_s=cfg.health_stale_s,
    )
    return http.server.ThreadingHTTPServer((host, cfg.report_port), handler)


def serve_reports(cfg: Config, heartbeat: Heartbeat | None = None) -> threading.Thread:
    server = build_server(cfg, heartbeat=heartbeat)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Reports at http://localhost:%d/", cfg.report_port)
    return thread
