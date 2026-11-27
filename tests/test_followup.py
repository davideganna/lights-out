"""PR watcher: merge on approval, sync PRs merged or closed by hand."""

from __future__ import annotations

import json

import httpx
import pytest

from lights_out.core.config import Config
from lights_out.integrations.github import GitHubClient
from lights_out.orchestration.followup import PRWatcher
from lights_out.orchestration.pipeline import BOT, MAX_ATTEMPTS_PER_DAY, Outcome, today
from lights_out.reporting.store import Store

SHA = "abcdef1234567890"
T0 = "2026-01-01T10:00:00Z"
AFTER = "2026-01-01T11:00:00Z"
BEFORE = "2026-01-01T09:00:00Z"
CMD = "/lights-out merge"


class FakeTrello:
    def __init__(self):
        self.comments: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.current_list: str | None = "PR Opened"

    def card_list(self, card_id):
        return self.current_list

    def comment(self, card_id, text):
        self.comments.append((card_id, text))

    def move(self, card_id, list_name, top=True):
        self.moves.append((card_id, list_name))


class FakeAlerter:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, event, text):
        self.sent.append((event, text))


class FakeGitHub:
    """Serves GitHub routes through httpx.MockTransport and records every request."""

    def __init__(self):
        self.pull = {
            "number": 7,
            "html_url": "https://github.com/o/app/pull/7",
            "state": "open",
            "merged": False,
            "merged_by": None,
            "user": {"login": "owner"},
            "head": {"sha": SHA, "ref": "lights-out/7"},
            "title": "Add thing",
        }
        self.comments: list[dict] = []
        self.reviews: list[dict] = []
        self.line_comments: list[dict] = []
        self.perms = {"alice": {"permission": "write", "role_name": "write"}}
        self.checks: list[dict] = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.merge_status = 200
        self.delete_error = False
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/repos/o/app")
        self.requests.append((req.method, path))
        if req.content:
            self.bodies.append(json.loads(req.content))
        m = req.method
        if m == "GET" and path == "/pulls/7":
            return httpx.Response(200, json=self.pull)
        if m == "GET" and path == f"/commits/{SHA}":
            return httpx.Response(200, json={"commit": {"committer": {"date": T0}}})
        if m == "GET" and path == "/issues/7/comments":
            return httpx.Response(200, json=self.comments)
        if m == "GET" and path == "/pulls/7/comments":
            return httpx.Response(200, json=self.line_comments)
        if m == "GET" and path == "/pulls/7/reviews":
            return httpx.Response(200, json=self.reviews)
        if m == "GET" and path.startswith("/collaborators/"):
            user = path.split("/")[2]
            if user in self.perms:
                return httpx.Response(200, json=self.perms[user])
            return httpx.Response(404, json={})
        if m == "GET" and path == f"/commits/{SHA}/check-runs":
            return httpx.Response(200, json={"check_runs": self.checks})
        if m == "PUT" and path == "/pulls/7/merge":
            if self.merge_status >= 400:
                return httpx.Response(self.merge_status, json={"message": "not mergeable"})
            return httpx.Response(200, json={"sha": "m1"})
        if m == "DELETE" and path in ("/git/refs/heads/lights-out/7", "/git/refs/heads/agent/7-x"):
            if self.delete_error:
                raise httpx.ConnectError("down")
            return httpx.Response(204)
        if m == "POST" and path == "/issues/7/comments":
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"message": f"unexpected {m} {path}"})

    def client(self) -> GitHubClient:
        http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(self.handler))
        return GitHubClient("t", "o/app", http=http)

    def calls(self, method: str) -> list[str]:
        return [p for m, p in self.requests if m == method]

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests if m != "GET"]


def comment(id_: int, user: str, body: str = CMD, at: str = AFTER) -> dict:
    return {"id": id_, "user": {"login": user}, "body": body, "created_at": at}


def review(id_: int, user: str, state: str = "APPROVED", commit: str = SHA, body: str = "", at: str = AFTER) -> dict:
    return {
        "id": id_,
        "user": {"login": user},
        "state": state,
        "commit_id": commit,
        "submitted_at": at,
        "body": body,
    }


def line_comment(id_: int, user: str, body: str, path: str = "a.py", line=3, at: str = AFTER, **extra) -> dict:
    return {"id": id_, "user": {"login": user}, "body": body, "path": path, "line": line, "created_at": at, **extra}


