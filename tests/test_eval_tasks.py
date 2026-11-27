"""Evaluation task format, and the sample repository and tasks shipped in eval/."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lights_out.eval.tasks import DEFAULT_ACCEPTANCE_COMMAND, TaskError, load_task, load_tasks

EVAL = Path(__file__).resolve().parent.parent / "eval"
SAMPLE = EVAL / "sample-repo"
TASKS = EVAL / "tasks"
_COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "*.egg-info")

VALID = 'id = "t1"\ntitle = "T"\ndescription = "Do it"\ncategory = "feature"\nexpected = "proceed"\n'


def _task(tmp_path: Path, toml: str, *, acceptance: bool = True, name: str = "t1") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "task.toml").write_text(toml)
    if acceptance:
        (d / "acceptance").mkdir()
        (d / "acceptance" / "test_x.py").write_text("def test_x(): pass\n")
    return d


def test_valid_task_loads_with_defaults(tmp_path):
    t = load_task(_task(tmp_path, VALID))
    assert (t.id, t.category, t.expected) == ("t1", "feature", "proceed")
    assert t.comments == []
    assert t.acceptance_command == DEFAULT_ACCEPTANCE_COMMAND
    assert t.setup_patch is None
    assert t.solution_patch is None
    ticket = t.ticket()
    assert (ticket.id, ticket.short_id, ticket.url, ticket.labels) == ("t1", "t1", "eval://t1", [])


def test_solution_patch_is_found_when_present(tmp_path):
    d = _task(tmp_path, VALID)
    (d / "solution.patch").write_text("")
    assert load_task(d).solution_patch == d / "solution.patch"


def test_comments_become_ticket_comments(tmp_path):
    toml = VALID + '[[comments]]\nauthor = "ann"\ntext = "hello"\n'
    ticket = load_task(_task(tmp_path, toml)).ticket()
    assert [(c.author, c.text, c.username) for c in ticket.comments] == [("ann", "hello", "ann")]


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        (VALID.replace('title = "T"\n', ""), "title"),
        (VALID.replace("feature", "magic"), "category"),
        (VALID.replace("proceed", "maybe"), "expected"),
        (VALID.replace('id = "t1"', 'id = "other"'), "directory"),
        (VALID + "colour = 1\n", "colour"),
        (VALID + 'comments = ["just text"]\n', "comments"),
        (VALID + '[[comments]]\nauthor = "ann"\n', "comments"),
        (VALID + "acceptance_command = 3\n", "acceptance_command"),
    ],
)
def test_invalid_tasks(tmp_path, toml, message):
    with pytest.raises(TaskError, match=message):
        load_task(_task(tmp_path, toml))


def test_proceed_task_needs_acceptance_tests(tmp_path):
    with pytest.raises(TaskError, match="acceptance"):
        load_task(_task(tmp_path, VALID, acceptance=False))


def test_needs_info_task_needs_no_acceptance_tests(tmp_path):
    toml = VALID.replace("proceed", "needs_info")
    assert load_task(_task(tmp_path, toml, acceptance=False)).expected == "needs_info"


def test_load_tasks_by_id(tmp_path):
    _task(tmp_path, VALID)
    assert [t.id for t in load_tasks(tmp_path)] == ["t1"]
    assert [t.id for t in load_tasks(tmp_path, ["t1"])] == ["t1"]
    with pytest.raises(TaskError, match="unknown task"):
        load_tasks(tmp_path, ["nope"])


# --- the shipped sample repository and tasks ---------------------------------------
NEW_TASKS = {
    "delete-expense": "feature",
    "edit-expense": "feature",
    "date-range-filter": "feature",
    "report-json": "feature",
    "csv-import": "feature",
    "report-total": "feature",
    "fix-float-sums": "bug",
    "fix-invalid-dates": "bug",
    "fix-month-order": "bug",
    "fix-lost-notes": "bug",
    "storage-interface": "refactor",
    "report-argparse": "refactor",
    "document-all-commands": "docs",
    "currency-symbol": "feature",
}
PROCEED_IDS = [t.id for t in load_tasks(TASKS) if t.expected == "proceed"]
PATCHED_IDS = [t.id for t in load_tasks(TASKS) if t.setup_patch]


def test_real_tasks_load():
    tasks = {t.id: t for t in load_tasks(TASKS)}
    assert {"add-category-filter", "fix-month-boundary", "export-somewhere"} <= set(tasks)
    assert tasks["export-somewhere"].expected == "needs_info"
    assert tasks["fix-month-boundary"].setup_patch is not None
    needs_info = {
        "multiple-currencies": "ambiguous",
        "nicer-report": "ambiguous",
        "add-users": "ambiguous",
        "decimals-contradiction": "ambiguous",
        "web-interface": "too_large",
        "bank-sync": "too_large",
        "why-is-report-slow": "not_actionable",
        "meeting-notes": "not_actionable",
    }
    for task_id, category in needs_info.items():
        t = tasks[task_id]
        assert (t.expected, t.category) == ("needs_info", category), task_id
        assert not t.acceptance_dir.exists(), task_id
    for task_id in ("sort-list-by-date", "list-total-line"):
        assert tasks[task_id].expected == "proceed", task_id
        assert tasks[task_id].solution_patch is not None, task_id
    assert len(PROCEED_IDS) >= 16
    for task_id, category in NEW_TASKS.items():
        assert tasks[task_id].category == category, task_id
        assert tasks[task_id].expected == "proceed", task_id
    for task_id, category in NEW_TASKS.items():
        if category == "bug":
            assert tasks[task_id].setup_patch is not None, task_id
    assert tasks["currency-symbol"].comments
    for task_id in PROCEED_IDS:
        assert tasks[task_id].solution_patch is not None, task_id
        assert all(f.suffix == ".py" or f.name == "conftest.py" for f in tasks[task_id].acceptance_dir.iterdir())
        for f in tasks[task_id].acceptance_dir.glob("*.py"):
            assert f.read_text(encoding="utf-8").lstrip().startswith('"""'), f"{f} needs a docstring"


