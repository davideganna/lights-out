"""The optional lights-out.yaml and `lights-out config`."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from lights_out.cli import main
from lights_out.core.config import (
    ALERT_EVENT_NAMES,
    DEFAULT_HUMAN_REVIEW_PATHS,
    FILE_SECRET_KEYS,
    Config,
    ConfigError,
    Lists,
    effective_settings,
    valid_file_keys,
)

ROOT = Path(__file__).resolve().parent.parent
SETTING_VARS = [
    "TRELLO_BOARD_ID", "REPO_LABELS", "ALLOWED_MEMBERS", "DEFAULT_REPO", "MAX_DAY_USD", "MAX_FIX_ATTEMPTS",
    "CHECK_COMMAND", "MERGE_WITHOUT_CI", "HUMAN_REVIEW_PATHS", "ALERT_EVENTS", "PLANNER_EFFORT", "CODER_EFFORT",
    "BASE_BRANCH", "GITHUB_REPO", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ALERT_WEBHOOK_URL",
    *[f"LIST_{n.upper()}" for n in Lists().__dict__],
]  # fmt: skip


@pytest.fixture
def env(monkeypatch, tmp_path):
    from lights_out.core.config import _SETTINGS

    for k in [name.upper() for name, _, _ in _SETTINGS] + SETTING_VARS:
        monkeypatch.delenv(k, raising=False)
    for k in ("TRELLO_API_KEY", "TRELLO_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("TRELLO_BOARD_ID", "board")
    monkeypatch.setenv("DEFAULT_REPO", "o/r")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("LIGHTS_OUT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def write(env, tmp_path, content) -> Path:
    path = tmp_path / "conf.yaml"
    path.write_text(content if isinstance(content, str) else yaml.safe_dump(content))
    env.setenv("LIGHTS_OUT_CONFIG", str(path))
    return path


def load() -> Config:
    return Config.from_env(scrub_secrets=False)


def test_value_from_file(env, tmp_path):
    env.delenv("TRELLO_BOARD_ID")
    write(env, tmp_path, {"max_day_usd": 7, "check_command": "just test", "trello_board_id": "b1"})
    cfg = load()
    assert (cfg.max_day_usd, cfg.check_command, cfg.trello_board_id) == (7.0, "just test", "b1")


def test_env_overrides_file_and_default_applies(env, tmp_path):
    write(env, tmp_path, {"max_day_usd": 7.5, "check_command": "x"})
    env.setenv("MAX_DAY_USD", "9")
    cfg = load()
    assert cfg.max_day_usd == 9.0
    assert cfg.check_command == "x"
    assert cfg.base_branch == "main"


def test_empty_env_var_still_wins(env, tmp_path):
    write(env, tmp_path, {"human_review_paths": ["a/**"], "lists": {"triaged": "T"}})
    env.setenv("HUMAN_REVIEW_PATHS", "")
    env.setenv("LIST_TRIAGED", "")
    cfg = load()
    assert cfg.human_review_paths == []
    assert cfg.lists.triaged == ""


def test_lists_and_repos(env, tmp_path):
    write(env, tmp_path, {"lists": {"ready": "Todo", "needs_info": "Ask"}, "repos": {"Web": "acme/web"}})
    env.setenv("LIST_READY", "Queue")
    cfg = load()
    assert cfg.lists.ready == "Queue"
    assert cfg.lists.needs_info == "Ask"
    assert cfg.lists.done == "Done"
    assert cfg.repo_labels == {"web": "acme/web"}


@pytest.mark.parametrize("repos", [{"a": "not-a-repo"}, {"A": "o/r", "a": "o/s"}])
def test_bad_repos(env, tmp_path, repos):
    write(env, tmp_path, {"repos": repos})
    with pytest.raises(ConfigError):
        load()


def test_list_settings(env, tmp_path):
    write(
        env,
        tmp_path,
        {"allowed_members": ["@Alice", "bob", "alice"], "human_review_paths": ["x/**"], "alert_events": ["Failed"]},
    )
    cfg = load()
    assert cfg.allowed_members == ["alice", "bob"]
    assert cfg.human_review_paths == ["x/**"]
    assert cfg.alert_events == ["failed"]


def test_empty_alert_events_means_all(env, tmp_path):
    write(env, tmp_path, {"alert_events": []})
    assert load().alert_events == list(ALERT_EVENT_NAMES)


def test_unknown_alert_event_and_effort(env, tmp_path):
    write(env, tmp_path, {"alert_events": ["nope"]})
    with pytest.raises(ConfigError, match="nope"):
        load()
    write(env, tmp_path, {"coder_effort": "extreme"})
    with pytest.raises(ConfigError, match="extreme"):
        load()


@pytest.mark.parametrize("key", FILE_SECRET_KEYS)
def test_secret_in_file(env, tmp_path, key):
    write(env, tmp_path, {key.upper(): "s3cret"})
    with pytest.raises(ConfigError, match=r"\.env") as e:
        load()
    assert "s3cret" not in str(e.value)


def test_unknown_keys_list_valid_keys(env, tmp_path):
    write(env, tmp_path, {"max_dya_usd": 1})
    with pytest.raises(ConfigError) as e:
        load()
    assert "max_dya_usd" in str(e.value) and "max_day_usd" in str(e.value)
    write(env, tmp_path, {"lists": {"redy": "x"}})
    with pytest.raises(ConfigError) as e:
        load()
    assert "redy" in str(e.value) and "ready" in str(e.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_day_usd", "lots"),
        ("max_day_usd", True),
        ("max_fix_attempts", True),
        ("max_fix_attempts", 1.5),
        ("allowed_members", "alice"),
        ("human_review_paths", [1]),
        ("merge_without_ci", "yes"),
        ("check_command", 5),
        ("repos", ["a"]),
    ],
)
def test_wrong_types_name_the_key(env, tmp_path, key, value):
    write(env, tmp_path, {key: value})
    with pytest.raises(ConfigError, match=key):
        load()


def test_wrong_type_in_lists(env, tmp_path):
    write(env, tmp_path, {"lists": {"ready": 5}})
    with pytest.raises(ConfigError, match=r"lists\.ready"):
        load()


def test_empty_file_ok(env, tmp_path):
    write(env, tmp_path, "")
    assert load().max_day_usd == 50.0


@pytest.mark.parametrize("content", ["a: [", "- a\n- b\n"])
def test_bad_yaml_and_non_mapping(env, tmp_path, content):
    path = write(env, tmp_path, content)
    with pytest.raises(ConfigError, match=str(path)):
        load()


def test_missing_default_file_ok_explicit_missing_is_error(env, tmp_path):
    assert load().max_day_usd == 50.0
    env.setenv("LIGHTS_OUT_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="nope.yaml"):
        load()


def test_default_file_in_working_directory(env, tmp_path):
    (tmp_path / "lights-out.yaml").write_text("base_branch: dev\n")
    assert load().base_branch == "dev"


def test_config_command_sources_and_hidden_secrets(env, tmp_path, capsys):
    for k in ("TRELLO_API_KEY", "TRELLO_TOKEN", "GITHUB_TOKEN", "DEFAULT_REPO"):
        env.delenv(k)
    write(env, tmp_path, {"check_command": "just test", "repos": {"web": "a/b"}, "lists": {"ready": "Todo"}})
    env.setenv("MAX_DAY_USD", "3")
    env.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "max_day_usd = 3.0  [env]" in out
    assert "check_command = just test  [file]" in out
    assert "repos = web=a/b  [file]" in out
    assert "lists.ready = Todo  [file]" in out
    assert "base_branch = main  [default]" in out
    assert "anthropic_api_key = set  [env]" in out
    assert "github_token = not set  [default]" in out
    assert "sk-very-secret" not in out
    assert not (tmp_path / "data").exists()


def test_config_command_bad_file_exits_2(env, tmp_path, capsys):
    write(env, tmp_path, {"bogus": 1})
    assert main(["config"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_effective_settings_covers_every_field(env):
    keys = {k for k, _, _ in effective_settings({})}
    expected = {f.name for f in fields(Config)} - {"repo_labels", "lists"} | {"repos"}
    expected = {k for k in expected if k not in FILE_SECRET_KEYS}
    assert expected <= keys


def test_example_file(env):
    path = ROOT / "lights-out.example.yaml"
    env.setenv("LIGHTS_OUT_CONFIG", str(path))
    data = yaml.safe_load(path.read_text())
    flat = {k for k in data if k != "lists"} | {f"lists.{k}" for k in data["lists"]}
    expected = set(valid_file_keys()) - {"lists"} | {f"lists.{n}" for n in Lists().__dict__}
    assert flat == expected
    cfg = load()
    default = Config(trello_api_key="x", trello_token="x", trello_board_id="", github_token="x")
    for f in fields(Config):
        if f.name in FILE_SECRET_KEYS or f.name in (
            "repo_labels",
            "trello_board_id",
            "default_repo",
            "data_dir",
            "agent_user",
        ):
            continue
        assert getattr(cfg, f.name) == getattr(default, f.name), f.name
    assert cfg.human_review_paths == list(DEFAULT_HUMAN_REVIEW_PATHS)
    assert cfg.repo_labels == {"my-app": "your-org/my-app"}
