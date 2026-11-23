"""Merge policy and its configuration (pure, no git)."""

from __future__ import annotations

import pytest

from lights_out.core.config import DEFAULT_HUMAN_REVIEW_PATHS, Config, parse_patterns
from lights_out.orchestration.policy import merge_policy, pattern_matches
from lights_out.orchestration.workspace import FileChange


def make_cfg(**kw) -> Config:
    return Config(trello_api_key="k", trello_token="t", trello_board_id="b", github_token="g", **kw)


def fc(path, added=1, deleted=0) -> FileChange:
    return FileChange(path, added, deleted)


RISKY = [
    ".github/workflows/ci.yml",
    "Dockerfile",
    "Dockerfile.test",
    "docker/entry.sh",
    "Makefile",
    "pyproject.toml",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "app/db/migrations/0001.py",
    "migrations/0001.sql",
    "src/auth/views.py",
    "auth/x.py",
    "src/lights_out/oauth_client.py",
    "auth.py",
]


@pytest.mark.parametrize("path", RISKY)
def test_default_patterns_match_risky_files(path):
    reasons = merge_policy([fc(path)], make_cfg())
    assert len(reasons) == 1
    assert reasons[0].startswith(f"touches {path} (matches ")
    pattern = reasons[0].split("(matches ")[1].rstrip(")")
    assert pattern in DEFAULT_HUMAN_REVIEW_PATHS


@pytest.mark.parametrize("path", ["README.md", "src/app/main.py", "docs/Makefile.md", "src/Makefile", "authors.txt"])
def test_non_matching_files_pass(path):
    assert merge_policy([fc(path)], make_cfg()) == []


def test_glob_semantics():
    assert not pattern_matches("*.py", "a/b.py")
    assert pattern_matches("*.py", "b.py")
    assert pattern_matches("**/x/**", "x/a")
    assert pattern_matches("**/x/**", "p/q/x/a/b")
    assert pattern_matches("a/**", "a/b/c")
    assert pattern_matches("**/*.py", "m.py")
    assert pattern_matches("a?c", "abc") and not pattern_matches("a?c", "a/c")
    assert not pattern_matches("a.b", "axb")


def test_size_limit():
    cfg = make_cfg()
    assert merge_policy([fc("a.txt", 300, 100)], cfg) == []
    assert merge_policy([fc("a.txt", 300, 101)], cfg) == ["401 changed lines (limit 400)"]


def test_size_limit_can_be_disabled():
    assert merge_policy([fc("a.txt", 10000)], make_cfg(max_auto_merge_lines=0)) == []


def test_empty_paths_disable_path_holds_but_not_size():
    cfg = make_cfg(human_review_paths=[])
    assert merge_policy([fc(".github/workflows/ci.yml")], cfg) == []
    assert merge_policy([fc(".github/workflows/ci.yml", 500)], cfg) == ["500 changed lines (limit 400)"]


def test_several_reasons_in_order():
    changes = [fc("Makefile", 200), fc("README.md", 10), fc("src/auth/a.py", 200)]
    assert merge_policy(changes, make_cfg()) == [
        "touches Makefile (matches Makefile)",
        "touches src/auth/a.py (matches **/auth/**)",
        "410 changed lines (limit 400)",
    ]


def test_parse_patterns():
    assert parse_patterns(" a , ,b ") == ["a", "b"]
    assert parse_patterns("") == []


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_BOARD_ID": "b",
        "GITHUB_TOKEN": "g",
        "DEFAULT_REPO": "o/r",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("HUMAN_REVIEW_PATHS", raising=False)
    monkeypatch.delenv("MAX_AUTO_MERGE_LINES", raising=False)
    return monkeypatch


def test_config_defaults(env):
    cfg = Config.from_env(scrub_secrets=False)
    assert cfg.human_review_paths == list(DEFAULT_HUMAN_REVIEW_PATHS)
    assert cfg.max_auto_merge_lines == 400


def test_config_empty_paths_and_zero_limit(env):
    env.setenv("HUMAN_REVIEW_PATHS", "")
    env.setenv("MAX_AUTO_MERGE_LINES", "0")
    cfg = Config.from_env(scrub_secrets=False)
    assert cfg.human_review_paths == []
    assert cfg.max_auto_merge_lines == 0
