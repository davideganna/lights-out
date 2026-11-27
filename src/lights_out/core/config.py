"""Configuration, loaded from environment variables (see .env.example)."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeGuard

import yaml

from .routing import is_repo, parse_repo_labels

# Secrets the orchestrator needs but the coding agent must never see. They are
# removed from os.environ after loading, because the Claude Code subprocess
# inherits os.environ.
_ORCHESTRATOR_SECRETS = ("GITHUB_TOKEN", "TRELLO_API_KEY", "TRELLO_TOKEN", "ALERT_WEBHOOK_URL")

# Kinds of alert lights-out can send (ALERT_EVENTS picks a subset).
ALERT_EVENT_NAMES: tuple[str, ...] = ("failed", "pr_open", "needs_info", "budget", "lifecycle", "error")


DEFAULT_HUMAN_REVIEW_PATHS: tuple[str, ...] = (
    ".github/**",
    "Dockerfile*",
    "docker/**",
    "Makefile",
    "pyproject.toml",
    "requirements*.txt",
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
    "**/migrations/**",
    "**/auth/**",
    "**/*auth*.py",
)


# The only settings a `repos:` entry in lights-out.yaml may override for one repository.
REPO_OVERRIDE_KEYS: tuple[str, ...] = (
    "base_branch",
    "check_command",
    "check_timeout_s",
    "ci_timeout_s",
    "human_review_paths",
    "max_auto_merge_lines",
    "max_ticket_usd",
    "merge_without_ci",
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(RuntimeError):
    pass


def _repo_labels(spec: str) -> dict[str, str]:
    try:
        return parse_repo_labels(spec)
    except ValueError as e:
        raise ConfigError(str(e)) from e


def parse_members(spec: str) -> list[str]:
    """Comma-separated Trello usernames -> lowercase, no '@', no blanks or duplicates."""
    out: list[str] = []
    for part in spec.split(","):
        name = part.strip().lstrip("@").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def parse_patterns(spec: str) -> list[str]:
    """Comma-separated glob patterns -> list, with blanks dropped."""
    return [p.strip() for p in spec.split(",") if p.strip()]


def parse_alert_events(spec: str) -> list[str]:
    """Comma-separated alert event names -> list (blank = all). Unknown names are a ConfigError."""
    out: list[str] = []
    for part in spec.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in ALERT_EVENT_NAMES:
            raise ConfigError(f"Unknown ALERT_EVENTS name {name!r}; valid names: {', '.join(ALERT_EVENT_NAMES)}")
        if name not in out:
            out.append(name)
    return out or list(ALERT_EVENT_NAMES)


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"Missing required environment variable {name}")
    return value or ""


def _check_effort(name: str, value: str) -> str:
    if value and value not in EFFORT_LEVELS:
        raise ConfigError(f"{name} must be one of low, medium, high, xhigh, max or empty, got {value!r}")
    return value


def _effort(name: str, default: str) -> str:
    return _check_effort(name, _get(name, default).strip())


@dataclass
class Lists:
    """Names of the Trello columns the agent works with.

    The agent takes cards from `ready`. The stage columns (triaged, verifying,
    reviewing, pr_opened) show progress; set one to "" to skip it."""

    ready: str = "Queued"
    triaged: str = "Triaged"
    in_progress: str = "In Progress"
    verifying: str = "Verifying"
    reviewing: str = "Reviewing"
    pr_opened: str = "PR Opened"
    needs_info: str = "Needs Info"
    failed: str = "Failed"
    done: str = "Done"

    def all(self) -> list[str]:
        names = [
            self.ready,
            self.triaged,
            self.in_progress,
            self.verifying,
            self.reviewing,
            self.pr_opened,
            self.needs_info,
            self.failed,
            self.done,
        ]
        return [n for n in names if n]


@dataclass
class Config:
    trello_api_key: str
    trello_token: str
    trello_board_id: str
    github_token: str
    # Trello label -> "owner/repo"; a card's label picks the repository it is about.
    repo_labels: dict[str, str] = field(default_factory=dict)
    # Trello usernames (lowercase) whose queued cards are processed; empty = any board member
    allowed_members: list[str] = field(default_factory=list)
    default_repo: str = ""  # used for cards with no repository label; empty = ask for a label
    base_branch: str = "main"
    lists: Lists = field(default_factory=Lists)
    # "owner/repo" -> {setting: value} from `repos:` mapping entries (see REPO_OVERRIDE_KEYS)
    repo_overrides: dict[str, dict[str, object]] = field(default_factory=dict)

    # Models
    planner_model: str = "claude-opus-5-5"
    coder_model: str = "claude-sonnet-5-5"
    reviewer_model: str = "claude-opus-5-5"
    # Effort per role; empty = do not pass an effort (the model default applies)
    planner_effort: str = "high"
    coder_effort: str = ""
    reviewer_effort: str = "high"

    # Budgets and limits
    max_ticket_usd: float = 10.0
    max_day_usd: float = 50.0
    max_fix_attempts: int = 3
    max_ci_fix_attempts: int = 2
    max_review_rounds: int = 2
    max_pr_revisions: int = 3
    coder_max_turns: int = 150
    check_command: str = "make check"
    check_timeout_s: int = 1200
    ci_timeout_s: int = 1800
    ci_grace_s: int = 180
    merge_without_ci: bool = False
    # Changes touching these paths (glob patterns) or exceeding this many changed
    # lines (added + deleted) are left open for a human instead of auto-merged.
    human_review_paths: list[str] = field(default_factory=lambda: list(DEFAULT_HUMAN_REVIEW_PATHS))
    max_auto_merge_lines: int = 400  # 0 disables the size limit

    # How often to check the Queued column for new cards
    poll_seconds: int = 15
    health_stale_s: int = 900
    # How often to look at open PRs that were left for a human (merge command, merged or closed by hand)
    pr_poll_seconds: int = 120

    # Runtime
    data_dir: Path = Path("/data")
    agent_user: str = ""  # run Claude + checks as this OS user (set in Docker)
    git_author_name: str = "lights-out"
    git_author_email: str = "lights-out@users.noreply.github.com"
    report_port: int = 8080

    # Alerts: POST {"text": ...} to this URL (secret; empty = off) for the listed events
    alert_webhook_url: str = field(default="", repr=False)
    alert_events: list[str] = field(default_factory=lambda: list(ALERT_EVENT_NAMES))

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    def run_settings(self) -> dict[str, str]:
        """Model and effort per role, as recorded with each run."""
        return {
            "planner": f"{self.planner_model}/{self.planner_effort or 'default'}",
            "coder": f"{self.coder_model}/{self.coder_effort or 'default'}",
            "reviewer": f"{self.reviewer_model}/{self.reviewer_effort or 'default'}",
        }

    def for_repo(self, repo: str) -> Config:
        """This config with the repository's overrides applied (self if it has none)."""
        overrides = self.repo_overrides.get(repo)
        if not overrides:
            return self
        copied: dict[str, Any] = {k: list(v) if isinstance(v, list) else v for k, v in overrides.items()}
        return dataclasses.replace(self, **copied)

    @classmethod
    def from_env(cls, *, scrub_secrets: bool = True) -> Config:
        env = os.environ
        values = {k: v for k, (v, _src) in _resolve(env).items()}
        overrides = {} if "REPO_LABELS" in env else _file_repo_overrides(env)
        for name in ("TRELLO_API_KEY", "TRELLO_TOKEN", "GITHUB_TOKEN"):
            _get(name, required=True)
        if not values["trello_board_id"]:
            raise ConfigError(
                "Missing required environment variable TRELLO_BOARD_ID (or trello_board_id in lights-out.yaml)"
            )
        simple = {k: v for k, v in values.items() if k != "repos" and not k.startswith("lists.")}
        cfg = cls(
            trello_api_key=_get("TRELLO_API_KEY"),
            trello_token=_get("TRELLO_TOKEN"),
            github_token=_get("GITHUB_TOKEN"),
            repo_labels=values["repos"],
            repo_overrides=overrides,
            lists=Lists(**{n: values[f"lists.{n}"] for n in Lists().__dict__}),
            alert_webhook_url=_get("ALERT_WEBHOOK_URL").strip(),
            **simple,
        )
        if os.environ.get("GITHUB_REPO"):
            raise ConfigError(
                "GITHUB_REPO was replaced by REPO_LABELS (e.g. REPO_LABELS=my-app=owner/my-app) "
                "and the optional DEFAULT_REPO. Update your .env."
            )
        if not cfg.repo_labels and not cfg.default_repo:
            raise ConfigError(
                "Set REPO_LABELS (label=owner/repo, comma-separated) or `repos:` in lights-out.yaml "
                "so cards can be routed"
            )
        if cfg.default_repo and "/" not in cfg.default_repo:
            raise ConfigError("DEFAULT_REPO must look like 'owner/name'")
        if scrub_secrets:
            for name in _ORCHESTRATOR_SECRETS:
                os.environ.pop(name, None)
        for d in (cfg.workspaces_dir, cfg.reports_dir, cfg.runs_dir):
            d.mkdir(parents=True, exist_ok=True)
        return cfg


