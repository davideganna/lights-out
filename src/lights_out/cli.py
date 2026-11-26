"""Command line entry point: `lights-out <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from .agents.agents import ClaudeAgents, check_claude
from .core.config import Config, ConfigError
from .core.routing import Route
from .integrations.alerts import Alerter
from .integrations.github import GitHubClient
from .integrations.trello import TrelloClient
from .orchestration.followup import PRWatcher
from .orchestration.health import Heartbeat
from .orchestration.pipeline import Pipeline
from .orchestration.scheduler import Scheduler, run_until_signal, serve_reports
from .orchestration.workspace import Workspace, bootstrap_repository
from .reporting.report import write_reports
from .reporting.store import Store


def _workspace(cfg: Config, name: str, route: Route) -> Workspace:
    return Workspace(
        cfg.workspaces_dir / name,
        repo_url=route.url,
        base_branch=cfg.base_branch,
        github_token=cfg.github_token,
        agent_user=cfg.agent_user,
        author_name=cfg.git_author_name,
        author_email=cfg.git_author_email,
    )


def build(cfg: Config) -> tuple[TrelloClient, Store, Scheduler]:
    trello = TrelloClient(cfg.trello_api_key, cfg.trello_token, cfg.trello_board_id)
    store = Store(cfg.db_path)
    alerter = Alerter(cfg.alert_webhook_url, cfg.alert_events)
    heartbeat = Heartbeat()
    pipeline = Pipeline(
        cfg,
        trello,
        github_factory=lambda repo: GitHubClient(cfg.github_token, repo),
        store=store,
        agents_factory=lambda transcript_dir, budget: ClaudeAgents(cfg, transcript_dir, budget),
        workspace_factory=lambda t, route: _workspace(cfg, t.id, route),
        alerter=alerter,
        heartbeat=heartbeat,
    )
    watcher = PRWatcher(cfg, trello, store, lambda repo: GitHubClient(cfg.github_token, repo))
    return trello, store, Scheduler(cfg, trello, store, pipeline, heartbeat, alerter=alerter, pr_watcher=watcher)


def _targets(cfg: Config) -> list[tuple[str, str]]:
    """(label, repo) for every repository lights-out may work on."""
    targets = sorted(cfg.repo_labels.items())
    if cfg.default_repo:
        targets.append(("default", cfg.default_repo))
    return targets


def _route_for(cfg: Config, name: str) -> Route:
    """Resolve a bootstrap target given as a label or as owner/repo."""
    if name.lower() in cfg.repo_labels:
        return Route(label=name.lower(), repo=cfg.repo_labels[name.lower()])
    for label, repo in _targets(cfg):
        if repo.lower() == name.lower():
            return Route(label=label, repo=repo)
    raise ConfigError(f"{name!r} is not in REPO_LABELS or DEFAULT_REPO")


def _warn_if_open_board(cfg: Config) -> None:
    if not cfg.allowed_members:
        logging.warning("ALLOWED_MEMBERS is empty: any member of the Trello board can queue work")


def _doctor_branch_protection(github: GitHubClient, branch: str) -> bool:
    """Print the branch protection lines for one repository; return False on a problem."""
    try:
        bp = github.branch_protection(branch)
    except Exception as e:  # noqa: BLE001
        print(f"  branch protection: FAILED: {e}")
        return False
    if not bp.readable:
        print(
            f"  branch protection: WARNING: could not read protection for {branch} "
            "(token lacks Administration: read); check it by hand"
        )
        return True
    if not bp.protected:
        print(f"  branch protection: MISSING on {branch}")
        return False
    ok = True
    if not bp.required_checks:
        ok = False
        print(f"  branch protection: no required status checks on {branch}")
    if bp.force_pushes_allowed:
        ok = False
        print(f"  branch protection: force pushes allowed on {branch}")
    if ok:
        kind = "" if bp.classic else "rulesets; "
        print(f"  branch protection: OK ({kind}required checks: {', '.join(bp.required_checks)})")
    if bp.classic and not bp.enforce_admins:
        print(
            "  branch protection: WARNING: admins are not included (enforce_admins off); "
            "an admin token can bypass the rules"
        )
    if bp.rulesets:
        print(
            "  branch protection: WARNING: rulesets' bypass actors can't be read with this token; "
            "make sure nobody can bypass them"
        )
    return ok


def cmd_doctor(cfg: Config) -> int:
    ok = True
    trello = TrelloClient(cfg.trello_api_key, cfg.trello_token, cfg.trello_board_id)
    try:
        names = trello.list_ids(refresh=True)
        missing = [n for n in cfg.lists.all() if n not in names]
        print(f"Trello: board OK, lists = {sorted(names)}")
        if missing:
            ok = False
            print(f"  missing lists {missing}: run `lights-out setup-board`")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Trello: FAILED: {e}")
    for label, repo_name in _targets(cfg):
        github = GitHubClient(cfg.github_token, repo_name)
        try:
            repo = github._req("GET", f"/repos/{repo_name}")
            perms = repo.get("permissions", {})
            print(f"GitHub [{label}]: {repo['full_name']} (push={perms.get('push')}, default={repo['default_branch']})")
            if not perms.get("push"):
                ok = False
                print("  token cannot push to this repository")
            ok = _doctor_branch_protection(github, cfg.base_branch) and ok
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"GitHub [{label}]: {repo_name} FAILED: {e}")
    if cfg.allowed_members:
        print("Allowed members: " + ", ".join(f"@{m}" for m in cfg.allowed_members))
    else:
        print("Allowed members: WARNING: ALLOWED_MEMBERS is empty, any board member can queue work")
    if cfg.alert_webhook_url:
        print(f"Alerts: webhook set (events: {', '.join(cfg.alert_events)})")
    else:
        print("Alerts: off (ALERT_WEBHOOK_URL is empty)")
    try:
        model = check_claude(cfg)
        print(f"Claude: OK (model {model})")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Claude: FAILED: {e}")
    print("OK" if ok else "Problems found")
    return 0 if ok else 1


def cmd_test_alert(cfg: Config, alerter: Alerter | None = None) -> int:
    if not cfg.alert_webhook_url:
        print("ALERT_WEBHOOK_URL is not set")
        return 1
    alerter = alerter or Alerter(cfg.alert_webhook_url, cfg.alert_events)
    if alerter.post("lights-out: test alert"):
        print("Test alert sent.")
        return 0
    print("Test alert FAILED (see the warning above).")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lights-out")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="daemon: process cards as soon as they are queued, serve reports")
    sub.add_parser("run-once", help="process every Queued card now, then exit")
    sub.add_parser("doctor", help="check Trello, GitHub (incl. branch protection) and Claude")
    sub.add_parser("test-alert", help="send a test message to ALERT_WEBHOOK_URL")
    sub.add_parser("setup-board", help="create missing Trello lists and repository labels")
    boot = sub.add_parser("bootstrap", help="give an empty repository its first commit (CI, Makefile, CLAUDE.md)")
    boot.add_argument("repo", help="repository label or owner/repo (must be in REPO_LABELS or DEFAULT_REPO)")
    sub.add_parser("report", help="regenerate today's HTML report")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return cmd_doctor(cfg)
    if args.command == "test-alert":
        return cmd_test_alert(cfg)
    if args.command == "setup-board":
        trello = TrelloClient(cfg.trello_api_key, cfg.trello_token, cfg.trello_board_id)
        created = trello.ensure_lists(cfg.lists.all())
        print(f"Created lists: {created}" if created else "All lists already exist.")
        labels = trello.ensure_labels(sorted(cfg.repo_labels))
        print(f"Created labels: {labels}" if labels else "All repository labels already exist.")
        return 0
    if args.command == "bootstrap":
        try:
            route = _route_for(cfg, args.repo)
        except ConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 2
        done = bootstrap_repository(_workspace(cfg, "_bootstrap", route))
        print("Pushed the initial commit." if done else "Repository already has commits; nothing to do.")
        return 0

    trello, store, scheduler = build(cfg)
    if args.command == "report":
        print(write_reports(store, cfg.reports_dir, datetime.now().date().isoformat(), runs_dir=cfg.runs_dir))
        return 0
    if args.command == "run-once":
        asyncio.run(run_until_signal(scheduler.run_now(), stop=scheduler.stop))
        return 0
    if args.command == "run":
        _warn_if_open_board(cfg)
        serve_reports(cfg, scheduler.heartbeat)
        asyncio.run(run_until_signal(scheduler.run_forever(), stop=scheduler.stop))
    return 0


if __name__ == "__main__":
    sys.exit(main())
