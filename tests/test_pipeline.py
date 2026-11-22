"""End-to-end pipeline tests: real git (local bare repo), fake Trello/GitHub/agents."""

from __future__ import annotations

import asyncio
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lights_out.agents.agents import AgentResult, BudgetExceeded, ReviewResult, TriageResult
from lights_out.core.config import Config
from lights_out.core.models import Ticket
from lights_out.orchestration.pipeline import Pipeline
from lights_out.orchestration.scheduler import run_until_signal
from lights_out.orchestration.workspace import Workspace
from lights_out.reporting.store import Store


# --- fakes -------------------------------------------------------------------
class FakeTrello:
    def __init__(self, ticket: Ticket):
        self.ticket = ticket
        self.moves: list[str] = []
        self.comments: list[str] = []

    def move(self, card_id, list_name, top=True):
        self.moves.append(list_name)

    def comment(self, card_id, text):
        self.comments.append(text)

    def get_ticket(self, card_id):
        return self.ticket


@dataclass
class FakePR:
    number: int = 7
    url: str = "https://github.com/o/r/pull/7"
    head_sha: str = ""


class FakeGitHub:
    def __init__(self, ci="success"):
        self.ci = ci
        self.merged = False
        self.prs: list[dict] = []

    def open_or_update_pr(self, **kw):
        self.prs.append(kw)
        return FakePR(head_sha=kw["head_sha"])

    def wait_for_ci(self, sha, timeout_s, grace_s):
        return self.ci, (["check"] if self.ci == "failure" else [])

    def merge(self, number, title):
        self.merged = True
        return "abc"

    def delete_branch(self, branch):
        pass


@dataclass
class FakeAgents:
    decision: str = "proceed"
    write_file: bool = True
    broken_until_fix: bool = False  # first implementation fails the check
    reviews: list[bool] = field(default_factory=lambda: [True])
    raise_budget: bool = False
    spent_usd: float = 0.0
    calls: list[str] = field(default_factory=list)

    async def triage(self, ticket, cwd):
        self.calls.append("triage")
        self.spent_usd += 0.1
        return TriageResult(self.decision, "summary", ["What colour?"], "write feature.txt")

    async def implement(self, ticket, plan, cwd):
        self.calls.append("implement")
        if self.raise_budget:
            raise BudgetExceeded("Ticket budget of $1.00 used up")
        if self.write_file:
            (Path(cwd) / "feature.txt").write_text("broken" if self.broken_until_fix else "ok")
        self.spent_usd += 1.0
        return AgentResult("Added feature.txt", "s1", 1.0)

    async def follow_up(self, session_id, message, cwd):
        self.calls.append("fix")
        (Path(cwd) / "feature.txt").write_text("ok")
        self.spent_usd += 0.5
        return AgentResult("Fixed", session_id, 0.5)

    async def review(self, ticket, diff, cwd):
        self.calls.append("review")
        assert "feature.txt" in diff
        approved = self.reviews.pop(0)
        return ReviewResult(approved, [] if approved else ["needs more tests"], "looks fine")


# --- fixtures ------------------------------------------------------------------
def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path) -> Path:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    (seed / "README.md").write_text("hi\n")
    _git(seed, "add", ".")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    _git(seed, "push", "origin", "HEAD:main")
    return bare


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(
        trello_api_key="k",
        trello_token="t",
        trello_board_id="b",
        github_token="",
        repo_labels={"app": "o/app", "site": "o/site"},
        data_dir=tmp_path / "data",
        check_command="grep -qx ok feature.txt",
        max_fix_attempts=2,
    )
    for d in (c.workspaces_dir, c.reports_dir, c.runs_dir):
        d.mkdir(parents=True)
    return c


TICKET = Ticket(
    id="card1",
    short_id="12",
    title="Add a feature file",
    description="Please.",
    url="https://trello.com/c/x",
    labels=["App"],  # repository labels match case-insensitively
)


def make(cfg, origin, agents, github, ticket=TICKET):
    trello = FakeTrello(ticket)
    store = Store(cfg.db_path)
    trello.repos = []  # repositories the pipeline asked GitHub / git for

    def github_factory(repo):
        trello.repos.append(("github", repo))
        return github

    def workspace_factory(t, route):
        trello.repos.append(("workspace", route.repo))
        # every route clones the local test origin; the route itself is asserted via trello.repos
        return Workspace(cfg.workspaces_dir / t.id, repo_url=str(origin), base_branch="main", github_token="")

    pipe = Pipeline(
        cfg,
        trello,
        github_factory=github_factory,
        store=store,
        agents_factory=lambda d, budget: agents,
        workspace_factory=workspace_factory,
    )
    return pipe, trello, store