# --- Configuration file (lights-out.yaml) -------------------------------------------------------

DEFAULT_CONFIG_FILE = "lights-out.yaml"
FILE_SECRET_KEYS = (
    "github_token",
    "trello_api_key",
    "trello_token",
    "alert_webhook_url",
    "claude_code_oauth_token",
    "anthropic_api_key",
)

# (key, default, kind) for every non-secret setting except `lists` and `repos`. The environment
# variable is key.upper().
_SETTINGS: tuple[tuple[str, object, str], ...] = (
    ("trello_board_id", "", "str"),
    ("allowed_members", [], "members"),
    ("default_repo", "", "str"),
    ("base_branch", "main", "str"),
    ("planner_model", "claude-opus-5-5", "str"),
    ("coder_model", "claude-sonnet-5-5", "str"),
    ("reviewer_model", "claude-opus-5-5", "str"),
    ("planner_effort", "high", "effort"),
    ("coder_effort", "", "effort"),
    ("reviewer_effort", "high", "effort"),
    ("max_ticket_usd", 10.0, "float"),
    ("max_day_usd", 50.0, "float"),
    ("max_fix_attempts", 3, "int"),
    ("max_ci_fix_attempts", 2, "int"),
    ("max_review_rounds", 2, "int"),
    ("max_pr_revisions", 3, "int"),
    ("coder_max_turns", 150, "int"),
    ("check_command", "make check", "str"),
    ("check_timeout_s", 1200, "int"),
    ("ci_timeout_s", 1800, "int"),
    ("ci_grace_s", 180, "int"),
    ("merge_without_ci", False, "bool"),
    ("human_review_paths", list(DEFAULT_HUMAN_REVIEW_PATHS), "patterns"),
    ("max_auto_merge_lines", 400, "int"),
    ("poll_seconds", 15, "int"),
    ("health_stale_s", 900, "int"),
    ("pr_poll_seconds", 120, "int"),
    ("data_dir", Path("/data"), "path"),
    ("agent_user", "", "str"),
    ("git_author_name", "lights-out", "str"),
    ("git_author_email", "lights-out@users.noreply.github.com", "str"),
    ("report_port", 8080, "int"),
    ("alert_events", list(ALERT_EVENT_NAMES), "events"),
)