class Env:
    def __init__(self, tmp_path):
        self.cfg = Config(
            trello_api_key="k",
            trello_token="t",
            trello_board_id="b",
            github_token="",
            repo_labels={"app": "o/app"},
            data_dir=tmp_path,
            pr_poll_seconds=120,
        )
        self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(tmp_path / "s.db")
        self.trello = FakeTrello()
        self.gh = FakeGitHub()
        self.now = 0.0
        self.revisions: list[tuple] = []
        self.alerter = FakeAlerter()
        self.watcher = PRWatcher(
            self.cfg, self.trello, self.store, lambda repo: self.gh.client(), lambda: self.now, alerter=self.alerter
        )
        self.revising = PRWatcher(
            self.cfg,
            self.trello,
            self.store,
            lambda repo: self.gh.client(),
            lambda: self.now,
            revise=self.fake_revise,
            alerter=self.alerter,
        )

    async def fake_revise(self, run, pr, feedback):
        self.revisions.append((run, pr, feedback))
        return Outcome("pr_open", summary="revised")

    def seed(self, *, repo="o/app", status="pr_open", card="c1", cost=1.5, url="https://github.com/o/app/pull/7"):
        rid = self.store.start(day="2026-01-01", card_id=card, card_short_id="1", title="t", card_url="u")
        self.store.set_repo(rid, repo)
        self.store.finish(rid, status=status, cost_usd=cost, summary="did it", pr_url=url)
        return rid

    def run(self, rid):
        return next(r for r in self.store.all_runs() if r.id == rid)

    def events(self, rid):
        return [e["stage"] for e in self.store.all_events() if e["run_id"] == rid]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


async def test_merged_by_human(env):
    rid = env.seed()
    env.gh.pull.update(state="closed", merged=True, merged_by={"login": "bob"})
    await env.watcher.check_all()
    run = env.run(rid)
    assert run.status == "merged" and run.cost_usd == 1.5 and run.summary == "did it"
    assert run.pr_url and run.finished_at
    assert ("c1", "Done") in env.trello.moves
    assert "@bob" in env.trello.comments[0][1]
    ev = [e for e in env.store.all_events() if e["stage"] == "merged_by_human"]
    assert len(ev) == 1 and ev[0]["cost_usd"] == 1.5


async def test_closed_without_merging(env):
    rid = env.seed()
    env.gh.pull.update(state="closed")
    await env.watcher.check_all()
    run = env.run(rid)
    assert run.status == "failed" and run.error == "PR closed without merging"
    assert env.trello.moves == [("c1", env.cfg.lists.failed or env.cfg.lists.needs_info)]
    assert env.trello.comments


@pytest.mark.parametrize(
    "perm",
    [{"permission": "write", "role_name": "write"}, {"permission": "write", "role_name": "maintain"}],
)
async def test_command_from_writer_with_green_ci_merges(env, perm):
    rid = env.seed()
    env.gh.perms["alice"] = perm
    env.gh.comments = [comment(10, "alice", f"  {CMD}\n")]
    await env.watcher.check_all()
    assert env.gh.calls("PUT") == ["/pulls/7/merge"]
    assert env.gh.bodies[0]["merge_method"] == "squash"
    assert env.gh.calls("DELETE") == ["/git/refs/heads/lights-out/7"]
    assert env.gh.calls("POST") == ["/issues/7/comments"]
    assert ("c1", "Done") in env.trello.moves
    assert env.run(rid).status == "merged"
    assert "merged_on_approval" in env.events(rid)
    assert env.run(rid).cost_usd == 1.5


async def test_command_with_failing_ci_only_replies(env):
    rid = env.seed()
    env.gh.checks = [{"name": "lint", "status": "completed", "conclusion": "failure"}]
    env.gh.comments = [comment(10, "alice")]
    await env.watcher.check_all()
    assert env.gh.writes() == [("POST", "/issues/7/comments")]
    assert "lint" in env.gh.bodies[-1]["body"] and "abcdef1" in env.gh.bodies[-1]["body"]
    assert env.trello.moves == [] and env.trello.comments == []
    assert env.run(rid).status == "pr_open"


async def test_command_from_reader_ignored(env):
    rid = env.seed()
    env.gh.perms["rita"] = {"permission": "read", "role_name": "read"}
    env.gh.comments = [comment(10, "rita"), comment(11, "nobody")]
    await env.watcher.check_all()
    assert env.gh.writes() == []
    assert env.run(rid).status == "pr_open"


