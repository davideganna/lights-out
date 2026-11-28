"""`lights-out init`: interactive setup that checks credentials and writes `.env` and `lights-out.yaml`.

The flow takes injected `ask` / `ask_secret` functions and a `Services` bundle of checks, so tests can
script the answers and fake Trello, GitHub and Claude. Nothing is written until every question is
answered, and no secret is ever printed in full."""

from __future__ import annotations

import functools
import getpass
import math
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from .core.config import Config, parse_members
from .core.routing import is_repo

TRELLO_KEYS_URL = "https://trello.com/power-ups/admin"
GITHUB_PERMISSIONS = (
    "Contents, Pull requests and Workflows: read/write; Checks: read. Optional: Actions: read (CI failure "
    "logs for the coder) and Administration: read (so `doctor` can verify branch protection)."
)
CLAUDE_VARS = {"oauth": "CLAUDE_CODE_OAUTH_TOKEN", "api-key": "ANTHROPIC_API_KEY"}
DEFAULT_BUDGET = "25"
DEFAULT_CHECK = "make check"


class SetupError(RuntimeError):
    """A service check failed in an expected way (bad credential, no network, git or file problem).

    `default_services` converts the real clients' errors to this, so the flow catches only it."""


Ask = Callable[[str, str], str]
AskSecret = Callable[[str], str]


def mask(secret: str) -> str:
    """`abcd…wxyz` for long secrets; short ones are never shown at all."""
    if len(secret) >= 16:
        return f"{secret[:4]}…{secret[-4:]}"
    return "…"


