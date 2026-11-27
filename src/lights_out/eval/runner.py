"""Run evaluation tasks through the real pipeline and check the result with hidden acceptance tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from ..agents.agents import AgentsProtocol, ClaudeAgents
from ..core import config as config_module
from ..core.config import Config, Lists, _effort, _get
from ..integrations.github import GitHubClient
from ..integrations.trello import TrelloClient
from ..orchestration.pipeline import Pipeline
from ..orchestration.workspace import Workspace
from ..reporting.store import Store
from .fakes import LocalGitHub, MemoryTracker
from .tasks import Task

AgentsFactory = Callable[[Path, float], AgentsProtocol]
_IGNORED = shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "*.egg-info", ".git")
_TAIL = 3000


@dataclass
class TaskResult:
    id: str
    category: str
    expected: str
    repeat: int = 1
    outcome: str = ""  # merged | needs_info | failed | pr_open | error
    check_ok: bool | None = None
    check_output: str = ""
    acceptance_ok: bool | None = None
    acceptance_output: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    fix_rounds: int = 0
    review_rounds: int = 0
    error: str = ""
    passed: bool = False


def eval_config(out_dir: Path) -> Config:
    """Configuration for an evaluation run: no Trello or GitHub, everything stored under `out_dir`."""
    for name in config_module._ORCHESTRATOR_SECRETS:  # the agents must not see these
        os.environ.pop(name, None)
    cfg = Config(
        trello_api_key="eval",
        trello_token="eval",
        trello_board_id="eval",
        github_token="",
        repo_labels={},
        allowed_members=[],
        default_repo="eval/ledger",
        lists=Lists(),
        planner_model=_get("PLANNER_MODEL", "claude-opus-5-5"),
        coder_model=_get("CODER_MODEL", "claude-sonnet-5-5"),
        reviewer_model=_get("REVIEWER_MODEL", "claude-opus-5-5"),
        planner_effort=_effort("PLANNER_EFFORT", "high"),
        coder_effort=_effort("CODER_EFFORT", ""),
        reviewer_effort=_effort("REVIEWER_EFFORT", "high"),
        max_ticket_usd=float(_get("MAX_TICKET_USD", "10")),
        max_fix_attempts=int(_get("MAX_FIX_ATTEMPTS", "3")),
        max_review_rounds=int(_get("MAX_REVIEW_ROUNDS", "2")),
        check_command=_get("CHECK_COMMAND", "make check"),
        check_timeout_s=int(_get("CHECK_TIMEOUT_S", "1200")),
        merge_without_ci=True,
        human_review_paths=[],
        max_auto_merge_lines=0,
        data_dir=out_dir,
        agent_user=_get("AGENT_USER", ""),
    )
    for d in (cfg.workspaces_dir, cfg.reports_dir, cfg.runs_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def _git(cwd: Path, *args: str) -> None:
    r = subprocess.run(
        ["git", "-c", "user.name=eval", "-c", "user.email=eval@localhost", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:500]}")


def make_origin(sample_repo: Path, patch: Path | None, tmp: Path) -> Path:
    """A fresh bare repository holding the sample repo (plus `patch`) in a single commit."""
    work = tmp / "seed"
    shutil.copytree(sample_repo, work, ignore=_IGNORED)
    _git(work, "init", "-q", "-b", "main")
    if patch is not None:
        _git(work, "apply", str(Path(patch).resolve()))
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "Initial commit")
    bare = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(work, "push", "-q", str(bare), "main")
    return bare


def _passed(task: Task, r: TaskResult) -> bool:
    if task.expected == "needs_info":
        return r.outcome == "needs_info"
    return r.outcome == "merged" and r.check_ok is True and r.acceptance_ok is True


def _acceptance(task: Task, cfg: Config, origin: Path, branch: str, checkout: Path, r: TaskResult) -> None:
    """Check the pushed branch out fresh, add the hidden tests and run `make check` and the acceptance command."""
    ws = Workspace(
        checkout,
        repo_url=str(origin),
        base_branch=branch,
        github_token="",
        agent_user=cfg.agent_user,
    )
    ws.clone()
    check = ws.run_check(
        cfg.check_command, cfg.check_timeout_s
    )  # the agent's work as delivered, before the hidden tests
    r.check_ok, r.check_output = check.ok, check.output[-_TAIL:]
    if not check.ok:
        return
    shutil.copytree(task.acceptance_dir, ws.path / "tests" / "acceptance", dirs_exist_ok=True)
    acc = ws.run_check(task.acceptance_command, cfg.check_timeout_s)
    r.acceptance_ok, r.acceptance_output = acc.ok, acc.output[-_TAIL:]


async def run_task(
    task: Task,
    cfg: Config,
    sample_repo: Path,
    agents_factory: AgentsFactory | None = None,
    repeat_index: int = 1,
) -> TaskResult:
    result = TaskResult(id=task.id, category=task.category, expected=task.expected, repeat=repeat_index)
    store = Store(cfg.db_path)
    name = f"{task.id}-{repeat_index}"
    ticket = task.ticket()
    started = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="lights-out-eval-"))
    # mkdtemp makes it 0700 root; the agent user runs the checks in tmp/checkout, so it must be able to traverse
    # tmp (not list it). Safe: the hidden tests are only copied in after the pipeline has finished.
    tmp.chmod(0o711)
    try:
        # Only sample_repo and setup_patch reach the agent; never apply or copy solution.patch or acceptance/
        origin = make_origin(sample_repo, task.setup_patch, tmp)
        pipe = Pipeline(
            cfg,
            cast(TrelloClient, MemoryTracker(ticket)),
            github_factory=lambda repo: cast(GitHubClient, LocalGitHub()),
            store=store,
            agents_factory=agents_factory or (lambda d, b: ClaudeAgents(cfg, d, b)),
            workspace_factory=lambda t, route: Workspace(
                cfg.workspaces_dir / name,
                repo_url=str(origin),
                base_branch="main",
                github_token="",
                agent_user=cfg.agent_user,
                author_name=cfg.git_author_name,
                author_email=cfg.git_author_email,
            ),
        )
        outcome = await pipe.process(ticket, day=datetime.now(UTC).date().isoformat(), budget_usd=cfg.max_ticket_usd)
        result.outcome = outcome.status
        result.error = outcome.error
        runs = store.all_runs()
        if runs:
            run = runs[-1]
            result.cost_usd = run.cost_usd
            stages = [e["stage"] for e in store.all_events() if e["run_id"] == run.id]
            result.fix_rounds = sum(1 for s in stages if s == "fix" or s.startswith("ci fix"))
            result.review_rounds = sum(1 for s in stages if s.startswith("review "))
        if task.expected == "proceed" and outcome.status == "merged":
            # Inside the temp dir (removed below), so the hidden tests never linger where agents can read them.
            _acceptance(task, cfg, origin, ticket.branch, tmp / "checkout", result)
    except Exception as e:  # a broken run is a result, not a crash
        result.outcome = "error"
        result.error = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    result.duration_s = round(time.monotonic() - started, 1)
    result.passed = _passed(task, result)
    return result


def _flag(ok: bool | None) -> str:
    return "n/a" if ok is None else ("pass" if ok else "fail")


def format_result(r: TaskResult) -> str:
    return (
        f"{'PASS' if r.passed else 'FAIL'} {r.id} [{r.category}, expected {r.expected}] {r.outcome} "
        f"check={_flag(r.check_ok)} acceptance={_flag(r.acceptance_ok)} ${r.cost_usd:.2f} {r.duration_s:.0f}s "
        f"fix={r.fix_rounds} review={r.review_rounds}" + (f" error={r.error.splitlines()[0][:120]}" if r.error else "")
    )


async def run_eval(
    tasks: list[Task],
    cfg: Config,
    sample_repo: Path,
    *,
    repeat: int = 1,
    max_usd: float = 30.0,
    agents_factory: AgentsFactory | None = None,
    out: TextIO = sys.stdout,
) -> dict:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    results: list[TaskResult] = []
    stopped = ""
    for n in range(1, repeat + 1):
        for task in tasks:
            if sum(r.cost_usd for r in results) >= max_usd:
                stopped = "max-usd reached"
                break
            r = await run_task(task, cfg, sample_repo, agents_factory, n)
            results.append(r)
            print(format_result(r), file=out, flush=True)
        if stopped:
            break
    total = {
        "runs": len(results),
        "passed": sum(1 for r in results if r.passed),
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "duration_s": round(sum(r.duration_s for r in results), 1),
    }
    data = {"started_at": started_at, "tasks": [asdict(r) for r in results], "total": total, "stopped": stopped}
    if stopped:
        print(f"Stopped: {stopped} (${total['cost_usd']:.2f} spent, limit ${max_usd:.2f})", file=out)
    print(
        f"Total: {total['passed']}/{total['runs']} passed, ${total['cost_usd']:.2f}, {total['duration_s']:.0f}s",
        file=out,
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "results.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data
