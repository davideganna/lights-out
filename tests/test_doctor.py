"""doctor command and the Claude credential check; no network."""

from __future__ import annotations

import pytest
from claude_agent_sdk import ResultMessage

from lights_out import cli
from lights_out.agents import agents
from lights_out.agents.agents import AgentError, check_claude
from lights_out.core.config import Config
from lights_out.integrations.github import BranchProtection, GitHubError
from lights_out.integrations.github_auth import GitHubAppTokenProvider, StaticTokenProvider


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


PROTECTION: dict = {}


def _full(**kw) -> BranchProtection:
    base = dict(protected=True, classic=True, required_checks=["check", "docker"], enforce_admins=True)
    base.update(kw)
    return BranchProtection(**base)


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

        def branch_protection(self, branch):
            PROTECTION.setdefault("branches", []).append(branch)
            result = PROTECTION["value"]
            if isinstance(result, Exception):
                raise result
            return result

    PROTECTION["value"] = _full()
    PROTECTION["branches"] = []
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


def _doctor(monkeypatch, cfg, capsys, result):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    PROTECTION["value"] = result
    code = cli.cmd_doctor(cfg)
    return code, capsys.readouterr().out


def test_doctor_branch_protection_missing(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, BranchProtection())
    assert code == 1 and "  branch protection: MISSING on main" in out


def test_doctor_branch_protection_unreadable_warns(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, BranchProtection(readable=False))
    assert code == 0 and "branch protection: WARNING: could not read protection for main" in out
    assert "check it by hand" in out


def test_doctor_branch_protection_no_checks(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, _full(required_checks=[]))
    assert code == 1 and "no required status checks on main" in out


def test_doctor_branch_protection_force_pushes(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, _full(force_pushes_allowed=True))
    assert code == 1 and "force pushes allowed on main" in out


def test_doctor_branch_protection_both_problems(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, _full(required_checks=[], force_pushes_allowed=True))
    assert code == 1 and "no required status checks" in out and "force pushes allowed" in out


def test_doctor_branch_protection_ok(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, _full())
    assert code == 0 and "  branch protection: OK (required checks: check, docker)" in out
    assert "WARNING: admins" not in out


def test_doctor_branch_protection_admins_not_included_warns(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, _full(enforce_admins=False))
    assert code == 0 and "OK (required checks: check, docker)" in out
    assert "WARNING: admins are not included (enforce_admins off)" in out


def test_doctor_branch_protection_rulesets_only(monkeypatch, cfg, capsys):
    bp = BranchProtection(protected=True, rulesets=True, required_checks=["a", "b"])
    code, out = _doctor(monkeypatch, cfg, capsys, bp)
    assert code == 0 and "OK (rulesets; required checks: a, b)" in out
    assert "bypass actors can't be read" in out


def test_doctor_branch_protection_rulesets_without_checks(monkeypatch, cfg, capsys):
    bp = BranchProtection(protected=True, rulesets=True)
    code, out = _doctor(monkeypatch, cfg, capsys, bp)
    assert code == 1 and "no required status checks" in out


def test_doctor_branch_protection_error(monkeypatch, cfg, capsys):
    code, out = _doctor(monkeypatch, cfg, capsys, RuntimeError("boom"))
    assert code == 1 and "branch protection: FAILED: boom" in out


def test_warn_if_open_board(cfg, caplog):
    cli._warn_if_open_board(cfg)
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    caplog.clear()
    cfg.allowed_members = ["alice"]
    cli._warn_if_open_board(cfg)
    assert caplog.records == []


