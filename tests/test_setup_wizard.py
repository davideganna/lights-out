"""`lights-out init`: scripted answers and fake services; no network."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest

from lights_out import cli
from lights_out.core.config import effective_settings
from lights_out.integrations.github import GitHubClient, GitHubError
from lights_out.integrations.trello import TrelloClient
from lights_out.setup_wizard import Services, SetupError, mask, run_wizard

TRELLO_KEY = "trellokey-0123456789abcdef"
TRELLO_TOKEN = "trellotoken-0123456789abcdef0123"
GH_TOKEN = "github_pat_0123456789abcdefghij"
CLAUDE_TOKEN = "sk-ant-oat01-0123456789abcdefghij"
SECRETS = [TRELLO_KEY, TRELLO_TOKEN, GH_TOKEN, CLAUDE_TOKEN]


class Script:
    """Answers in order; fails the test if the wizard asks more than scripted."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def _next(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"script ran out at: {prompt}")
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a

    def ask(self, prompt, default=""):
        return self._next(prompt)

    def ask_secret(self, prompt):
        return self._next(prompt)


class Fake:
    def __init__(self, **kw):
        self.calls: list[tuple] = []
        self.trello_fail = 0
        self.github_fail = 0
        self.push_fail = 0
        self.claude_fail = 0
        self.empty = set()
        self.error_text = "bad credentials"
        self.__dict__.update(kw)

    def services(self) -> Services:
        return Services(
            trello_me=self.trello_me,
            trello_boards=lambda k, t: [("b1", "First"), ("b2", "Second")],
            github_user=self.github_user,
            github_repo=self.github_repo,
            github_is_empty=lambda t, r: r in self.empty,
            claude=self.claude,
            setup_board=lambda cfg: self.calls.append(("setup_board", cfg)) or (["Queued"], ["web"]),
            bootstrap=lambda cfg, label, repo: self.calls.append(("bootstrap", label, repo)) or True,
        )

    def trello_me(self, key, token):
        self.calls.append(("trello_me", key))
        if self.trello_fail:
            self.trello_fail -= 1
            raise SetupError(f"401 {self.error_text} {token}")
        return "Alice"

    def github_user(self, token):
        self.calls.append(("github_user", token))
        if self.github_fail:
            self.github_fail -= 1
            raise SetupError(f"401 {self.error_text} {token}")
        return "alice"

    def github_repo(self, token, repo):
        self.calls.append(("github_repo", repo))
        if self.push_fail:
            self.push_fail -= 1
            return repo, False
        return repo, True

    def claude(self, var, value):
        self.calls.append(("claude", var))
        if self.claude_fail:
            self.claude_fail -= 1
            raise SetupError(f"invalid {value}")
        return "claude-model"


HAPPY = [
    TRELLO_KEY,
    TRELLO_TOKEN,
    "2",  # board
    GH_TOKEN,
    "acme/Web",
    "",  # label default
    "acme/api",
    "backend",
    "",  # end of repos
    "",  # oauth
    CLAUDE_TOKEN,
    "",  # members default
    "",  # budget default
    "",  # check default
    "y",  # setup-board
]


def run(tmp_path, fake=None, answers=None, force=False, extra=()):
    fake = fake or Fake()
    # Secrets (getpass) and plain answers share one queue, in the order asked.
    script = Script(list(HAPPY if answers is None else answers) + list(extra))
    lines: list[str] = []
    rc = run_wizard(tmp_path, force, script.ask, script.ask_secret, fake.services(), lines.append)
    return rc, fake, script, "\n".join(lines)


def test_mask():
    assert mask("abcdefghijklmnop") == "abcd…mnop"
    assert mask("short") == "…"
    assert mask("") == "…"
    assert mask("a" * 15) == "…"


def test_happy_path(tmp_path):
    rc, fake, script, output = run(tmp_path)
    assert rc == 0 and not script.answers
    env = (tmp_path / ".env").read_text().splitlines()
    assert env == [
        f"TRELLO_API_KEY={TRELLO_KEY}",
        f"TRELLO_TOKEN={TRELLO_TOKEN}",
        f"GITHUB_TOKEN={GH_TOKEN}",
        f"CLAUDE_CODE_OAUTH_TOKEN={CLAUDE_TOKEN}",
    ]
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    rows = {k: (v, src) for k, v, src in effective_settings({"LIGHTS_OUT_CONFIG": str(tmp_path / "lights-out.yaml")})}
    assert rows["trello_board_id"] == ("b2", "file")
    assert rows["repos"] == ("web=acme/Web,backend=acme/api", "file")
    assert rows["allowed_members"] == ("alice", "file")
    assert rows["max_day_usd"] == ("25.0", "file")
    assert rows["check_command"] == ("make check", "file")
    assert "docker compose up -d" in output and "doctor" in output
    assert [c[0] for c in fake.calls].count("claude") == 1