def branch_file(origin: Path, branch: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{branch}:{path}"], cwd=origin, capture_output=True, text=True, check=True
    ).stdout


# --- tests -----------------------------------------------------------------------
async def test_happy_path_merges_and_moves_to_done(cfg, origin):
    agents, gh = FakeAgents(), FakeGitHub("success")
    pipe, trello, store = make(cfg, origin, agents, gh)
    out = await pipe.process(TICKET, "2026-11-22", budget_usd=10)
    assert out.status == "merged", out.error
    assert gh.merged
    assert trello.moves == ["Triaged", "In Progress", "Verifying", "Reviewing", "PR Opened", "Done"]
    assert branch_file(origin, TICKET.branch, "feature.txt") == "ok"
    assert gh.prs[0]["head"] == TICKET.branch and gh.prs[0]["base"] == "main"
    run = store.day_runs("2026-11-22")[0]
    assert run.status == "merged" and run.pr_url and run.cost_usd == pytest.approx(1.1)


async def test_unclear_ticket_goes_to_needs_info_with_questions(cfg, origin):
    agents, gh = FakeAgents(decision="needs_info"), FakeGitHub()
    pipe, trello, store = make(cfg, origin, agents, gh)
    out = await pipe.process(TICKET, "n", budget_usd=10)
    assert out.status == "needs_info"
    assert agents.calls == ["triage"]
    assert trello.moves[-1] == "Needs Info"
    assert "What colour?" in trello.comments[-1]
    assert not gh.prs


def test_triage_schema_has_no_feature_only_gate():
    from lights_out.agents import prompts

    assert prompts.TRIAGE_SCHEMA["properties"]["decision"]["enum"] == ["proceed", "needs_info"]
    assert "not_a_feature" not in prompts.TRIAGE_SYSTEM


async def test_failing_checks_are_fed_back_to_the_coder(cfg, origin):
    agents, gh = FakeAgents(broken_until_fix=True), FakeGitHub()
    pipe, trello, store = make(cfg, origin, agents, gh)
    out = await pipe.process(TICKET, "n", budget_usd=10)
    assert out.status == "merged"
    assert agents.calls == ["triage", "implement", "fix", "review"]
    stages = [e["stage"] for e in store.all_events()]
    assert stages == ["workspace", "triage", "implement", "checks", "fix", "checks", "review 1", "deliver", "ci", "end"]
    assert (cfg.reports_dir / "data.json").exists()  # the dashboard follows the card live


async def test_review_rejection_triggers_revision(cfg, origin):
    agents, gh = FakeAgents(reviews=[False, True]), FakeGitHub()
    pipe, trello, store = make(cfg, origin, agents, gh)
    out = await pipe.process(TICKET, "n", budget_usd=10)
    assert out.status == "merged"
    assert agents.calls == ["triage", "implement", "review", "fix", "review"]


async def test_reviewer_never_approving_fails_the_ticket(cfg, origin):
    agents = FakeAgents(reviews=[False, False, False])
    pipe, trello, store = make(cfg, origin, agents, FakeGitHub())
    out = await pipe.process(TICKET, "n", budget_usd=10)
    assert out.status == "failed" and "needs more tests" in out.error
    assert trello.moves[-1] == "Failed"


async def test_no_changes_fails(cfg, origin):
    cfg.check_command = "true"
    pipe, trello, store = make(cfg, origin, FakeAgents(write_file=False), FakeGitHub())
    out = await pipe.process(TICKET, "n", budget_usd=10)
    assert out.status == "failed" and "without changing" in out.error


async def test_ci_failure_leaves_pr_open_and_does_not_merge(cfg, origin):
    gh = FakeGitHub("failure")
    pipe, trello, store = make(cfg, origin, FakeAgents(), gh)
    out = await pipe.process(TICKET, "n", budget_usd=10)
    assert out.status == "pr_open" and not gh.merged
    assert trello.moves[-1] == "Failed"


async def test_no_ci_does_not_merge_unless_allowed(cfg, origin):
    gh = FakeGitHub("none")
    pipe, *_ = make(cfg, origin, FakeAgents(), gh)
    assert (await pipe.process(TICKET, "n", budget_usd=10)).status == "pr_open"
    cfg.merge_without_ci = True
    gh2 = FakeGitHub("none")
    pipe2, *_ = make(cfg, origin, FakeAgents(), gh2)
    assert (await pipe2.process(TICKET, "n2", budget_usd=10)).status == "merged"