def test_doctor_reports_alerts_without_the_url(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    cfg.alert_webhook_url = "https://hooks.example/SECRET"
    assert cli.cmd_doctor(cfg) == 0
    out = capsys.readouterr().out
    assert "Alerts: webhook set (events: failed" in out
    assert "SECRET" not in out and "hooks.example" not in out


def test_doctor_reports_alerts_off(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    assert cli.cmd_doctor(cfg) == 0
    assert "Alerts: off" in capsys.readouterr().out


class _PostAlerter:
    def __init__(self, ok):
        self.ok, self.posted = ok, []

    def post(self, text):
        self.posted.append(text)
        return self.ok


def test_test_alert_success_failure_and_missing_url(cfg, capsys):
    assert cli.cmd_test_alert(cfg, _PostAlerter(True)) == 1
    assert "not set" in capsys.readouterr().out
    cfg.alert_webhook_url = "https://hooks.example/SECRET"
    fake = _PostAlerter(True)
    assert cli.cmd_test_alert(cfg, fake) == 0
    assert fake.posted == ["lights-out: test alert"] and "Test alert sent." in capsys.readouterr().out
    assert cli.cmd_test_alert(cfg, _PostAlerter(False)) == 1
    assert "FAILED" in capsys.readouterr().out


# --- GitHub auth modes -------------------------------------------------------------------------


class FakeAppProvider(GitHubAppTokenProvider):
    def __init__(self, installed=("o/app",), perms=None, slug_error=None):
        self.app_id = "77"
        self._installed = set(installed)
        self._perms = perms if perms is not None else {"contents": "write"}
        self._slug_error = slug_error
        self.identity_calls = 0

    def slug(self):
        if self._slug_error:
            raise GitHubError(self._slug_error)
        return "my-bot"

    def is_installed(self, repo):
        return repo in self._installed

    def permissions(self, repo):
        return self._perms

    def token_for(self, repo):
        return "ghs_x"

    def bot_identity(self):
        self.identity_calls += 1
        return "my-bot[bot]", "1+my-bot[bot]@users.noreply.github.com"


def test_doctor_personal_token(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    assert cli.cmd_doctor(cfg, StaticTokenProvider("t")) == 0
    assert "GitHub auth: personal token" in capsys.readouterr().out


def test_doctor_app_installed_runs_branch_protection(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    assert cli.cmd_doctor(cfg, FakeAppProvider()) == 0
    out = capsys.readouterr().out
    assert "GitHub auth: App my-bot (id 77)" in out
    assert "push=True" in out and "branch protection: OK" in out


def test_doctor_app_not_installed(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    assert cli.cmd_doctor(cfg, FakeAppProvider(installed=())) == 1
    out = capsys.readouterr().out
    assert "o/app App not installed" in out and "branch protection" not in out


def test_doctor_app_without_contents_write(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    assert cli.cmd_doctor(cfg, FakeAppProvider(perms={"contents": "read"})) == 1
    assert "App cannot push to this repository (Contents: write missing)" in capsys.readouterr().out


def test_doctor_app_slug_failure(monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    assert cli.cmd_doctor(cfg, FakeAppProvider(slug_error="401")) == 1
    assert "GitHub auth: App FAILED: 401" in capsys.readouterr().out


def test_doctor_warns_about_group_readable_key(monkeypatch, cfg, tmp_path, capsys):
    monkeypatch.setattr(cli, "check_claude", lambda c: "m")
    key = tmp_path / "k.pem"
    key.write_text("x")
    key.chmod(0o640)
    cfg.github_app_private_key_file = str(key)
    assert cli.cmd_doctor(cfg, FakeAppProvider()) == 0
    assert "group or others" in capsys.readouterr().out
    key.chmod(0o600)
    cli.cmd_doctor(cfg, FakeAppProvider())
    assert "group or others" not in capsys.readouterr().out


def test_bot_author_applied_when_not_configured(cfg):
    p = FakeAppProvider()
    cli.apply_bot_author(cfg, p)
    assert (cfg.git_author_name, cfg.git_author_email) == ("my-bot[bot]", "1+my-bot[bot]@users.noreply.github.com")


def test_bot_author_kept_when_configured(cfg):
    cfg.git_author_configured = True
    p = FakeAppProvider()
    cli.apply_bot_author(cfg, p)
    assert cfg.git_author_name == "lights-out" and p.identity_calls == 0


def test_bot_author_not_used_with_personal_token(cfg):
    cli.apply_bot_author(cfg, StaticTokenProvider("t"))
    assert cfg.git_author_name == "lights-out"


def test_bot_author_failure_returns_false(cfg, capsys):
    class Broken(FakeAppProvider):
        def bot_identity(self):
            raise GitHubError("boom")

    assert cli._set_bot_author(cfg, Broken()) is False
    assert "GitHub App error: boom" in capsys.readouterr().err


def test_bot_author_malformed_response_returns_false(cfg, capsys):
    class Malformed(FakeAppProvider):
        def bot_identity(self):
            return {}["id"]

    assert cli._set_bot_author(cfg, Malformed()) is False
    assert "GitHub App error" in capsys.readouterr().err


def test_doctor_checks_the_repositorys_own_base_branch(cfg, capsys):
    cfg.repo_overrides = {"o/app": {"base_branch": "develop"}}
    PROTECTION["value"] = _full(protected=False)
    assert cli.cmd_doctor(cfg) == 1
    assert PROTECTION["branches"] == ["develop"]
    assert "develop" in capsys.readouterr().out