@dataclass
class Services:
    """The outside world, injected so tests need no network."""

    trello_me: Callable[[str, str], str]  # (key, token) -> username; raises if bad
    trello_boards: Callable[[str, str], list[tuple[str, str]]]  # open boards: (id, name)
    github_user: Callable[[str], str]
    github_repo: Callable[[str, str], tuple[str, bool]]  # (token, repo) -> (full_name, push)
    github_is_empty: Callable[[str, str], bool]
    claude: Callable[[str, str], str]  # (variable, value) -> model; raises if bad
    setup_board: Callable[[Config], tuple[list[str], list[str]]]
    bootstrap: Callable[[Config, str, str], bool]


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a real service call so the errors it can raise become SetupError."""
    from .agents.agents import AgentError
    from .integrations.github import GitHubError
    from .integrations.trello import TrelloError
    from .orchestration.workspace import WorkspaceError

    expected = (
        GitHubError,
        TrelloError,
        AgentError,
        WorkspaceError,
        httpx.HTTPError,
        OSError,
        subprocess.SubprocessError,
        KeyError,  # a reply without the expected field
        ValueError,  # a reply that is not JSON
    )

    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except expected as e:
            raise SetupError(str(e) or type(e).__name__) from e

    return call


def default_services(
    setup_board: Callable[[Config], tuple[list[str], list[str]]],
    bootstrap: Callable[[Config, str, str], bool],
) -> Services:
    """The real checks, built from the existing clients. `cli` passes in setup_board and bootstrap."""
    from .agents.agents import CLAUDE_CREDENTIAL_VARS, check_claude
    from .integrations.github import GitHubClient
    from .integrations.trello import TrelloClient

    def trello_boards(key: str, token: str) -> list[tuple[str, str]]:
        return [(b["id"], b["name"]) for b in TrelloClient(key, token, "").boards()]

    def github_repo(token: str, repo: str) -> tuple[str, bool]:
        full_name, push, _branch = GitHubClient(token, repo).repo_access()
        return full_name, push

    def claude(var: str, value: str) -> str:
        # The check reads the credential from the environment: set it only for the check.
        names = set(CLAUDE_CREDENTIAL_VARS) | {var}
        saved = {n: os.environ.get(n) for n in names}
        try:
            for n in names:
                os.environ.pop(n, None)
            os.environ[var] = value
            return check_claude(Config("", "", "", "", agent_user=os.environ.get("AGENT_USER", "")))
        finally:
            for n, old in saved.items():
                os.environ.pop(n, None)
                if old is not None:
                    os.environ[n] = old

    return Services(
        trello_me=_guard(lambda key, token: TrelloClient(key, token, "").me()),
        trello_boards=_guard(trello_boards),
        github_user=_guard(lambda token: GitHubClient(token, "")._req("GET", "/user")["login"]),
        github_repo=_guard(github_repo),
        github_is_empty=_guard(lambda token, repo: GitHubClient(token, repo).is_empty()),
        claude=_guard(claude),
        setup_board=_guard(setup_board),
        bootstrap=_guard(bootstrap),
    )


def _input(prompt: str, default: str) -> str:
    return input(f"{prompt} [{default}]: " if default else f"{prompt}: ")


def _getpass(prompt: str) -> str:
    return getpass.getpass(f"{prompt}: ")


class _Wizard:
    def __init__(self, ask: Ask, ask_secret: AskSecret, services: Services, out: Callable[[str], None]):
        self._ask, self._ask_secret, self.svc, self.say = ask, ask_secret, services, out
        self.secrets: list[str] = []

    def safe(self, text: object) -> str:
        """Text with every secret collected so far masked."""
        result = str(text)
        for s in sorted(self.secrets, key=len, reverse=True):
            if s:
                result = result.replace(s, mask(s))
        return result

    def ask(self, prompt: str, default: str = "") -> str:
        return (self._ask(prompt, default) or "").strip() or default

    def secret(self, prompt: str) -> str:
        while True:
            value = (self._ask_secret(prompt) or "").strip()
            if value and not any(c.isspace() for c in value):
                self.secrets.append(value)
                return value
            self.say("The value must not be empty or contain spaces.")

    def failed(self, what: str, error: object) -> None:
        self.say(f"{what} failed: {self.safe(error)}")

    # --- steps -----------------------------------------------------------
    def trello(self) -> tuple[str, str, str, str, str]:
        """Returns (key, token, username, board id, board name)."""
        self.say(
            f"\n1. Trello. Create an API key and a token at {TRELLO_KEYS_URL} "
            "(create a Power-Up, then 'API key', then the token link)."
        )
        while True:
            key = self.secret("Trello API key")
            token = self.secret("Trello token")
            try:
                user = self.svc.trello_me(key, token)
                break
            except SetupError as e:
                self.failed("Trello check", e)
                self.say("Check the API key and token and try again.")
        self.say(f"Trello: signed in as {user}.")
        while True:
            try:
                boards = self.svc.trello_boards(key, token)
            except SetupError as e:
                self.failed("Listing Trello boards", e)
                boards = []
            if boards:
                break
            self.say("No open boards found. Create one in Trello.")
            self.ask("Press Enter to look again")
        for i, (_id, name) in enumerate(boards, 1):
            self.say(f"  {i}. {name}")
        while True:
            answer = self.ask("Board number")
            if answer.isdigit() and 1 <= int(answer) <= len(boards):
                board_id, board_name = boards[int(answer) - 1]
                return key, token, user, board_id, board_name
            self.say(f"Enter a number from 1 to {len(boards)}.")

    def github(self) -> tuple[str, dict[str, str]]:
        self.say(
            "\n2. GitHub. Create a fine-grained token limited to your target repositories.\n"
            f"Permissions: {GITHUB_PERMISSIONS}"
        )
        while True:
            token = self.secret("GitHub token")
            try:
                login = self.svc.github_user(token)
                break
            except SetupError as e:
                self.failed("GitHub check", e)
                self.say("Check the token and try again.")
        self.say(f"GitHub: signed in as {login}.")
        repos: dict[str, str] = {}  # label -> owner/repo
        while True:
            repo = self.ask("Repository (owner/repo)" + ("; empty to finish" if repos else ""))
            if not repo:
                if repos:
                    return token, repos
                self.say("Enter at least one repository.")
                continue
            if not is_repo(repo):
                self.say(f"{repo!r} is not in the form owner/repo.")
                continue
            if repo.lower() in {r.lower() for r in repos.values()}:
                self.say(f"{repo} is already in the list.")
                continue
            label = self.ask("Trello label for this repository", repo.split("/")[1].lower())
            if label.lower() in {lb.lower() for lb in repos}:
                self.say(f"The label {label!r} is already used; labels must be unique.")
                continue
            try:
                full_name, push = self.svc.github_repo(token, repo)
            except SetupError as e:
                self.failed(f"Checking {repo}", e)
                self.say("Check the name and that the token has access to this repository.")
                continue
            if not push:
                self.say(f"The token cannot push to {full_name}: give it Contents: read/write on this repository.")
                continue
            self.say(f"GitHub: {full_name} OK (push access).")
            repos[label] = repo

    def claude(self) -> tuple[str, str]:
        self.say("\n3. Claude. Use a token from `claude setup-token` (oauth) or an Anthropic API key (api-key).")
        while True:
            kind = self.ask("Credential type (oauth or api-key)", "oauth").lower()
            if kind in CLAUDE_VARS:
                break
            self.say("Answer oauth or api-key.")
        var = CLAUDE_VARS[kind]
        while True:
            value = self.secret(var)
            try:
                model = self.svc.claude(var, value)
                break
            except SetupError as e:
                self.failed("Claude check", e)
                self.say("Check the credential and try again.")
        self.say(f"Claude: OK (model {model}).")
        return var, value

    def optional(self, user: str) -> tuple[list[str], float, str]:
        self.say("\n4. Optional settings.")
        members = parse_members(self.ask("Trello usernames allowed to queue cards (comma-separated)", user))
        while True:
            raw = self.ask("Daily budget in USD", DEFAULT_BUDGET)
            try:
                budget = float(raw)
            except ValueError:
                budget = math.nan
            if math.isfinite(budget) and budget > 0:
                break
            self.say("The budget must be a number above 0.")
        check = self.ask("Check command run on every change", DEFAULT_CHECK)
        return members, budget, check


def _write_files(out_dir: Path, env_lines: dict[str, str], settings: dict) -> tuple[Path, Path]:
    """Write both files, or neither: each goes to a temporary file first and is then renamed into place."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env_path, yaml_path = out_dir / ".env", out_dir / "lights-out.yaml"
    contents = [
        (env_path, "".join(f"{k}={v}\n" for k, v in env_lines.items()), 0o600),
        (yaml_path, yaml.safe_dump(settings, sort_keys=False), 0o644),
    ]
    temps: list[Path] = []
    created: list[Path] = []  # final files that did not exist before
    try:
        for path, text, mode in contents:
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{path.name}.")
            temps.append(Path(tmp))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), mode)  # the mode is right even when --force replaces a looser file
                f.write(text)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                # In Docker we run as root: hand the files to whoever owns the mounted directory.
                st = out_dir.stat()
                os.chown(tmp, st.st_uid, st.st_gid)
        for (path, _text, _mode), tmp in zip(contents, temps, strict=True):
            if not path.exists():
                created.append(path)
            os.replace(tmp, path)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
    return env_path, yaml_path