def test_api_key_variable(tmp_path):
    answers = list(HAPPY)
    answers[9] = "api-key"
    rc, _, _, _ = run(tmp_path, answers=answers)
    assert rc == 0
    assert f"ANTHROPIC_API_KEY={CLAUDE_TOKEN}" in (tmp_path / ".env").read_text()


def test_bad_trello_credentials_asked_again(tmp_path):
    fake = Fake(trello_fail=1)
    answers = [TRELLO_KEY, TRELLO_TOKEN] + HAPPY
    rc, fake, _, output = run(tmp_path, fake, answers)
    assert rc == 0
    assert [c for c in fake.calls if c[0] == "trello_me"].__len__() == 2
    assert "Trello check failed" in output and "try again" in output


def test_bad_github_token_asked_again(tmp_path):
    answers = HAPPY[:3] + [GH_TOKEN] + HAPPY[3:]
    rc, fake, _, output = run(tmp_path, Fake(github_fail=1), answers)
    assert rc == 0 and [c for c in fake.calls if c[0] == "github_user"].__len__() == 2
    assert "GitHub check failed" in output


def test_no_push_access_asked_again(tmp_path):
    answers = HAPPY[:4] + ["acme/Web", ""] + HAPPY[4:]
    rc, fake, _, output = run(tmp_path, Fake(push_fail=1), answers)
    assert rc == 0
    assert [c for c in fake.calls if c == ("github_repo", "acme/Web")].__len__() == 2
    assert "cannot push" in output


def test_bad_claude_credential_asked_again(tmp_path):
    answers = HAPPY[:10] + [CLAUDE_TOKEN] + HAPPY[10:]
    rc, fake, _, output = run(tmp_path, Fake(claude_fail=1), answers)
    assert rc == 0 and [c for c in fake.calls if c[0] == "claude"].__len__() == 2
    assert "Claude check failed" in output


def test_invalid_board_number_asked_again(tmp_path):
    answers = HAPPY[:2] + ["9", "x", "1"] + HAPPY[3:]
    rc, _, _, output = run(tmp_path, answers=answers)
    assert rc == 0 and "Enter a number from 1 to 2" in output
    assert "b1" in (tmp_path / "lights-out.yaml").read_text()


def test_repo_and_budget_validation(tmp_path):
    answers = (
        HAPPY[:4]
        + ["", "notarepo", "acme/web", "", "acme/web", "acme/other", "web", "acme/other", "other", ""]
        + HAPPY[9:12]
        + ["abc", "0", "-3", "12.5", "pytest"]
        + HAPPY[14:]
    )
    rc, _, script, output = run(tmp_path, answers=answers)
    assert rc == 0, output
    assert "at least one repository" in output
    assert "owner/repo" in output
    assert "already in the list" in output
    assert "must be unique" in output
    assert "above 0" in output
    text = (tmp_path / "lights-out.yaml").read_text()
    assert "max_day_usd: 12.5" in text and "check_command: pytest" in text


def test_empty_secret_asked_again(tmp_path):
    rc, _, _, output = run(tmp_path, answers=["", "has space"] + HAPPY)
    assert rc == 0 and "must not be empty" in output


@pytest.mark.parametrize("name", [".env", "lights-out.yaml"])
def test_existing_file_refuses_without_force(tmp_path, name):
    (tmp_path / name).write_text("old")
    rc, _, script, output = run(tmp_path, answers=[])
    assert rc == 1 and name in output and "--force" in output
    assert (tmp_path / name).read_text() == "old"
    assert script.prompts == []


def test_force_overwrites_and_fixes_mode(tmp_path):
    env = tmp_path / ".env"
    env.write_text("old")
    env.chmod(0o644)
    (tmp_path / "lights-out.yaml").write_text("old")
    rc, _, _, _ = run(tmp_path, force=True)
    assert rc == 0
    assert TRELLO_KEY in env.read_text()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "trello_board_id" in (tmp_path / "lights-out.yaml").read_text()


def test_setup_board_and_bootstrap_called(tmp_path):
    fake = Fake(empty={"acme/api"})
    rc, fake, _, output = run(tmp_path, fake, extra=["y"])
    assert rc == 0
    setup = [c for c in fake.calls if c[0] == "setup_board"]
    assert len(setup) == 1
    cfg = setup[0][1]
    assert cfg.trello_board_id == "b2"
    assert cfg.repo_labels == {"web": "acme/Web", "backend": "acme/api"}
    assert [c for c in fake.calls if c[0] == "bootstrap"] == [("bootstrap", "backend", "acme/api")]
    assert "Created lists" in output


def test_declined_offers_are_not_called(tmp_path):
    fake = Fake(empty={"acme/api"})
    answers = HAPPY[:-1] + ["n", "n"]
    rc, fake, _, _ = run(tmp_path, fake, answers)
    assert rc == 0
    assert not [c for c in fake.calls if c[0] in ("setup_board", "bootstrap")]
    assert (tmp_path / ".env").exists()


