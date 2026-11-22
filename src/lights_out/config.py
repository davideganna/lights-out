"""Configuration, loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .routing import parse_repo_labels

# Secrets the orchestrator needs but the coding agent must never see. They are
# removed from os.environ after loading, because the Claude Code subprocess
# inherits os.environ.
_ORCHESTRATOR_SECRETS = ("GITHUB_TOKEN", "TRELLO_API_KEY", "TRELLO_TOKEN")


class ConfigError(RuntimeError):
    pass


def _repo_labels(spec: str) -> dict[str, str]:
    try:
        return parse_repo_labels(spec)
    except ValueError as e:
        raise ConfigError(str(e)) from e


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"Missing required environment variable {name}")
    return value or ""


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
    default_repo: str = ""  # used for cards with no repository label; empty = ask for a label
    base_branch: str = "main"
    lists: Lists = field(default_factory=Lists)

    # Models
    planner_model: str = "claude-opus-5-5"
    coder_model: str = "claude-sonnet-5-5"
    reviewer_model: str = "claude-opus-5-5"

    # Budgets and limits
    max_ticket_usd: float = 10.0
    max_day_usd: float = 50.0
    max_fix_attempts: int = 3
    max_review_rounds: int = 2
    coder_max_turns: int = 150
    check_command: str = "make check"
    check_timeout_s: int = 1200
    ci_timeout_s: int = 1800
    ci_grace_s: int = 180
    merge_without_ci: bool = False

    # How often to check the Queued column for new cards
    poll_seconds: int = 15

    # Runtime
    data_dir: Path = Path("/data")
    agent_user: str = ""  # run Claude + checks as this OS user (set in Docker)
    git_author_name: str = "lights-out"
    git_author_email: str = "lights-out@users.noreply.github.com"
    report_port: int = 8080

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

    @classmethod
    def from_env(cls, *, scrub_secrets: bool = True) -> Config:
        cfg = cls(
            trello_api_key=_get("TRELLO_API_KEY", required=True),
            trello_token=_get("TRELLO_TOKEN", required=True),
            trello_board_id=_get("TRELLO_BOARD_ID", required=True),
            github_token=_get("GITHUB_TOKEN", required=True),
            repo_labels=_repo_labels(_get("REPO_LABELS", "")),
            default_repo=_get("DEFAULT_REPO", ""),
            base_branch=_get("BASE_BRANCH", "main"),
            lists=Lists(
                **{name: os.environ.get(f"LIST_{name.upper()}", default) for name, default in Lists().__dict__.items()}
            ),
            planner_model=_get("PLANNER_MODEL", "claude-opus-5-5"),
            coder_model=_get("CODER_MODEL", "claude-sonnet-5-5"),
            reviewer_model=_get("REVIEWER_MODEL", "claude-opus-5-5"),
            max_ticket_usd=float(_get("MAX_TICKET_USD", "10")),
            max_day_usd=float(_get("MAX_DAY_USD", "50")),
            max_fix_attempts=int(_get("MAX_FIX_ATTEMPTS", "3")),
            max_review_rounds=int(_get("MAX_REVIEW_ROUNDS", "2")),
            coder_max_turns=int(_get("CODER_MAX_TURNS", "150")),
            check_command=_get("CHECK_COMMAND", "make check"),
            check_timeout_s=int(_get("CHECK_TIMEOUT_S", "1200")),
            ci_timeout_s=int(_get("CI_TIMEOUT_S", "1800")),
            ci_grace_s=int(_get("CI_GRACE_S", "180")),
            merge_without_ci=_get("MERGE_WITHOUT_CI", "false").lower() == "true",
            poll_seconds=int(_get("POLL_SECONDS", "15")),
            data_dir=Path(_get("DATA_DIR", "/data")),
            agent_user=_get("AGENT_USER", ""),
            git_author_name=_get("GIT_AUTHOR_NAME", "lights-out"),
            git_author_email=_get("GIT_AUTHOR_EMAIL", "lights-out@users.noreply.github.com"),
            report_port=int(_get("REPORT_PORT", "8080")),
        )
        if os.environ.get("GITHUB_REPO"):
            raise ConfigError(
                "GITHUB_REPO was replaced by REPO_LABELS (e.g. REPO_LABELS=my-app=owner/my-app) "
                "and the optional DEFAULT_REPO. Update your .env."
            )
        if not cfg.repo_labels and not cfg.default_repo:
            raise ConfigError("Set REPO_LABELS (label=owner/repo, comma-separated) so cards can be routed")
        if cfg.default_repo and "/" not in cfg.default_repo:
            raise ConfigError("DEFAULT_REPO must look like 'owner/name'")
        if scrub_secrets:
            for name in _ORCHESTRATOR_SECRETS:
                os.environ.pop(name, None)
        for d in (cfg.workspaces_dir, cfg.reports_dir, cfg.runs_dir):
            d.mkdir(parents=True, exist_ok=True)
        return cfg