_DISPLAY_SECRETS = (
    "GITHUB_TOKEN",
    "TRELLO_API_KEY",
    "TRELLO_TOKEN",
    "ALERT_WEBHOOK_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)


def valid_file_keys() -> list[str]:
    """Top-level keys allowed in the file, derived from the Config fields."""
    skip = set(FILE_SECRET_KEYS) | {"repo_labels", "repo_overrides"}
    return sorted({f.name for f in fields(Config) if f.name not in skip} | {"repos"})


def load_config_file(env: Mapping[str, str]) -> tuple[dict, Path | None]:
    """Read and validate the optional YAML file. Returns ({}, None) when the default file is absent."""
    explicit = env.get("LIGHTS_OUT_CONFIG", "")
    path = Path(explicit or DEFAULT_CONFIG_FILE)
    if not path.is_file():
        if explicit:
            raise ConfigError(f"LIGHTS_OUT_CONFIG points to {path}, which does not exist")
        return {}, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e
    if data is None:
        return {}, path
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping of settings at the top level")
    valid = valid_file_keys()
    for key in data:
        if str(key).lower() in FILE_SECRET_KEYS:
            raise ConfigError(f"{key} is a secret; move it to .env as {str(key).upper()}")
        if key not in valid:
            raise ConfigError(f"Unknown key {key!r} in {path}; valid keys: {', '.join(valid)}")
    lists = data.get("lists")
    if lists is not None:
        if not isinstance(lists, dict):
            raise ConfigError(f"lists in {path} must be a mapping, got {lists!r}")
        names = sorted(Lists().__dict__)
        for sub in lists:
            if sub not in names:
                raise ConfigError(f"Unknown key 'lists.{sub}' in {path}; valid keys: {', '.join(names)}")
    return data, path


def _bad_type(key: str, path: Path | None, expected: str, value: object) -> ConfigError:
    return ConfigError(f"{key} in {path} must be {expected}, got {value!r}")


def _from_file(key: str, kind: str, value: object, path: Path | None) -> object:
    def is_str_list(v: object) -> TypeGuard[list[str]]:
        return isinstance(v, list) and all(isinstance(i, str) for i in v)

    if kind in ("str", "path", "effort"):
        if not isinstance(value, str):
            raise _bad_type(key, path, "text", value)
        if kind == "path":
            return Path(value)
        if kind == "effort":
            return _check_effort(key.upper(), value.strip())
        return value
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise _bad_type(key, path, "an integer", value)
        return value
    if kind == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _bad_type(key, path, "a number", value)
        return float(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise _bad_type(key, path, "true or false", value)
        return value
    if kind in ("members", "patterns", "events"):
        if not is_str_list(value):
            raise _bad_type(key, path, "a list of text values", value)
        joined = ",".join(value)
        if kind == "members":
            return parse_members(joined)
        if kind == "events":
            return parse_alert_events(joined)
        return [p.strip() for p in value if p.strip()]
    if kind == "repos":
        return _repos_from_file(key, value, path)[0]
    raise AssertionError(kind)


def _repos_from_file(key: str, value: object, path: Path | None) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    """Labels and per-repository overrides from the file's `repos:` mapping."""
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise _bad_type(key, path, "a mapping of label to owner/repo", value)
    kinds = {k: kind for k, _d, kind in _SETTINGS}
    labels: dict[str, str] = {}
    overrides: dict[str, dict[str, object]] = {}
    for label, entry in value.items():
        name = label.strip().lower()
        if isinstance(entry, dict):
            repo = entry.get("repo")
            if not isinstance(repo, str) or not name or not is_repo(repo.strip()):
                raise ConfigError(f"repos entry {label!r} in {path} needs `repo: owner/repo`, got {repo!r}")
            repo = repo.strip()
            found: dict[str, object] = {}
            for k, v in entry.items():
                if k == "repo":
                    continue
                if k not in REPO_OVERRIDE_KEYS:
                    raise ConfigError(
                        f"repos entry {label!r} ({repo}): {k!r} can't be set per repository; "
                        f"allowed keys: {', '.join(REPO_OVERRIDE_KEYS)}"
                    )
                found[k] = _from_file(f"repos.{label}.{k}", kinds[k], v, path)
        elif isinstance(entry, str):
            repo, found = entry.strip(), {}
            if not name or not is_repo(repo):
                raise ConfigError(f"repos entry {label!r}: {repo!r} in {path} must look like 'label: owner/repo'")
        else:
            raise _bad_type(key, path, "a mapping of label to owner/repo", value)
        if name in labels:
            raise ConfigError(f"repos in {path} lists the label {label!r} twice")
        labels[name] = repo
        if found:
            if repo in overrides:
                raise ConfigError(f"repos in {path} sets overrides for {repo} more than once")
            overrides[repo] = found
    return labels, overrides


def _file_repo_overrides(env: Mapping[str, str]) -> dict[str, dict[str, object]]:
    data, path = load_config_file(env)
    if data.get("repos") is None:
        return {}
    return _repos_from_file("repos", data["repos"], path)[1]


def ignored_repo_overrides(env: Mapping[str, str]) -> Path | None:
    """The config file's path when REPO_LABELS in env makes its per-repository overrides inactive."""
    if "REPO_LABELS" not in env:
        return None
    data, path = load_config_file(env)
    if path is None or data.get("repos") is None:
        return None
    return path if _repos_from_file("repos", data["repos"], path)[1] else None


def _from_env(name: str, kind: str, raw: str) -> object:
    if kind == "str":
        return raw
    if kind == "path":
        return Path(raw)
    if kind == "effort":
        return _check_effort(name, raw.strip())
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return raw.lower() == "true"
    if kind == "members":
        return parse_members(raw)
    if kind == "patterns":
        return parse_patterns(raw)
    if kind == "events":
        return parse_alert_events(raw)
    if kind == "repos":
        return _repo_labels(raw)
    raise AssertionError(kind)


def _resolve(env: Mapping[str, str]) -> dict[str, tuple[object, str]]:
    """Value and source ("env", "file" or "default") for every non-secret setting."""
    data, path = load_config_file(env)
    table: list[tuple[str, str, object, str]] = [(k, k.upper(), d, kind) for k, d, kind in _SETTINGS]
    table.append(("repos", "REPO_LABELS", {}, "repos"))
    file_lists = data.get("lists") or {}
    for name, default in Lists().__dict__.items():
        table.append((f"lists.{name}", f"LIST_{name.upper()}", default, "str"))
    out: dict[str, tuple[object, str]] = {}
    for key, var, default, kind in table:
        file_value = file_lists.get(key[6:]) if key.startswith("lists.") else data.get(key)
        has_file = (key[6:] in file_lists) if key.startswith("lists.") else key in data
        if var in env:
            out[key] = (_from_env(var, kind, env[var]), "env")
        elif has_file and file_value is not None:
            out[key] = (_from_file(key, kind, file_value, path), "file")
        else:
            out[key] = (list(default) if isinstance(default, list) else default, "default")
    return out


def effective_settings(env: Mapping[str, str] | None = None) -> list[tuple[str, str, str]]:
    """(key, display value, source) for every setting; secrets only as set / not set."""
    env = os.environ if env is None else env
    rows: list[tuple[str, str, str]] = []
    for key, (value, source) in _resolve(env).items():
        if isinstance(value, dict):
            text = ",".join(f"{k}={v}" for k, v in value.items())
        elif isinstance(value, list):
            text = ",".join(value)
        else:
            text = str(value).lower() if isinstance(value, bool) else str(value)
        rows.append((key, text, source))
        if key == "repos" and source == "file":
            labels = value if isinstance(value, dict) else {}
            for repo, found in _file_repo_overrides(env).items():
                label = next(lb for lb, r in labels.items() if r == repo)
                for k, v in found.items():
                    if isinstance(v, list):
                        shown = ",".join(v)
                    else:
                        shown = str(v).lower() if isinstance(v, bool) else str(v)
                    rows.append((f"repos.{label}.{k}", shown, "file"))
    for name in _DISPLAY_SECRETS:
        is_set = bool(env.get(name))
        rows.append((name.lower(), "set" if is_set else "not set", "env" if is_set else "default"))
    return rows