@pytest.mark.parametrize("cut", range(0, 14))
def test_ctrl_c_writes_nothing(tmp_path, cut):
    answers = HAPPY[:cut] + [KeyboardInterrupt()]
    rc, _, _, output = run(tmp_path, answers=answers)
    assert rc == 130 and "nothing written" in output
    assert list(tmp_path.iterdir()) == []


def test_eof_writes_nothing(tmp_path):
    rc, _, _, _ = run(tmp_path, answers=HAPPY[:3] + [EOFError()])
    assert rc == 130 and list(tmp_path.iterdir()) == []


def test_ctrl_c_after_writing_keeps_complete_files(tmp_path):
    rc, _, _, output = run(tmp_path, answers=HAPPY[:-1] + [KeyboardInterrupt()])
    assert rc == 130 and "complete" in output
    assert (tmp_path / ".env").exists() and (tmp_path / "lights-out.yaml").exists()


def test_no_secret_in_output(tmp_path):
    fake = Fake(trello_fail=1, github_fail=1, claude_fail=1, empty={"acme/api"})
    answers = [TRELLO_KEY, TRELLO_TOKEN] + HAPPY[:3] + [GH_TOKEN] + HAPPY[3:10] + [CLAUDE_TOKEN] + HAPPY[10:] + ["y"]
    rc, _, _, output = run(tmp_path, fake, answers)
    assert rc == 0
    for secret in SECRETS:
        assert secret not in output
    assert mask(GH_TOKEN) in output


def test_secret_in_failing_bootstrap_is_masked(tmp_path):
    fake = Fake(empty={"acme/api"})
    services = fake.services()

    def boom(cfg, label, repo):
        raise SetupError(f"clone https://x:{GH_TOKEN}@github.com/{repo} failed")

    services.bootstrap = boom
    script = Script(HAPPY + ["y"])
    lines: list[str] = []
    assert run_wizard(tmp_path, False, script.ask, script.ask_secret, services, lines.append) == 0
    assert GH_TOKEN not in "\n".join(lines)


def test_init_runs_before_config_from_env(tmp_path, monkeypatch):
    for var in ("TRELLO_API_KEY", "TRELLO_TOKEN", "GITHUB_TOKEN", "TRELLO_BOARD_ID"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("old")
    assert cli.main(["init", "--out", str(tmp_path)]) == 1
    assert (tmp_path / ".env").read_text() == "old"


def _mock(handler):
    return httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status,expected", [(409, True), (200, False)])
def test_github_is_empty(status, expected):
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(status, json=[])

    gh = GitHubClient("t", "o/r", http=_mock(handler))
    assert gh.is_empty() is expected
    assert seen[0].path == "/repos/o/r/commits" and seen[0].params["per_page"] == "1"


def test_github_is_empty_other_error():
    gh = GitHubClient("t", "o/r", http=_mock(lambda r: httpx.Response(404, json={})))
    with pytest.raises(GitHubError):
        gh.is_empty()


def test_github_repo_access():
    body = {"full_name": "o/r", "default_branch": "main", "permissions": {"push": True}}
    gh = GitHubClient("t", "o/r", http=_mock(lambda r: httpx.Response(200, json=body)))
    assert gh.repo_access() == ("o/r", True, "main")


def test_trello_boards():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, json=[{"id": "1", "name": "A", "url": "u"}])

    boards = TrelloClient("k", "t", "", http=_mock(handler)).boards()
    assert boards == [{"id": "1", "name": "A", "url": "u"}]
    assert seen[0].path == "/members/me/boards"
    assert seen[0].params["filter"] == "open" and seen[0].params["fields"] == "name,url"


def test_files_are_in_out_dir_created_if_missing(tmp_path):
    target = Path(tmp_path) / "nested" / "dir"
    rc, _, _, _ = run(target)
    assert rc == 0 and (target / ".env").exists()


def test_unexpected_errors_are_not_swallowed(tmp_path):
    fake = Fake()
    services = fake.services()

    def boom(key, token):
        raise ZeroDivisionError("bug")

    services.trello_me = boom
    script = Script(HAPPY)
    with pytest.raises(ZeroDivisionError):
        run_wizard(tmp_path, False, script.ask, script.ask_secret, services, lambda _: None)


def test_default_services_guard_converts_expected_errors():
    from lights_out.setup_wizard import _guard

    def http_fail():
        raise httpx.ConnectError("no network")

    def os_fail():
        raise PermissionError("denied")

    for fn in (http_fail, os_fail, lambda: {}["x"]):
        with pytest.raises(SetupError):
            _guard(fn)()
    assert _guard(lambda: 7)() == 7


def test_interrupt_while_writing_leaves_no_partial_files(tmp_path, monkeypatch):
    real_replace = os.replace
    calls = []

    def replace(src, dst):
        if calls:
            raise KeyboardInterrupt
        calls.append(dst)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    rc, _, _, output = run(tmp_path)
    assert rc == 130
    assert list(tmp_path.iterdir()) == []
