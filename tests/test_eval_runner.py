"""Evaluation runner with fake agents: real git and real checks, no Claude, no network."""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lights_out.agents.agents import AgentResult, ReviewResult, TriageResult
from lights_out.cli import main
from lights_out.eval.runner import eval_config, make_origin, run_eval, run_task
from lights_out.eval.tasks import Task


@dataclass
class EvalAgents:
    decision: str = "proceed"
    spent_usd: float = 0.0
    cost: float = 1.0
    calls: list[str] = field(default_factory=list)

    async def triage(self, ticket, cwd):
        self.spent_usd += self.cost
        return TriageResult(self.decision, "needs a format", ["Which format?"], "write feature.txt")

    async def implement(self, ticket, plan, cwd):
        (Path(cwd) / "feature.txt").write_text("ok\n")
        return AgentResult("Added feature.txt", "s1", 0.0)

    async def follow_up(self, session_id, message, cwd):
        return AgentResult("Fixed", session_id, 0.0)

    async def review(self, ticket, diff, cwd, risk_flags=None):
        return ReviewResult(True, [], "fine")


def factory(**kw):
    return lambda transcript_dir, budget: EvalAgents(**kw)


@pytest.fixture
def sample(tmp_path) -> Path:
    d = tmp_path / "sample"
    d.mkdir()
    (d / "Makefile").write_text("check:\n\ttest -f feature.txt\n")
    (d / "README.md").write_text("sample\n")
    return d


@pytest.fixture
def cfg(tmp_path):
    c = eval_config(tmp_path / "out")
    c.check_command = "make check"
    c.agent_user = ""
    return c


def make_task(tmp_path, task_id="t1", *, expected="proceed", acceptance="grep -qx ok feature.txt") -> Task:
    d = tmp_path / "tasks" / task_id
    (d / "acceptance").mkdir(parents=True)
    (d / "acceptance" / "note.txt").write_text("hidden\n")
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        description="Add feature.txt",
        category="feature" if expected == "proceed" else "ambiguous",
        expected=expected,
        dir=d,
        acceptance_command=acceptance,
    )


async def test_passing_proceed_task(tmp_path, sample, cfg):
    task = make_task(tmp_path)
    r = await run_task(task, cfg, sample, factory())
    assert r.outcome == "merged"
    assert r.check_ok is True and r.acceptance_ok is True
    assert r.passed
    assert r.review_rounds == 1 and r.fix_rounds == 0
    assert r.cost_usd == pytest.approx(1.0)


async def test_acceptance_runs_on_the_pushed_branch_with_hidden_tests(tmp_path, sample, cfg):
    task = make_task(tmp_path, acceptance="test -f tests/acceptance/note.txt && git log --oneline | grep -q t1")
    r = await run_task(task, cfg, sample, factory())
    assert r.acceptance_ok is True and r.passed
    assert not (sample / "tests").exists()  # the sample repo itself never sees them


async def test_hidden_tests_are_not_left_behind(tmp_path, sample, cfg):
    r = await run_task(make_task(tmp_path), cfg, sample, factory())
    assert r.acceptance_ok is True
    assert not list(cfg.data_dir.rglob("acceptance"))


async def test_acceptance_checkout_is_reachable_by_the_agent_user(tmp_path, sample, cfg, monkeypatch):
    from lights_out.eval import runner

    seen = {}
    real_workspace = runner.Workspace

    class Spy(real_workspace):
        """Records the user, then runs as the current user (the test environment has no `agent` account)."""

        def __init__(self, path, **kw):
            if path.name == "checkout":
                seen["agent_user"] = kw["agent_user"]
                seen["parent_mode"] = path.parent.stat().st_mode
            super().__init__(path, **{**kw, "agent_user": ""})

    monkeypatch.setattr(runner, "Workspace", Spy)
    cfg.agent_user = "agent"
    r = await run_task(make_task(tmp_path), cfg, sample, factory())
    assert r.passed
    assert seen["agent_user"] == "agent"  # the checks run as AGENT_USER
    assert seen["parent_mode"] & 0o001  # and that user can traverse the temp dir holding the checkout


