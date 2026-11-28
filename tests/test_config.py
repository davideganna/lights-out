"""Effort settings in the configuration."""

from __future__ import annotations

import pytest

from lights_out.core.config import EFFORT_LEVELS, Config, ConfigError

VARS = ["PLANNER_EFFORT", "CODER_EFFORT", "REVIEWER_EFFORT"]
FIELDS = {"PLANNER_EFFORT": "planner_effort", "CODER_EFFORT": "coder_effort", "REVIEWER_EFFORT": "reviewer_effort"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in ("TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID", "GITHUB_TOKEN"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("DEFAULT_REPO", "o/r")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LIGHTS_OUT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    for k in VARS + ["PLANNER_MODEL", "CODER_MODEL", "REVIEWER_MODEL"]:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def load() -> Config:
    return Config.from_env(scrub_secrets=False)


def test_defaults(env):
    cfg = load()
    assert (cfg.planner_effort, cfg.coder_effort, cfg.reviewer_effort) == ("high", "", "high")
    assert cfg.run_settings() == {
        "planner": "claude-opus-5-5/high",
        "coder": "claude-sonnet-5-5/default",
        "reviewer": "claude-opus-5-5/high",
    }


def test_dataclass_defaults_match_env_defaults(env):
    direct = Config(trello_api_key="k", trello_token="t", trello_board_id="b", github_token="g")
    assert direct.run_settings() == load().run_settings()


@pytest.mark.parametrize("name", VARS)
@pytest.mark.parametrize("value", EFFORT_LEVELS)
def test_each_allowed_value(env, name, value):
    env.setenv(name, value)
    assert getattr(load(), FIELDS[name]) == value


@pytest.mark.parametrize("name", VARS)
def test_empty_means_model_default(env, name):
    env.setenv(name, "")
    cfg = load()
    assert getattr(cfg, FIELDS[name]) == ""
    assert "/default" in "".join(cfg.run_settings().values())


@pytest.mark.parametrize("name", VARS)
@pytest.mark.parametrize("value", ["extreme", "HIGH"])
def test_invalid_value_is_a_config_error(env, name, value):
    env.setenv(name, value)
    with pytest.raises(ConfigError) as e:
        load()
    assert name in str(e.value) and "low, medium, high, xhigh, max" in str(e.value)


def test_whitespace_is_stripped(env):
    env.setenv("CODER_EFFORT", " max ")
    assert load().coder_effort == "max"


def test_max_pr_revisions_default_and_override(env, monkeypatch):
    assert load().max_pr_revisions == 3
    monkeypatch.setenv("MAX_PR_REVISIONS", "5")
    assert load().max_pr_revisions == 5


# --- GitHub App settings ---------------------------------------------------------------------------


@pytest.fixture
def app_env(env, tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("pem")
    env.delenv("GITHUB_TOKEN")
    for k in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_FILE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        env.delenv(k, raising=False)
    env.setenv("GITHUB_APP_ID", "123")
    env.setenv("GITHUB_APP_PRIVATE_KEY_FILE", str(key))
    return env


def test_app_alone_is_ok(app_env):
    cfg = load()
    assert cfg.uses_github_app and cfg.github_app_id == "123" and cfg.github_token == ""


def test_only_app_id_is_an_error(app_env):
    app_env.delenv("GITHUB_APP_PRIVATE_KEY_FILE")
    with pytest.raises(ConfigError, match="GITHUB_APP_PRIVATE_KEY_FILE"):
        load()


def test_only_key_file_is_an_error(app_env):
    app_env.delenv("GITHUB_APP_ID")
    with pytest.raises(ConfigError, match="GITHUB_APP_ID"):
        load()


def test_app_and_token_is_an_error(app_env):
    app_env.setenv("GITHUB_TOKEN", "x")
    with pytest.raises(ConfigError, match="pick one"):
        load()


def test_missing_key_file_is_an_error(app_env, tmp_path):
    app_env.setenv("GITHUB_APP_PRIVATE_KEY_FILE", str(tmp_path / "nope.pem"))
    with pytest.raises(ConfigError, match="existing file"):
        load()


def test_no_app_and_no_token_is_an_error(app_env):
    app_env.delenv("GITHUB_APP_ID")
    app_env.delenv("GITHUB_APP_PRIVATE_KEY_FILE")
    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        load()


def test_personal_token_path_unchanged(env):
    env.delenv("GITHUB_APP_ID", raising=False)
    env.delenv("GITHUB_APP_PRIVATE_KEY_FILE", raising=False)
    cfg = load()
    assert not cfg.uses_github_app and cfg.github_token == "x"


def test_app_variables_are_scrubbed(app_env):
    import os

    Config.from_env()
    assert "GITHUB_APP_ID" not in os.environ
    assert "GITHUB_APP_PRIVATE_KEY_FILE" not in os.environ


def test_git_author_configured(app_env):
    assert load().git_author_configured is False
    app_env.setenv("GIT_AUTHOR_NAME", "Me")
    assert load().git_author_configured is True
