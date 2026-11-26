"""Queue watcher: picks queued cards, respects the daily budget and retry limit."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass

import pytest

from lights_out.core.config import Config
from lights_out.core.models import Ticket
from lights_out.orchestration.health import Heartbeat
from lights_out.orchestration.pipeline import Outcome
from lights_out.orchestration.scheduler import MAX_ATTEMPTS_PER_DAY, Scheduler, run_until_signal, today
from lights_out.reporting.store import Store


class FakeTrello:
    def __init__(self, cards):
        self.queued = list(cards)
        self.comments: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []

    def cards_in(self, list_name):
        assert list_name == "Queued"
        return list(self.queued)

    def comment(self, card_id, text):
        self.comments.append((card_id, text))

    def move(self, card_id, list_name, top=True):
        self.moves.append((card_id, list_name))


@dataclass
class FakePipeline:
    store: Store
    trello: FakeTrello
    cost: float = 1.0
    leave_in_queue: bool = False

    async def process(self, card, day, budget_usd):
        rid = self.store.start(
            day=day, card_id=card.id, card_short_id=card.short_id, title=card.title, card_url=card.url
        )
        self.store.finish(rid, status="merged", cost_usd=self.cost)
        if not self.leave_in_queue:
            self.trello.queued = [c for c in self.trello.queued if c.id != card.id]
        return Outcome("merged")


def card(n: int) -> Ticket:
    return Ticket(id=f"c{n}", short_id=str(n), title=f"card {n}", description="", url=f"u{n}")


class FakeAlerter:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, event, text):
        self.sent.append((event, text))
        return True

    def events(self) -> list[str]:
        return [e for e, _ in self.sent]


@pytest.fixture
def setup(tmp_path):
    def make(cards, alerter=None, sched_kw=None, **pipe_kw):
        sched_kw = sched_kw or {}
        cfg = Config(
            trello_api_key="k",
            trello_token="t",
            trello_board_id="b",
            github_token="",
            repo_labels={"app": "o/app"},
            data_dir=tmp_path,
            max_day_usd=10,
        )
        cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        store = Store(tmp_path / "s.db")
        trello = FakeTrello(cards)
        pipe = FakePipeline(store, trello, **pipe_kw)
        return Scheduler(cfg, trello, store, pipe, alerter=alerter, **sched_kw), trello, store

    return make


async def test_processes_queue_top_first_then_idles(setup):
    sched, trello, store = setup([card(1), card(2)])
    assert await sched.work_once()
    assert await sched.work_once()
    assert not await sched.work_once()
    assert [r.card_short_id for r in store.day_runs(today())] == ["1", "2"]
    assert (sched.cfg.reports_dir / "data.json").exists()


async def test_work_once_beats_on_empty_queue_and_when_budget_reached(setup):
    now = [1000.0]
    hb = Heartbeat(clock=lambda: now[0])
    sched, trello, store = setup([], sched_kw={"heartbeat": hb})
    now[0] = 1500.0
    assert not await sched.work_once()
    assert hb.snapshot(900)[1]["age_s"] == 0
    sched, trello, store = setup([card(1)], sched_kw={"heartbeat": hb}, cost=9.8)
    assert await sched.work_once()
    now[0] = 1800.0
    assert not await sched.work_once()  # budget used up
    assert hb.snapshot(900)[1]["age_s"] == 0


async def test_daily_budget_stops_work(setup):
    sched, trello, store = setup([card(1), card(2)], cost=9.8)
    assert await sched.work_once()
    assert not await sched.work_once()  # $0.20 left: below the minimum
    assert len(trello.queued) == 1


async def test_card_requeued_too_often_is_skipped_once_with_comment(setup):
    sched, trello, store = setup([card(1)], cost=0.1, leave_in_queue=True)
    for _ in range(MAX_ATTEMPTS_PER_DAY):
        assert await sched.work_once()
    assert not await sched.work_once()
    assert not await sched.work_once()
    assert len(trello.comments) == 1 and "won't pick it up again" in trello.comments[0][1]


async def test_recover_requeues_interrupted_runs(setup):
    sched, trello, store = setup([])
    store.start(day=today(), card_id="c9", card_short_id="9", title="t", card_url="u")
    sched.recover()
    assert trello.moves == [("c9", "Queued")]
    assert store.day_runs(today())[0].status == "interrupted"


async def test_run_until_signal_stops_on_sigterm_and_restores_handler():
    before = signal.getsignal(signal.SIGTERM)
    asyncio.get_running_loop().call_later(0.01, signal.raise_signal, signal.SIGTERM)
    assert await run_until_signal(asyncio.sleep(3600)) is True
    assert signal.getsignal(signal.SIGTERM) == before


async def test_run_until_signal_returns_false_when_coroutine_finishes():
    async def quick():
        return 1

    assert await run_until_signal(quick()) is False


async def test_sigterm_stops_scheduler_without_picking_up_another_card(setup):
    sched, trello, store = setup([card(1), card(2)])
    started: list[str] = []
    entered = asyncio.Event()

    async def blocking_process(c, day, budget_usd):
        started.append(c.id)
        entered.set()
        await asyncio.sleep(3600)

    sched.pipeline.process = blocking_process
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(run_until_signal(sched.run_forever(), stop=sched.stop))
    await asyncio.wait_for(entered.wait(), timeout=30)
    loop.call_soon(signal.raise_signal, signal.SIGTERM)
    assert await asyncio.wait_for(task, timeout=30) is True
    assert started == ["c1"]


async def test_signal_during_blocking_stage_finishes_card_and_picks_up_no_more(setup):
    sched, trello, store = setup([card(1), card(2)])
    started: list[str] = []

    async def blocking_stage(c, day, budget_usd):
        # No await: like _deliver, the whole card runs without yielding to the event loop.
        started.append(c.id)
        signal.raise_signal(signal.SIGTERM)
        rid = store.start(day=day, card_id=c.id, card_short_id=c.short_id, title=c.title, card_url=c.url)
        store.finish(rid, status="merged", cost_usd=1.0)
        trello.queued = [q for q in trello.queued if q.id != c.id]
        return Outcome("merged")

    sched.pipeline.process = blocking_stage
    assert await asyncio.wait_for(run_until_signal(sched.run_forever(), stop=sched.stop), timeout=30) is True
    assert started == ["c1"]
    assert [r.status for r in store.day_runs(today())] == ["merged"]
    assert trello.comments == [] and trello.moves == []


async def test_run_now_stops_after_signal_during_blocking_stage(setup):
    sched, trello, store = setup([card(1), card(2)], leave_in_queue=True)
    real = sched.pipeline.process

    async def process_then_signal(c, day, budget_usd):
        out = await real(c, day, budget_usd)
        signal.raise_signal(signal.SIGTERM)
        return out

    sched.pipeline.process = process_then_signal
    assert await asyncio.wait_for(run_until_signal(sched.run_now(), stop=sched.stop), timeout=30) is True
    assert [r.card_short_id for r in store.day_runs(today())] == ["1"]


async def test_budget_alert_once_per_day(setup, monkeypatch):
    alerter = FakeAlerter()
    sched, trello, store = setup([card(1), card(2)], alerter=alerter, cost=9.8)
    assert await sched.work_once()
    assert alerter.sent == []  # merged card: nothing
    assert not await sched.work_once()
    assert not await sched.work_once()
    assert alerter.events() == ["budget"]
    assert alerter.sent[0][1].startswith("lights-out:") and "MAX_DAY_USD" in alerter.sent[0][1]
    monkeypatch.setattr("lights_out.orchestration.scheduler.today", lambda: "2999-01-01")
    sched.cfg.max_day_usd = 0
    assert not await sched.work_once()
    assert alerter.events() == ["budget", "budget"]


async def test_run_forever_alerts_started_and_stopping(setup):
    alerter = FakeAlerter()
    sched, trello, store = setup([], alerter=alerter)
    sched.cfg.poll_seconds = 1
    task = asyncio.ensure_future(run_until_signal(sched.run_forever(), stop=sched.stop))
    await asyncio.sleep(0.05)
    assert alerter.events() == ["lifecycle"] and "started" in alerter.sent[0][1]
    signal.raise_signal(signal.SIGTERM)
    assert await asyncio.wait_for(task, timeout=30) is True
    assert alerter.events() == ["lifecycle", "lifecycle"] and "stopping" in alerter.sent[1][1]


async def test_run_now_sends_no_lifecycle_alerts(setup):
    alerter = FakeAlerter()
    sched, *_ = setup([card(1)], alerter=alerter)
    await sched.run_now()
    assert alerter.sent == []


async def test_loop_exception_sends_error_alert(setup):
    alerter = FakeAlerter()
    sched, *_ = setup([card(1)], alerter=alerter)

    async def boom(c, day, budget_usd):
        sched.stop.set()
        raise RuntimeError("trello down\nsecond line")

    sched.pipeline.process = boom
    await asyncio.wait_for(sched.run_forever(), timeout=30)
    assert alerter.events() == ["lifecycle", "error", "lifecycle"]
    text = alerter.sent[1][1]
    assert text.startswith("lights-out:") and "RuntimeError: trello down" in text and "second line" not in text
