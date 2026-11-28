"""GitHub App token provider, client and workspace token handling; no network."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from lights_out.integrations.github import GitHubClient, GitHubError
from lights_out.integrations.github_auth import GitHubAppTokenProvider, StaticTokenProvider
from lights_out.orchestration.workspace import Workspace

T = 1_800_000_000.0


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, now: float = T):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Api:
    """Fake GitHub: records requests and answers the App endpoints."""

    def __init__(self, installed_owners=("acme", "other")):
        self.requests: list[httpx.Request] = []
        self.installed = set(installed_owners)
        self.tokens_issued = 0
        self.clock = Clock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/repos/") and path.endswith("/installation"):
            owner = path.split("/")[2]
            if owner not in self.installed:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"id": 100 + len(owner), "permissions": {"contents": "write"}})
        if path.startswith("/app/installations/") and request.method == "POST":
            self.tokens_issued += 1
            return httpx.Response(
                201, json={"token": f"ghs_{self.tokens_issued}", "expires_at": iso(self.clock.now + 3600)}
            )
        if path == "/app":
            return httpx.Response(200, json={"slug": "my-bot"})
        if path == "/users/my-bot[bot]":
            return httpx.Response(200, json={"id": 4242})
        return httpx.Response(404)

    def count(self, suffix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(suffix))


@pytest.fixture
def api() -> Api:
    return Api()


@pytest.fixture
def provider(api, pem) -> GitHubAppTokenProvider:
    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(api))
    return GitHubAppTokenProvider("123", pem, http=http, clock=api.clock)


def test_jwt_claims(provider, key):
    token = provider.make_jwt()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert claims == {"iat": int(T) - 60, "exp": int(T) + 540, "iss": "123"}


def test_installation_lookup_uses_jwt_and_is_cached_per_owner(provider, api, key):
    provider.installation_id("acme/one")
    provider.installation_id("ACME/two")
    assert api.count("/installation") == 1
    first = api.requests[0]
    assert first.url.path == "/repos/acme/one/installation"
    bearer = first.headers["Authorization"].removeprefix("Bearer ")
    assert jwt.decode(
        bearer, key.public_key(), algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    provider.installation_id("other/x")
    assert api.count("/installation") == 2


def test_not_installed(provider, api):
    with pytest.raises(GitHubError, match="not installed on nobody/x") as ei:
        provider.token_for("nobody/x")
    assert ei.value.status == 404
    assert provider.is_installed("nobody/x") is False
    assert provider.is_installed("acme/x") is True


def test_is_installed_raises_other_errors(pem):
    http = httpx.Client(base_url="https://x", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    p = GitHubAppTokenProvider("1", pem, http=http, clock=Clock())
    with pytest.raises(GitHubError):
        p.is_installed("a/b")


def test_permissions_kept(provider):
    assert provider.permissions("acme/x") == {"contents": "write"}


def test_token_cached_until_five_minutes_before_expiry(provider, api):
    assert provider.token_for("acme/a") == "ghs_1"
    assert provider.token_for("acme/b") == "ghs_1"
    assert api.count("/access_tokens") == 1
    api.clock.now = T + 3600 - 300 - 1
    assert provider.token_for("acme/a") == "ghs_1"
    api.clock.now = T + 3600 - 300
    assert provider.token_for("acme/a") == "ghs_2"
    assert api.count("/access_tokens") == 2


def test_errors_do_not_contain_credentials(pem):
    http = httpx.Client(base_url="https://x", transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad")))
    p = GitHubAppTokenProvider("1", pem, http=http, clock=Clock())
    with pytest.raises(GitHubError) as ei:
        p.token_for("a/b")
    assert "Bearer" not in str(ei.value) and "BEGIN" not in str(ei.value)


def test_static_provider():
    p = StaticTokenProvider("ghp_x")
    assert p.token_for("a/b") == "ghp_x"
    assert p.is_app is False


def test_bot_identity(provider, api):
    assert provider.slug() == "my-bot"
    assert provider.bot_identity() == ("my-bot[bot]", "4242+my-bot[bot]@users.noreply.github.com")
    users = [r for r in api.requests if r.url.path.startswith("/users/")]
    assert len(users) == 1 and "Authorization" not in users[0].headers
    assert api.count("/app") == 1  # slug cached


def test_client_asks_provider_on_every_request():
    seen: list[str] = []
    calls = {"n": 0}

    def token() -> str:
        calls["n"] += 1
        return f"tok{calls['n']}"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"state": "open"})

    client = GitHubClient(token, "o/r", http=httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler)))
    client._req("GET", "/a")
    client._req("GET", "/b")
    client._get_raw("/c")
    assert seen == ["Bearer tok1", "Bearer tok2", "Bearer tok3"]


def test_client_accepts_plain_string_token():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    client = GitHubClient("ghp_s", "o/r")
    client._http = httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    client._req("GET", "/a")
    assert seen == ["Bearer ghp_s"]


def test_default_client_has_no_authorization_header():
    assert "Authorization" not in GitHubClient("ghp_s", "o/r")._http.headers


def test_workspace_asks_for_a_fresh_token_each_time(tmp_path):
    n = {"i": 0}

    def token() -> str:
        n["i"] += 1
        return f"tok{n['i']}"

    ws = Workspace(tmp_path, repo_url="https://github.com/o/r.git", base_branch="main", github_token=token)

    def header(env: dict[str, str]) -> str:
        return base64.b64decode(env["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")).decode()

    assert header(ws._git_env()) == "x-access-token:tok1"
    assert header(ws._git_env()) == "x-access-token:tok2"


def test_workspace_token_is_not_in_argv(tmp_path, monkeypatch):
    import subprocess

    captured = []

    def fake_run(cmd, **kw):
        captured.append((cmd, kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tokens = iter(["secret-1", "secret-2"])
    ws = Workspace(
        tmp_path, repo_url="https://github.com/o/r.git", base_branch="main", github_token=lambda: next(tokens)
    )
    ws.git("status")
    ws.git("push")
    assert len(captured) == 2
    for cmd, env in captured:
        assert not any("secret" in a for a in cmd)
        assert "secret" in base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode()
    assert captured[0][1]["GIT_CONFIG_VALUE_0"] != captured[1][1]["GIT_CONFIG_VALUE_0"]


def test_workspace_empty_token_adds_no_header(tmp_path):
    ws = Workspace(tmp_path, repo_url="https://github.com/o/r.git", base_branch="main", github_token="")
    assert "GIT_CONFIG_COUNT" not in ws._git_env()