def run_wizard(
    out_dir: Path,
    force: bool,
    ask: Ask = _input,
    ask_secret: AskSecret = _getpass,
    services: Services | None = None,
    out: Callable[[str], None] = print,
) -> int:
    if services is None:
        raise ValueError("services is required")
    out_dir = Path(out_dir)
    existing = [p for p in (out_dir / ".env", out_dir / "lights-out.yaml") if p.exists()]
    if existing and not force:
        for p in existing:
            out(f"{p} already exists. Use --force to overwrite it.")
        return 1

    w = _Wizard(ask, ask_secret, services, out)
    written = False
    try:
        out("lights-out setup: each credential is checked right away. Nothing is written until the end.")
        key, token, user, board_id, board_name = w.trello()
        gh_token, repos = w.github()
        claude_var, claude_value = w.claude()
        members, budget, check = w.optional(user)

        env_lines = {
            "TRELLO_API_KEY": key,
            "TRELLO_TOKEN": token,
            "GITHUB_TOKEN": gh_token,
            claude_var: claude_value,
        }
        settings = {
            "trello_board_id": board_id,
            "repos": dict(repos),
            "allowed_members": members,
            "max_day_usd": budget,
            "check_command": check,
        }
        env_path, yaml_path = _write_files(out_dir, env_lines, settings)
        written = True

        out(f"\n5. Wrote {env_path} (mode 600) and {yaml_path}:")
        for k, v in env_lines.items():
            out(f"  {k}={mask(v)}")
        out(f"  trello_board_id: {board_id} ({board_name})")
        out("  repos: " + ", ".join(f"{lb} -> {r}" for lb, r in repos.items()))
        out("  allowed_members: " + (", ".join(members) or "(anyone on the board)"))
        out(f"  max_day_usd: {budget}")
        out(f"  check_command: {check}")

        cfg = Config(
            trello_api_key=key,
            trello_token=token,
            trello_board_id=board_id,
            github_token=gh_token,
            repo_labels=dict(repos),
            allowed_members=members,
            max_day_usd=budget,
            check_command=check,
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            agent_user=os.environ.get("AGENT_USER", ""),
        )
        if w.ask("\n6. Create board columns and labels now? (y/n)", "y").lower() in ("y", "yes"):
            try:
                lists, labels = services.setup_board(cfg)
                out(f"Created lists: {lists}" if lists else "All lists already exist.")
                out(f"Created labels: {labels}" if labels else "All repository labels already exist.")
            except SetupError as e:
                w.failed("Board setup", e)
                out("Run `lights-out setup-board` later.")
        for label, repo in repos.items():
            try:
                empty = services.github_is_empty(gh_token, repo)
            except SetupError as e:
                w.failed(f"Checking whether {repo} is empty", e)
                continue
            if empty and w.ask(
                f"7. {repo} has no commits. Bootstrap it (CI, Makefile, CLAUDE.md)? (y/n)", "y"
            ).lower() in (
                "y",
                "yes",
            ):
                try:
                    done = services.bootstrap(cfg, label, repo)
                    out("Pushed the initial commit." if done else "Repository already has commits; nothing to do.")
                except SetupError as e:
                    w.failed(f"Bootstrapping {repo}", e)
                    out(f"Run `lights-out bootstrap {label}` later.")

        out("\n8. Next steps:")
        out("  1. In docker-compose.yml, uncomment the `./lights-out.yaml:/app/lights-out.yaml:ro` line.")
        out("  2. docker compose run --rm lights-out doctor")
        out("  3. docker compose up -d")
        return 0
    except (KeyboardInterrupt, EOFError):
        out("\nAborted; the files written so far are complete." if written else "\nAborted; nothing written.")
        return 130
