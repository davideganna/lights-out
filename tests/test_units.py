"""Unit tests for models, scheduling helpers, the Trello/GitHub adapters and the report."""

from __future__ import annotations

import json

import httpx
import pytest

from lights_out.core.models import Comment, Ticket
from lights_out.integrations.github import GitHubClient, GitHubError
from lights_out.integrations.trello import TrelloClient, queuing_member
from lights_out.orchestration.workspace import Workspace, WorkspaceError, run_as
from lights_out.reporting.report import write_reports
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
                    {
                        "date": "2026-11-21T10:00:00Z",
                        "data": {"text": "newer"},
                        "memberCreator": {"fullName": "B", "username": "bee"},
                    },
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
    assert [c.username for c in t.comments] == ["", "bee"]
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


def test_ci_calls_on_poll_once_per_poll():
    gh = _gh([{"check_runs": []}, {"check_runs": [run("in_progress")]}, {"check_runs": [run("completed", "success")]}])
    polls = []
    assert gh.wait_for_ci("sha", timeout_s=600, grace_s=120, on_poll=lambda: polls.append(1)) == ("success", [])
    assert len(polls) == 3


def test_ci_failure_lists_failed_checks():
    gh = _gh([{"check_runs": [run("completed", "failure", "lint"), run("completed", "success", "tests")]}])
    assert gh.wait_for_ci("sha", timeout_s=600, grace_s=120) == ("failure", ["lint"])


def test_ci_none_after_grace_period():
    gh = _gh([{"check_runs": []}] * 20)
    assert gh.wait_for_ci("sha", timeout_s=600, grace_s=60, poll_s=20)[0] == "none"


def _bp(classic, rules=None, rules_status=200):
    """classic: (status, json); rules: list of rules."""

    def handler(req):
        if req.url.path.endswith("/branches/main/protection"):
            return httpx.Response(classic[0], json=classic[1])
        if req.url.path.endswith("/rules/branches/main"):
            return httpx.Response(rules_status, json=rules if rules_status == 200 else {})
        return httpx.Response(500)

    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubClient("t", "o/r", http=http).branch_protection("main")


def _classic(*, checks=True, force=False, admins=True):
    return (
        200,
        {
            "required_status_checks": {"contexts": ["a", "b"], "checks": [{"context": "b"}, {"context": "c"}]}
            if checks
            else None,
            "allow_force_pushes": {"enabled": force},
            "enforce_admins": {"enabled": admins},
        },
    )


def test_bp_classic_and_rules_404_is_unprotected():
    bp = _bp((404, {}), rules_status=404)
    assert bp.readable and not bp.protected and not bp.force_pushes_allowed


def test_bp_403_is_not_readable_and_does_not_raise():
    assert _bp((403, {}), []).readable is False
    assert _bp((404, {}), rules_status=403).readable is False


def test_bp_without_required_checks():
    bp = _bp(_classic(checks=False), [])
    assert bp.protected and bp.required_checks == [] and not bp.force_pushes_allowed


def test_bp_force_pushes_allowed():
    assert _bp(_classic(force=True), []).force_pushes_allowed is True


def test_bp_fully_protected_dedupes_checks():
    bp = _bp(_classic(), [])
    assert bp.classic and not bp.rulesets and bp.enforce_admins
    assert bp.required_checks == ["a", "b", "c"]


def test_bp_admins_not_included():
    assert _bp(_classic(admins=False), []).enforce_admins is False


def test_bp_rulesets_only():
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "a"}, {"context": "b"}]},
        },
        {"type": "non_fast_forward"},
    ]
    bp = _bp((404, {}), rules)
    assert bp.protected and bp.rulesets and not bp.classic
    assert bp.required_checks == ["a", "b"] and not bp.force_pushes_allowed


def test_bp_rulesets_without_required_checks():
    bp = _bp((404, {}), [{"type": "non_fast_forward"}])
    assert bp.protected and bp.required_checks == []


def test_bp_classic_with_rules_404():
    bp = _bp(_classic(), rules_status=404)
    assert bp.protected and not bp.rulesets


