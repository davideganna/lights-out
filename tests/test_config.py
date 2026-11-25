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
