"""Queue watcher: picks queued cards, respects the daily budget and retry limit."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lights_out.core.config import Config
from lights_out.core.models import Ticket
from lights_out.orchestration.pipeline import Outcome
from lights_out.orchestration.scheduler import MAX_ATTEMPTS_PER_DAY, Scheduler, today
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


@pytest.fixture
def setup(tmp_path):
    def make(cards, **pipe_kw):
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
        return Scheduler(cfg, trello, store, pipe), trello, store

    return make


async def test_processes_queue_top_first_then_idles(setup):
    sched, trello, store = setup([card(1), card(2)])
    assert await sched.work_once()
    assert await sched.work_once()
    assert not await sched.work_once()
    assert [r.card_short_id for r in store.day_runs(today())] == ["1", "2"]
    assert (sched.cfg.reports_dir / f"{today()}.html").exists()


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