def test_bp_server_error_raises():
    with pytest.raises(GitHubError):
        _bp((500, {}), [])


def test_store_records_a_stage_timeline(tmp_path):
    store = Store(tmp_path / "s.db")
    rid = store.start(day="n", card_id="c", card_short_id="3", title="t", card_url="u")
    store.stage(rid, "triage", 0.0)
    store.stage(rid, "implement", 0.25)
    store.stage(rid, "ci")  # no cost given: keeps the run's current cost
    store.finish(rid, status="merged", cost_usd=1.5)
    events = store.all_events()
    assert [e["stage"] for e in events] == ["triage", "implement", "ci", "end"]
    assert [e["cost_usd"] for e in events] == [0.0, 0.25, 0.25, 1.5]


def test_store_adds_the_events_table_to_an_old_database(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, card_id TEXT NOT NULL,"
        " card_short_id TEXT NOT NULL, title TEXT NOT NULL, card_url TEXT NOT NULL, status TEXT NOT NULL,"
        " stage TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT,"
        " cost_usd REAL NOT NULL DEFAULT 0, pr_url TEXT, summary TEXT NOT NULL DEFAULT '',"
        " error TEXT NOT NULL DEFAULT '');"
        "INSERT INTO runs (day, card_id, card_short_id, title, card_url, status, started_at)"
        " VALUES ('n', 'c', '1', 't', 'u', 'merged', '2026-11-22T10:00:00+00:00');"
    )
    con.commit()
    con.close()
    store = Store(db)
    assert store.all_runs()[0].repo == "" and store.all_events() == []


def test_dashboard_data_snapshot(tmp_path):
    import json

    store = Store(tmp_path / "s.db")
    rid = store.start(day="n", card_id="c", card_short_id="3", title="<b>x</b>", card_url="https://trello.com/c/x")
    store.set_repo(rid, "o/app")
    store.stage(rid, "triage", 0.1)
    store.finish(rid, status="merged", cost_usd=1.5, summary="did it", pr_url="https://github.com/o/app/pull/1")
    page = write_reports(store, tmp_path / "out")
    assert page.name == "index.html" and "lights-out dashboard" in page.read_text()
    data = json.loads((tmp_path / "out" / "data.json").read_text())
    run = data["runs"][0]
    assert run["title"] == "<b>x</b>"  # raw: the page inserts it as text, never as HTML
    assert run["repo"] == "o/app" and run["status"] == "merged" and run["cost_usd"] == 1.5
    assert [e["stage"] for e in data["events"]] == ["triage", "end"]
    assert not (tmp_path / "out" / "data.json.tmp").exists()


def _moved(user, date, list_id):
    return {
        "type": "updateCard",
        "date": date,
        "memberCreator": {"username": user},
        "data": {"listAfter": {"id": list_id}},
    }


def test_queuing_member_moved_into_list():
    actions = [_moved("alice", "2026-11-20T10:00:00Z", "Q"), _moved("bob", "2026-11-21T10:00:00Z", "other")]
    assert queuing_member(actions, "Q") == "alice"


def test_queuing_member_created_in_list():
    actions = [
        {
            "type": "createCard",
            "date": "2026-11-20T10:00:00Z",
            "memberCreator": {"username": "carol"},
            "data": {"list": {"id": "Q"}},
        }
    ]
    assert queuing_member(actions, "Q") == "carol"


def test_queuing_member_latest_move_wins_whatever_the_order():
    actions = [_moved("alice", "2026-11-20T10:00:00Z", "Q"), _moved("bob", "2026-11-22T10:00:00Z", "Q")]
    assert queuing_member(actions, "Q") == "bob"
    assert queuing_member(list(reversed(actions)), "Q") == "bob"


def test_queuing_member_none_without_match():
    assert queuing_member([], "Q") is None
    assert queuing_member([_moved("alice", "2026-11-20T10:00:00Z", "other")], "Q") is None


