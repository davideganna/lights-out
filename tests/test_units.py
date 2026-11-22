"""Unit tests for models, scheduling helpers, the Trello/GitHub adapters and the report."""

from __future__ import annotations

import json

import httpx
import pytest

from lights_out.core.models import Comment, Ticket
from lights_out.integrations.github import GitHubClient
from lights_out.integrations.trello import TrelloClient
from lights_out.orchestration.workspace import Workspace, WorkspaceError, run_as
from lights_out.reporting.report import render_day
from lights_out.reporting.store import Store


def test_ticket_branch_and_prompt():
    t = Ticket(
        id="x",
        short_id="5",
        title="Add Dark-mode toggle!!",
        description="desc",
        url="u",
        checklists=["AC:", "  - [ ] works"],
        comments=[Comment("Ann", "Use blue", "2026-11-20 10:00")],
    )
    assert t.branch == "agent/5-add-dark-mode-toggle"
    p = t.as_prompt()
    assert "Use blue" in p and "[ ] works" in p and "desc" in p


def test_run_as():
    assert run_as("", ["ls"]) == ["ls"]
    assert run_as("agent", ["ls"])[:2] == ["setpriv", "--reuid=agent"]


def test_push_refuses_base_branch(tmp_path):
    ws = Workspace(tmp_path, repo_url="x", base_branch="main", github_token="")
    with pytest.raises(WorkspaceError):
        ws.push("main")


def _trello(handler) -> TrelloClient:
    http = httpx.Client(base_url="https://api.trello.com/1", transport=httpx.MockTransport(handler))
    return TrelloClient("k", "t", "board", http=http)


def test_trello_cards_sorted_and_comments_oldest_first():
    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/boards/board/lists"):
            return httpx.Response(200, json=[{"id": "L1", "name": "Ready"}])
        if path.endswith("/lists/L1/cards"):
            return httpx.Response(
                200,
                json=[
                    {"id": "b", "name": "second", "desc": "", "idShort": 2, "shortUrl": "u2", "labels": [], "pos": 20},
                    {"id": "a", "name": "first", "desc": "", "idShort": 1, "shortUrl": "u1", "labels": [], "pos": 10},
                ],
            )
        if path.endswith("/cards/a"):
            return httpx.Response(
                200,
                json={
                    "id": "a",
                    "name": "first",
                    "desc": "d",
                    "idShort": 1,
                    "shortUrl": "u1",
                    "labels": [{"name": "feature"}],
                },
            )
        if path.endswith("/cards/a/checklists"):
            return httpx.Response(200, json=[])
        if path.endswith("/cards/a/actions"):
            return httpx.Response(
                200,
                json=[
                    {"date": "2026-11-21T10:00:00Z", "data": {"text": "newer"}, "memberCreator": {"fullName": "B"}},
                    {"date": "2026-11-20T10:00:00Z", "data": {"text": "older"}, "memberCreator": {"fullName": "A"}},
                ],
            )
        if path.endswith("/cards/a/actions/comments"):
            assert json.loads(req.content)["text"] == "hello"
            return httpx.Response(200, json={})
        return httpx.Response(404)

    tr = _trello(handler)
    assert [c.title for c in tr.cards_in("Ready")] == ["first", "second"]
    t = tr.get_ticket("a")
    assert [c.text for c in t.comments] == ["older", "newer"] and t.labels == ["feature"]
    tr.comment("a", "hello")


def test_trello_missing_list_is_a_clear_error():
    tr = _trello(lambda req: httpx.Response(200, json=[]))
    with pytest.raises(Exception, match="not found"):
        tr.list_id("Ready")


def _gh(responses: list[dict]) -> GitHubClient:
    it = iter(responses)

    def handler(req):
        return httpx.Response(200, json=next(it))

    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    now = [0.0]
    return GitHubClient("t", "o/r", http=http, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])


def run(status, conclusion=None, name="check"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_ci_waits_for_pending_then_succeeds():
    gh = _gh([{"check_runs": []}, {"check_runs": [run("in_progress")]}, {"check_runs": [run("completed", "success")]}])
    assert gh.wait_for_ci("sha", timeout_s=600, grace_s=120) == ("success", [])


def test_ci_failure_lists_failed_checks():
    gh = _gh([{"check_runs": [run("completed", "failure", "lint"), run("completed", "success", "tests")]}])
    assert gh.wait_for_ci("sha", timeout_s=600, grace_s=120) == ("failure", ["lint"])


def test_ci_none_after_grace_period():
    gh = _gh([{"check_runs": []}] * 20)
    assert gh.wait_for_ci("sha", timeout_s=600, grace_s=60, poll_s=20)[0] == "none"


def test_report_renders(tmp_path):
    store = Store(tmp_path / "s.db")
    rid = store.start(day="n", card_id="c", card_short_id="3", title="<b>x</b>", card_url="u")
    store.finish(rid, status="merged", cost_usd=1.5, summary="did it", pr_url="https://pr")
    page = render_day(store, "n")
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "Merged" in page and "$1.50" in page