async def test_budget_exhaustion_is_reported_on_the_card(cfg, origin):
    pipe, trello, store = make(cfg, origin, FakeAgents(raise_budget=True), FakeGitHub())
    out = await pipe.process(TICKET, "n", budget_usd=1)
    assert out.status == "failed"
    assert "budget" in trello.comments[-1]
    assert store.day_runs("n")[0].status == "failed"


async def test_disabled_stage_columns_are_skipped(cfg, origin):
    cfg.lists.triaged = cfg.lists.verifying = cfg.lists.reviewing = cfg.lists.pr_opened = ""
    pipe, trello, store = make(cfg, origin, FakeAgents(), FakeGitHub())
    assert (await pipe.process(TICKET, "n", budget_usd=10)).status == "merged"
    assert trello.moves == ["In Progress", "Done"]


# --- repository routing --------------------------------------------------------
async def test_card_is_routed_to_the_repository_of_its_label(cfg, origin):
    pipe, trello, store = make(cfg, origin, FakeAgents(), FakeGitHub())
    assert (await pipe.process(TICKET, "n", budget_usd=10)).status == "merged"
    assert trello.repos == [("github", "o/app"), ("workspace", "o/app")]
    assert "`o/app`" in trello.comments[0]
    assert store.day_runs("n")[0].repo == "o/app"


@pytest.mark.parametrize(
    "labels,expected",
    [([], "no repository label"), (["urgent"], "no repository label"), (["app", "site"], "more than one")],
)
async def test_card_without_exactly_one_repo_label_goes_to_needs_info(cfg, origin, labels, expected):
    ticket = Ticket(id="c2", short_id="2", title="t", description="d", url="u", labels=labels)
    agents = FakeAgents()
    pipe, trello, store = make(cfg, origin, agents, FakeGitHub(), ticket=ticket)
    out = await pipe.process(ticket, "n", budget_usd=10)
    assert out.status == "needs_info"
    assert agents.calls == [] and trello.repos == []  # nothing cloned, no agent spend
    assert trello.moves == ["Needs Info"]
    assert expected in trello.comments[-1] and "**app**" in trello.comments[-1]


async def test_default_repo_catches_unlabelled_cards(cfg, origin):
    cfg.default_repo = "o/default"
    ticket = Ticket(id="c3", short_id="3", title="t", description="d", url="u", labels=["urgent"])
    pipe, trello, store = make(cfg, origin, FakeAgents(), FakeGitHub(), ticket=ticket)
    assert (await pipe.process(ticket, "n", budget_usd=10)).status == "merged"
    assert ("workspace", "o/default") in trello.repos


class BlockingAgents(FakeAgents):
    """implement() signals that it started, then waits forever (until cancelled)."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.never = asyncio.Event()

    async def implement(self, ticket, plan, cwd):
        self.entered.set()
        await self.never.wait()


async def test_cancellation_puts_card_back_in_queue_and_records_interrupted(cfg, origin):
    agents, gh = BlockingAgents(), FakeGitHub()
    pipe, trello, store = make(cfg, origin, agents, gh)
    task = asyncio.create_task(pipe.process(TICKET, "2026-11-22", budget_usd=10))
    await asyncio.wait_for(agents.entered.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert trello.moves[-1] == "Queued"
    assert "shutting down" in trello.comments[-1] and "'implement'" in trello.comments[-1]
    run = store.day_runs("2026-11-22")[0]
    assert run.status == "interrupted" and "implement" in run.error
    assert store.running() == []
    assert not gh.prs


class SignalDuringCI(FakeGitHub):
    def wait_for_ci(self, sha, timeout_s, grace_s):
        signal.raise_signal(signal.SIGTERM)  # a stop request arrives while delivery is blocking
        return super().wait_for_ci(sha, timeout_s, grace_s)


async def test_stop_during_delivery_finishes_the_card(cfg, origin):
    gh = SignalDuringCI("success")
    pipe, trello, store = make(cfg, origin, FakeAgents(), gh)
    stopped = await asyncio.wait_for(run_until_signal(pipe.process(TICKET, "2026-11-22", budget_usd=10)), timeout=60)
    assert stopped is True
    assert gh.merged
    assert trello.moves[-1] == "Done"
    assert store.day_runs("2026-11-22")[0].status == "merged"
    assert store.running() == []