async def test_command_older_than_head_commit_ignored(env):
    env.seed()
    env.gh.comments = [comment(10, "alice", at=BEFORE)]
    await env.watcher.check_all()
    assert env.gh.writes() == []
    assert not any(p.startswith("/collaborators") for p in env.gh.calls("GET"))


async def test_bot_comment_and_extra_text_ignored(env):
    env.seed()
    env.gh.comments = [
        comment(10, "alice", f"🤖 **lights-out** reply: post `{CMD}`"),
        comment(11, "alice", f"🤖 **lights-out**\n{CMD}"),
        comment(12, "alice", f"{CMD} please"),
    ]
    await env.watcher.check_all()
    assert env.gh.writes() == []


async def test_command_handled_only_once(env):
    env.seed()
    env.gh.comments = [comment(10, "alice")]
    env.gh.checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
    await env.watcher.check_all()
    await env.watcher.check_all()
    assert env.gh.calls("POST") == ["/issues/7/comments"]  # one reply only
    assert env.gh.calls("PUT") == []
    env.gh.checks = [{"name": "ci", "status": "completed", "conclusion": "success"}]
    await env.watcher.check_all()
    assert env.gh.calls("PUT") == []  # old command stays handled
    env.gh.comments.append(comment(11, "alice"))
    await env.watcher.check_all()
    assert env.gh.calls("PUT") == ["/pulls/7/merge"]


async def test_merge_error_replies_and_keeps_run_open(env):
    rid = env.seed()
    env.gh.merge_status = 409
    env.gh.comments = [comment(10, "alice")]
    await env.watcher.check_all()
    await env.watcher.check_all()
    assert env.gh.calls("PUT") == ["/pulls/7/merge"]
    assert env.gh.calls("POST") == ["/issues/7/comments"]
    assert env.run(rid).status == "pr_open" and env.trello.moves == []


async def test_approving_review_from_other_writer_merges(env):
    rid = env.seed()
    env.gh.reviews = [review(1, "owner"), review(2, "alice", commit="old"), review(3, "alice", state="COMMENTED")]
    await env.watcher.check_all()
    assert env.gh.writes() == []
    env.gh.reviews.append(review(4, "alice"))
    await env.watcher.check_all()
    assert env.gh.calls("PUT") == ["/pulls/7/merge"]
    assert env.run(rid).status == "merged"


async def test_unconfigured_repo_and_non_open_runs_skipped(env):
    env.seed(repo="other/repo", card="c1")
    env.seed(status="merged", card="c2")
    # latest run of c3 is not pr_open even though an earlier one was
    env.seed(card="c3")
    env.seed(status="failed", card="c3")
    env.gh.comments = [comment(10, "alice")]
    await env.watcher.check_all()
    assert env.gh.requests == []


async def test_one_failing_pr_does_not_stop_others(env):
    env.seed(card="c1", url="https://github.com/o/app/pull/99")
    rid = env.seed(card="c2")
    env.gh.pull.update(state="closed", merged=True, merged_by={"login": "bob"})
    await env.watcher.check_all()
    assert env.run(rid).status == "merged"


async def test_maybe_check_skips_while_running(env):
    env.seed()
    env.store.start(day="2026-01-01", card_id="x", card_short_id="2", title="t", card_url="u")
    await env.watcher.maybe_check()
    assert env.gh.requests == []


async def test_maybe_check_respects_poll_interval(env):
    env.seed()
    await env.watcher.maybe_check()
    n = len(env.gh.requests)
    assert n > 0
    env.now = 119
    await env.watcher.maybe_check()
    assert len(env.gh.requests) == n
    env.now = 120
    await env.watcher.maybe_check()
    assert len(env.gh.requests) > n


async def test_trello_and_network_errors_are_contained(env):
    from lights_out.integrations.trello import TrelloError

    rid = env.seed()
    env.gh.pull.update(state="closed", merged=True, merged_by={"login": "bob"})

    def boom(card_id, text):
        raise TrelloError("down")

    env.trello.comment = boom
    await env.watcher.check_all()  # logged, not raised
    assert env.run(rid).status == "pr_open"  # retried at the next poll
    env.gh.handler = lambda req: (_ for _ in ()).throw(httpx.ConnectError("offline"))
    await env.watcher.check_all()
    assert env.run(rid).status == "pr_open"


# --- feedback -> revision -------------------------------------------------------


