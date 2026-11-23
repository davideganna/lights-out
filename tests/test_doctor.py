"""doctor command and the Claude credential check; no network."""

from __future__ import annotations

import pytest
from claude_agent_sdk import ResultMessage

from lights_out import cli
from lights_out.agents import agents
from lights_out.agents.agents import AgentError, check_claude
from lights_out.core.config import Config


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        trello_api_key="k",
        trello_token="t",
        trello_board_id="b",
        github_token="",
        repo_labels={"app": "o/app"},
        data_dir=tmp_path / "data",
    )


@pytest.fixture(autouse=True)
def other_checks(monkeypatch, cfg):
    class FakeTrello:
        def __init__(self, *a):
            pass

        def list_ids(self, refresh=False):
            return {n: n for n in cfg.lists.all()}

    class FakeGitHub:
        def __init__(self, *a):
            pass

        def _req(self, method, path):
            return {"full_name": "o/app", "permissions": {"push": True}, "default_branch": "main"}

    monkeypatch.setattr(cli, "TrelloClient", FakeTrello)
    monkeypatch.setattr(cli, "GitHubClient", FakeGitHub)


def _result(is_error=False, **kw) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1, session_id="s", **kw
    )


def test_check_claude_without_credentials_makes_no_request(monkeypatch, cfg):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    calls = []

    async def fake_query(**kw):
        calls.append(kw)
        yield _result()

    monkeypatch.setattr(agents, "query", fake_query)
    with pytest.raises(AgentError) as ei:
        check_claude(cfg)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(ei.value) and "ANTHROPIC_API_KEY" in str(ei.value)
    assert calls == []


def test_check_claude_live_success_uses_planner_model_and_one_turn(monkeypatch, cfg):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    captured = {}

    async def fake_query(*, prompt, options):
        captured["options"] = options
        yield _result()

    monkeypatch.setattr(agents, "query", fake_query)
    assert check_claude(cfg) == cfg.planner_model
    opts = captured["options"]
    assert opts.max_turns == 1
    assert opts.tools == []
    assert opts.model == cfg.planner_model


def test_check_claude_error_never_contains_token(monkeypatch, cfg):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-SECRET123")

    async def fake_query(*, prompt, options):
        raise RuntimeError("401 for tok-SECRET123")
        yield  # pragma: no cover

    monkeypatch.setattr(agents, "query", fake_query)
    with pytest.raises(AgentError) as ei:
        check_claude(cfg)
    assert "SECRET123" not in str(ei.value)
    assert "401" in str(ei.value)


def test_check_claude_error_result_is_redacted(monkeypatch, cfg):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-SECRET456")

    async def fake_query(*, prompt, options):
        yield _result(is_error=True, errors=["bad key sk-SECRET456"])

    monkeypatch.setattr(agents, "query", fake_query)
    with pytest.raises(AgentError) as ei:
        check_claude(cfg)
    assert "SECRET456" not in str(ei.value)
    assert "bad key" in str(ei.value)


def test_doctor_claude_ok(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "claude-opus-5-5")
    assert cli.cmd_doctor(cfg) == 0
    out = capsys.readouterr().out
    assert "Claude: OK (model claude-opus-5-5)" in out
    assert out.strip().endswith("OK")


def test_doctor_claude_failed(monkeypatch, cfg, capsys):
    def boom(c):
        raise AgentError("invalid x-api-key")

    monkeypatch.setattr(cli, "check_claude", boom)
    assert cli.cmd_doctor(cfg) == 1
    out = capsys.readouterr().out
    assert "Claude: FAILED: invalid x-api-key" in out
    assert "Problems found" in out


def test_doctor_lists_allowed_members(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    cfg.allowed_members = ["alice", "bob"]
    assert cli.cmd_doctor(cfg) == 0
    assert "Allowed members: @alice, @bob" in capsys.readouterr().out


def test_doctor_warns_on_empty_allow_list_without_failing(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    assert cli.cmd_doctor(cfg) == 0
    assert "WARNING: ALLOWED_MEMBERS is empty" in capsys.readouterr().out


def test_warn_if_open_board(cfg, caplog):
    cli._warn_if_open_board(cfg)
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    caplog.clear()
    cfg.allowed_members = ["alice"]
    cli._warn_if_open_board(cfg)
    assert caplog.records == []