async def test_failing_acceptance_is_recorded(tmp_path, sample, cfg):
    task = make_task(tmp_path, acceptance="echo hidden-test-broke; exit 3")
    r = await run_task(task, cfg, sample, factory())
    assert r.outcome == "merged"
    assert r.check_ok is True and r.acceptance_ok is False
    assert "hidden-test-broke" in r.acceptance_output
    assert not r.passed


async def test_needs_info_task(tmp_path, sample, cfg):
    task = make_task(tmp_path, expected="needs_info")
    r = await run_task(task, cfg, sample, factory(decision="needs_info"))
    assert r.outcome == "needs_info" and r.passed
    assert r.check_ok is None and r.acceptance_ok is None
    assert not (cfg.data_dir / "checkouts").exists()


async def test_needs_info_expected_but_agent_proceeds_fails(tmp_path, sample, cfg):
    r = await run_task(make_task(tmp_path, expected="needs_info"), cfg, sample, factory())
    assert r.outcome == "merged" and not r.passed
    assert r.check_ok is None  # no acceptance run for needs_info tasks


async def test_proceed_expected_but_needs_info_fails(tmp_path, sample, cfg):
    r = await run_task(make_task(tmp_path), cfg, sample, factory(decision="needs_info"))
    assert r.outcome == "needs_info" and not r.passed


async def test_exception_becomes_error_outcome(tmp_path, sample, cfg):
    def boom(d, b):
        raise RuntimeError("no agents today")

    r = await run_task(make_task(tmp_path), cfg, sample, boom)
    assert r.outcome == "error" and "no agents today" in r.error and not r.passed


async def test_max_usd_stops_the_run(tmp_path, sample, cfg):
    tasks = [make_task(tmp_path, f"t{i}") for i in range(3)]
    out = io.StringIO()
    data = await run_eval(tasks, cfg, sample, max_usd=1.5, agents_factory=factory(), out=out)
    assert data["total"]["runs"] == 2
    assert data["stopped"] == "max-usd reached"
    text = out.getvalue()
    assert "PASS t0" in text and "PASS t1" in text and "t2" not in text
    assert "max-usd reached" in text and "Total: 2/2 passed" in text
    on_disk = json.loads((cfg.data_dir / "results.json").read_text())
    assert on_disk["total"]["cost_usd"] == pytest.approx(2.0)
    assert [t["id"] for t in on_disk["tasks"]] == ["t0", "t1"]


async def test_repeat(tmp_path, sample, cfg):
    data = await run_eval([make_task(tmp_path)], cfg, sample, repeat=2, agents_factory=factory(), out=io.StringIO())
    assert [t["repeat"] for t in data["tasks"]] == [1, 2]
    assert data["stopped"] == ""


def test_make_origin_applies_patch_in_a_single_commit(tmp_path, sample):
    (sample / "a.txt").write_text("one\n")
    (sample / "__pycache__").mkdir()
    patch = tmp_path / "p.patch"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
    origin = make_origin(sample, patch, tmp_path / "work")

    def git(*a):
        return subprocess.run(["git", *a], cwd=origin, capture_output=True, text=True, check=True).stdout

    assert git("show", "main:a.txt") == "two\n"
    assert len(git("rev-list", "main").split()) == 1
    assert "__pycache__" not in git("ls-tree", "-r", "--name-only", "main")


def test_eval_help_needs_no_environment(monkeypatch, capsys):
    for name in ("TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["eval", "--help"])
    assert exc.value.code == 0
    assert "--max-usd" in capsys.readouterr().out


def test_eval_unknown_task_is_exit_2(monkeypatch, tmp_path, capsys):
    (tmp_path / "eval" / "sample-repo").mkdir(parents=True)
    (tmp_path / "eval" / "tasks").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["eval", "--tasks", "nope"]) == 2
    assert "unknown task" in capsys.readouterr().err


def test_eval_without_eval_dir_is_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["eval"]) == 2
    assert "not found" in capsys.readouterr().err