async def test_writer_comment_triggers_one_revision(env):
    env.seed()
    env.gh.comments = [comment(10, "alice", "please rename foo")]
    await env.revising.check_all()
    assert len(env.revisions) == 1
    run, pr, fb = env.revisions[0]
    assert pr.number == 7 and run.card_id == "c1"
    assert [(f.kind, f.id, f.user, f.body) for f in fb] == [("feedback-comment", "10", "alice", "please rename foo")]
    assert fb[0].quote() == "@alice: please rename foo"


async def test_line_comments_carry_file_and_line_with_fallback(env):
    env.seed()
    env.gh.line_comments = [
        line_comment(20, "alice", "nit", path="src/x.py", line=12),
        line_comment(21, "alice", "outdated", path="src/y.py", line=None, original_line=5),
    ]
    await env.revising.check_all()
    fb = env.revisions[0][2]
    assert [(f.kind, f.path, f.line) for f in fb] == [
        ("feedback-line", "src/x.py", 12),
        ("feedback-line", "src/y.py", 5),
    ]
    assert fb[0].quote() == "@alice (src/x.py:12): nit"


async def test_review_bodies(env):
    env.seed()
    env.gh.reviews = [
        review(1, "alice", "CHANGES_REQUESTED", body="fix this"),
        review(2, "alice", "COMMENTED", body="and that"),
        review(3, "alice", "COMMENTED", body=""),
        review(4, "alice", "APPROVED", commit="old", body="lgtm"),
    ]
    await env.revising.check_all()
    assert [(f.kind, f.body) for f in env.revisions[0][2]] == [
        ("feedback-review", "fix this"),
        ("feedback-review", "and that"),
    ]


async def test_ignored_feedback(env):
    env.seed()
    env.gh.perms["rita"] = {"permission": "read", "role_name": "read"}
    env.gh.comments = [
        comment(10, "alice", "old", at=BEFORE),
        comment(11, "rita", "reader"),
        comment(12, "nobody", "stranger"),
        comment(13, "alice", f"{BOT} revised this PR"),
        comment(15, "alice", "   "),
    ]
    env.gh.line_comments = [line_comment(20, "alice", "old", at=BEFORE), line_comment(21, "rita", "reader")]
    env.gh.reviews = [review(1, "alice", "COMMENTED", body="old", at=BEFORE)]
    await env.revising.check_all()
    assert env.revisions == []
    assert env.gh.writes() == []


async def test_approval_wins_over_feedback(env):
    rid = env.seed()
    env.gh.comments = [comment(10, "alice", "please change"), comment(11, "alice", CMD)]
    await env.revising.check_all()
    assert env.revisions == []
    assert env.gh.calls("PUT") == ["/pulls/7/merge"]
    assert env.run(rid).status == "merged"


async def test_handled_feedback_does_not_trigger_again(env):
    rid = env.seed()
    env.gh.comments = [comment(10, "alice", "change it")]
    env.gh.line_comments = [line_comment(20, "alice", "here")]
    env.gh.reviews = [review(1, "alice", "COMMENTED", body="body")]
    for kind, i in (("feedback-comment", "10"), ("feedback-line", "20"), ("feedback-review", "1")):
        env.store.mark_handled("o/app", kind, i, rid)
    await env.revising.check_all()
    assert env.revisions == []


async def test_check_all_stops_after_a_revision(env):
    env.seed(card="c1")
    env.seed(card="c2", url="https://github.com/o/app/pull/7")
    env.gh.comments = [comment(10, "alice", "change it")]
    await env.revising.check_all()
    assert len(env.revisions) == 1


async def test_max_pr_revisions_enforced_with_one_comment(env):
    rid = env.seed()
    for _ in range(env.cfg.max_pr_revisions):
        env.store.add_revision("o/app", 7, rid)
    env.gh.comments = [comment(10, "alice", "again")]
    await env.revising.check_all()
    await env.revising.check_all()
    assert env.revisions == []
    posts = [b["body"] for b in env.gh.bodies]
    assert len(posts) == 1 and "human needs to take over" in posts[0] and "MAX_PR_REVISIONS" in posts[0]


async def test_daily_budget_defers_feedback(env):
    rid = env.seed(cost=env.cfg.max_day_usd)
    env.store._db.execute("UPDATE runs SET day=?", (today(),))
    env.gh.comments = [comment(10, "alice", "change it")]
    await env.revising.check_all()
    assert env.revisions == [] and env.gh.writes() == []
    env.store._db.execute("UPDATE runs SET cost_usd=1 WHERE id=?", (rid,))
    await env.revising.check_all()
    assert len(env.revisions) == 1