def test_trello_queued_by_and_me():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url.path)
        if req.url.path.endswith("/boards/board/lists"):
            return httpx.Response(200, json=[{"id": "Q", "name": "Queued"}])
        if req.url.path.endswith("/cards/a/actions"):
            assert req.url.params["filter"] == "createCard,updateCard"
            return httpx.Response(200, json=[_moved("Alice", "2026-11-20T10:00:00Z", "Q")])
        if req.url.path.endswith("/members/me"):
            return httpx.Response(200, json={"username": "Lights_Out"})
        return httpx.Response(404)

    tr = _trello(handler)
    assert tr.queued_by("a", "Queued") == "Alice"
    assert tr.me() == "lights_out"
    assert tr.me() == "lights_out"
    assert seen.count("/1/members/me") + seen.count("/members/me") == 1


def test_queuing_member_skips_ignored_member():
    actions = [_moved("alice", "2026-11-20T10:00:00Z", "Q"), _moved("Lights-Out-Bot", "2026-11-21T10:00:00Z", "Q")]
    assert queuing_member(actions, "Q") == "Lights-Out-Bot"
    assert queuing_member(actions, "Q", ignore="lights-out-bot") == "alice"
    assert queuing_member(actions[1:], "Q", ignore="lights-out-bot") is None


# --- CI failure logs ---------------------------------------------------------------
def _logs_client(runs, logs_handler, seen):
    def handler(req):
        seen.append(req)
        if req.url.host == "api.github.com" and req.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": runs})
        return logs_handler(req)

    http = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": "Bearer t"},
        transport=httpx.MockTransport(handler),
    )
    return GitHubClient("t", "o/r", http=http)


def _actions_run(id=42, slug="github-actions", conclusion="failure", name="test", output=None):
    return {
        "id": id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "app": {"slug": slug},
        "output": output or {"title": None, "summary": None, "text": None},
    }


def _redirecting(log):
    def handler(req):
        if req.url.path == "/repos/o/r/actions/jobs/42/logs":
            return httpx.Response(
                302, headers={"Location": "https://results-receiver.actions.githubusercontent.com/log.txt"}
            )
        return httpx.Response(200, text=log)

    return handler


def test_ci_failure_logs_follow_the_redirect_without_leaking_the_token():
    seen: list = []
    gh = _logs_client([_actions_run()], _redirecting("step failed: boom"), seen)
    assert gh.ci_failure_logs("sha") == [("test", "step failed: boom")]
    api = [r for r in seen if r.url.path.endswith("/logs")][0]
    target = [r for r in seen if r.url.host == "results-receiver.actions.githubusercontent.com"][0]
    assert api.headers["Authorization"] == "Bearer t"
    assert "Authorization" not in target.headers


def test_ci_failure_logs_keep_the_last_8000_characters():
    log = "".join(chr(97 + i % 26) for i in range(20000))
    gh = _logs_client([_actions_run()], _redirecting(log), [])
    [(_, text)] = gh.ci_failure_logs("sha")
    assert text == log[-8000:] and len(text) == 8000


def test_ci_failure_logs_fall_back_to_the_output_on_403():
    out = {"title": "Tests failed", "summary": "1 failed", "text": "details here"}
    gh = _logs_client([_actions_run(output=out)], lambda req: httpx.Response(403, json={}), [])
    [(name, text)] = gh.ci_failure_logs("sha")
    assert name == "test"
    assert "Tests failed" in text and "1 failed" in text and "details here" in text
    assert "full log couldn't be read" in text


def test_ci_failure_logs_for_other_apps_use_the_output_only():
    def handler(req):
        raise AssertionError(f"unexpected request {req.url}")

    out = {"title": "Lint", "summary": None, "text": "bad style"}
    gh = _logs_client([_actions_run(slug="other-ci", output=out)], handler, [])
    [(_, text)] = gh.ci_failure_logs("sha")
    assert "Lint" in text and "bad style" in text and "couldn't be read" in text


def test_ci_failure_logs_only_include_failed_runs():
    runs = [
        _actions_run(id=1, name="ok", conclusion="success"),
        _actions_run(id=2, name="skip", conclusion="skipped"),
        _actions_run(id=42, name="bad"),
    ]
    gh = _logs_client(runs, _redirecting("x"), [])
    assert [n for n, _ in gh.ci_failure_logs("sha")] == ["bad"]