@pytest.fixture
def sample_copy(tmp_path) -> Path:
    dest = tmp_path / "sample"
    shutil.copytree(SAMPLE, dest, ignore=_COPY_IGNORE)
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    return dest


def test_setup_patches_apply(sample_copy):
    for task in load_tasks(TASKS):
        if task.setup_patch:
            r = subprocess.run(["git", "apply", "--check", str(task.setup_patch)], cwd=sample_copy, capture_output=True)
            assert r.returncode == 0, (task.id, r.stderr)


def test_sample_repo_check_passes(sample_copy):
    r = subprocess.run(["make", "check", f"PYTHON={sys.executable}"], cwd=sample_copy, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def _acceptance(repo: Path, task) -> int:
    dest = repo / "tests" / "acceptance"
    shutil.copytree(task.acceptance_dir, dest)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/acceptance"], cwd=repo)
    return r.returncode


def test_month_boundary_acceptance_passes_clean_and_fails_when_bugged(sample_copy):
    task = load_task(TASKS / "fix-month-boundary")
    assert _acceptance(sample_copy, task) == 0
    shutil.rmtree(sample_copy / "tests" / "acceptance")
    patch = TASKS / "fix-month-boundary" / "setup.patch"
    subprocess.run(["git", "apply", str(patch)], cwd=sample_copy, check=True)
    assert _acceptance(sample_copy, task) != 0


def test_planted_bug_passes_the_sample_repos_own_checks(sample_copy):
    patch = TASKS / "fix-month-boundary" / "setup.patch"
    subprocess.run(["git", "apply", str(patch)], cwd=sample_copy, check=True)
    r = subprocess.run(["make", "check", f"PYTHON={sys.executable}"], cwd=sample_copy, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def _git_apply(repo: Path, patch: Path) -> None:
    r = subprocess.run(["git", "apply", str(patch)], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _prepare(repo: Path, task, *, solution: bool = False) -> None:
    if task.setup_patch:
        _git_apply(repo, task.setup_patch)
    if solution:
        _git_apply(repo, task.solution_patch)


def _run_acceptance(repo: Path, task) -> subprocess.CompletedProcess:
    shutil.copytree(task.acceptance_dir, repo / "tests" / "acceptance")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/acceptance"]
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True)


def _make_check(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["make", "check", f"PYTHON={sys.executable}"], cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize("task_id", PROCEED_IDS)
def test_acceptance_fails_on_sample_repo(sample_copy, task_id):
    task = load_task(TASKS / task_id)
    _prepare(sample_copy, task)
    r = _run_acceptance(sample_copy, task)
    assert r.returncode == 1, f"expected failing tests (exit 1), got {r.returncode}\n{r.stdout}{r.stderr}"


@pytest.mark.parametrize("task_id", PROCEED_IDS)
def test_solution_passes(sample_copy, task_id):
    task = load_task(TASKS / task_id)
    _prepare(sample_copy, task, solution=True)
    check = _make_check(sample_copy)
    assert check.returncode == 0, check.stdout + check.stderr
    r = _run_acceptance(sample_copy, task)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("task_id", PATCHED_IDS)
def test_setup_patches_keep_sample_check_green(sample_copy, task_id):
    _git_apply(sample_copy, load_task(TASKS / task_id).setup_patch)
    r = _make_check(sample_copy)
    assert r.returncode == 0, r.stdout + r.stderr