async def test_attempt_limit_defers_feedback(env):
    rid = env.seed(cost=0)
    env.store._db.execute("UPDATE runs SET day=? WHERE id=?", (today(), rid))
    for _ in range(MAX_ATTEMPTS_PER_DAY - 1):  # earlier attempts today; the latest run stays pr_open
        env.store._db.execute(
            "INSERT INTO runs (day, card_id, card_short_id, title, card_url, status, started_at) "
            "VALUES (?, 'c1', '1', 't', 'u', 'failed', 'x')",
            (today(),),
        )
    env.store._db.execute("UPDATE runs SET id = id + 100 WHERE id = ?", (rid,))
    env.store._db.commit()
    assert env.store.attempts("c1", today()) == MAX_ATTEMPTS_PER_DAY
    env.gh.comments = [comment(10, "alice", "change it")]
    await env.revising.check_all()
    assert env.revisions == []
    assert env.gh.writes() == []


async def test_without_revise_callback_feedback_is_ignored(env):
    env.seed()
    env.gh.comments = [comment(10, "alice", "change it")]
    await env.watcher.check_all()
    assert env.gh.requests and not any(p.startswith("/collaborators") for p in env.gh.calls("GET"))
    assert env.gh.writes() == []


async def test_review_comments_and_review_body_parsing():
    gh = FakeGitHub()
    gh.line_comments = [line_comment(1, "a", "x", line=None, original_line=9)]
    gh.reviews = [review(2, "a", "COMMENTED", body="hello")]
    c = gh.client()
    assert c.review_comments(7) == [{"id": 1, "user": "a", "body": "x", "path": "a.py", "line": 9, "created_at": AFTER}]
    assert c.reviews(7)[0]["body"] == "hello"


def _merge_by_human(env, ref="agent/7-x"):
    env.gh.pull.update(state="closed", merged=True, merged_by={"login": "bob"})
    env.gh.pull["head"]["ref"] = ref


async def test_merged_by_human_deletes_agent_branch(env):
    rid = env.seed()
    before = env.run(rid)
    _merge_by_human(env)
    await env.watcher.check_all()
    after = env.run(rid)
    assert after.status == "merged" and after.cost_usd == before.cost_usd
    assert after.finished_at == before.finished_at
    assert ("c1", "Done") in env.trello.moves and env.trello.comments
    assert env.gh.calls("DELETE") == ["/git/refs/heads/agent/7-x"]


async def test_merged_by_human_non_agent_branch_is_kept(env):
    env.seed()
    _merge_by_human(env, "lights-out/7")
    await env.watcher.check_all()
    assert env.gh.calls("DELETE") == []


async def test_closed_sends_one_pr_open_alert(env):
    rid = env.seed()
    env.gh.pull.update(state="closed")
    await env.watcher.check_all()
    run = env.run(rid)
    assert run.status == "failed" and "pr_closed" in env.events(rid)
    assert [e for e, _ in env.alerter.sent] == ["pr_open"]
    assert "https://github.com/o/app/pull/7" in env.alerter.sent[0][1]


@pytest.mark.parametrize("where", ["Done", "Backlog"])
@pytest.mark.parametrize("merged", [True, False])
async def test_card_already_moved_by_human_is_not_moved(env, where, merged):
    rid = env.seed()
    env.trello.current_list = where
    env.gh.pull.update(state="closed", merged=merged, merged_by={"login": "bob"} if merged else None)
    await env.watcher.check_all()
    assert env.trello.moves == []
    assert env.run(rid).status == ("merged" if merged else "failed")
    n = len(env.gh.requests)
    await env.watcher.check_all()
    assert len(env.gh.requests) == n


async def test_open_pr_is_untouched(env):
    rid = env.seed()
    await env.watcher.check_all()
    assert env.trello.moves == [] and env.alerter.sent == []
    assert env.run(rid).status == "pr_open"


async def test_branch_delete_outage_does_not_undo_the_merge(env):
    rid = env.seed()
    _merge_by_human(env)
    env.gh.delete_error = True
    await env.watcher.check_all()
    assert env.run(rid).status == "merged"


async def test_poll_seconds_zero_disables_the_watcher(env):
    env.seed()
    env.cfg.pr_poll_seconds = 0
    await env.watcher.maybe_check()
    await env.watcher.check_now()
    assert env.gh.requests == []


async def test_check_now_runs_immediately_then_interval_applies(env):
    env.seed()
    await env.watcher.check_now()
    n = len(env.gh.requests)
    assert n > 0
    env.now = 10
    await env.watcher.maybe_check()
    assert len(env.gh.requests) == n
