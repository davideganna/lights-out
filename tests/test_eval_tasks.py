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
    ticket = t.ticket()
    assert (ticket.id, ticket.short_id, ticket.url, ticket.labels) == ("t1", "t1", "eval://t1", [])


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
def test_real_tasks_load():
    tasks = {t.id: t for t in load_tasks(TASKS)}
    assert {"add-category-filter", "fix-month-boundary", "export-somewhere"} <= set(tasks)
    assert tasks["export-somewhere"].expected == "needs_info"
    assert tasks["fix-month-boundary"].setup_patch is not None


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


def _acceptance(repo: Path) -> int:
    dest = repo / "tests" / "acceptance"
    shutil.copytree(TASKS / "fix-month-boundary" / "acceptance", dest)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/acceptance"], cwd=repo)
    return r.returncode


def test_month_boundary_acceptance_passes_clean_and_fails_when_bugged(sample_copy):
    assert _acceptance(sample_copy) == 0
    shutil.rmtree(sample_copy / "tests" / "acceptance")
    patch = TASKS / "fix-month-boundary" / "setup.patch"
    subprocess.run(["git", "apply", str(patch)], cwd=sample_copy, check=True)
    assert _acceptance(sample_copy) != 0


def test_planted_bug_passes_the_sample_repos_own_checks(sample_copy):
    patch = TASKS / "fix-month-boundary" / "setup.patch"
    subprocess.run(["git", "apply", str(patch)], cwd=sample_copy, check=True)
    r = subprocess.run(["make", "check", f"PYTHON={sys.executable}"], cwd=sample_copy, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
