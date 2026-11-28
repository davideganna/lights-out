"""GitHub credentials: a personal token, or a GitHub App that mints short-lived installation tokens."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
import jwt

from ..core.config import Config
from .github import GitHubError

REFRESH_MARGIN_S = 300  # request a new installation token this long before it expires
JWT_BACKDATE_S = 60  # allow for clock drift
JWT_LIFETIME_S = 540  # GitHub allows at most 10 minutes


class TokenProvider(Protocol):
    is_app: bool

    def token_for(self, repo: str) -> str: ...


class StaticTokenProvider:
    """A personal access token."""

    is_app = False

    def __init__(self, token: str):
        self._token = token

    def token_for(self, repo: str) -> str:
        return self._token


class GitHubAppTokenProvider:
    is_app = True

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        *,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.app_id = app_id
        self._key = private_key_pem
        self._clock = clock
        self._http = http or httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        self._installations: dict[str, dict] = {}  # lower-case owner -> installation
        self._tokens: dict[int, tuple[str, float]] = {}  # installation id -> (token, refresh-at epoch seconds)
        self._slug: str | None = None

    def make_jwt(self) -> str:
        now = int(self._clock())
        claims = {"iat": now - JWT_BACKDATE_S, "exp": now + JWT_LIFETIME_S, "iss": self.app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def _call(self, method: str, path: str, *, auth: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self.make_jwt()}"} if auth else {}
        try:
            r = self._http.request(method, path, headers=headers)
        except httpx.HTTPError as e:
            raise GitHubError(f"GitHub {method} {path} failed: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise GitHubError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}", status=r.status_code)
        return r.json()

    def installation(self, repo: str) -> dict:
        owner = repo.split("/")[0].lower()
        if owner not in self._installations:
            try:
                self._installations[owner] = self._call("GET", f"/repos/{repo}/installation")
            except GitHubError as e:
                if e.status == 404:
                    raise GitHubError(f"GitHub App is not installed on {repo}", status=404) from e
                raise
        return self._installations[owner]

    def installation_id(self, repo: str) -> int:
        return self.installation(repo)["id"]

    def permissions(self, repo: str) -> dict:
        return self.installation(repo).get("permissions") or {}

    def token_for(self, repo: str) -> str:
        inst_id = self.installation_id(repo)
        cached = self._tokens.get(inst_id)
        if cached and self._clock() < cached[1]:
            return cached[0]
        data = self._call("POST", f"/app/installations/{inst_id}/access_tokens")
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[inst_id] = (data["token"], expires - REFRESH_MARGIN_S)
        return data["token"]

    def slug(self) -> str:
        if self._slug is None:
            self._slug = self._call("GET", "/app")["slug"]
        return self._slug

    def bot_identity(self) -> tuple[str, str]:
        """(name, email) for commits authored as the App's bot user."""
        login = f"{self.slug()}[bot]"
        user = self._call("GET", f"/users/{login}", auth=False)
        return login, f"{user['id']}+{login}@users.noreply.github.com"

    def is_installed(self, repo: str) -> bool:
        try:
            self.installation(repo)
        except GitHubError as e:
            if e.status == 404:
                return False
            raise
        return True


def provider_from_config(cfg: Config) -> TokenProvider:
    if not cfg.uses_github_app:
        return StaticTokenProvider(cfg.github_token)
    pem = Path(cfg.github_app_private_key_file).read_text(encoding="utf-8")
    return GitHubAppTokenProvider(cfg.github_app_id, pem)
