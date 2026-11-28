"""Command line entry point: `lights-out <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .agents.agents import ClaudeAgents, check_claude
from .core.config import Config, ConfigError, effective_settings, ignored_repo_overrides
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
        base_branch=cfg.for_repo(route.repo).base_branch,
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
        agents_factory=lambda transcript_dir, budget, repo_cfg: ClaudeAgents(repo_cfg, transcript_dir, budget),
        workspace_factory=lambda t, route: _workspace(cfg, t.id, route),
        alerter=alerter,
        heartbeat=heartbeat,
    )
    watcher = PRWatcher(
        cfg, trello, store, lambda repo: GitHubClient(cfg.github_token, repo), revise=pipeline.revise, alerter=alerter
    )
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
            full_name, push, default_branch = github.repo_access()
            print(f"GitHub [{label}]: {full_name} (push={push}, default={default_branch})")
            if not push:
                ok = False
                print("  token cannot push to this repository")
            ok = _doctor_branch_protection(github, cfg.for_repo(repo_name).base_branch) and ok
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


def setup_board(cfg: Config) -> tuple[list[str], list[str]]:
    """Create the missing Trello lists and repository labels; returns (lists, labels) created."""
    trello = TrelloClient(cfg.trello_api_key, cfg.trello_token, cfg.trello_board_id)
    return trello.ensure_lists(cfg.lists.all()), trello.ensure_labels(sorted(cfg.repo_labels))


def bootstrap(cfg: Config, label: str, repo: str) -> bool:
    """Give an empty repository its first commit; False if it already has commits."""
    return bootstrap_repository(_workspace(cfg, "_bootstrap", Route(label=label, repo=repo)))


def cmd_init(args: argparse.Namespace) -> int:
    from .setup_wizard import default_services, run_wizard

    return run_wizard(Path(args.out), args.force, services=default_services(setup_board, bootstrap))


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


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval.runner import eval_config, run_eval
    from .eval.tasks import TaskError, load_tasks

    eval_dir = Path.cwd() / "eval"
    sample_repo, tasks_dir = eval_dir / "sample-repo", eval_dir / "tasks"
    if not sample_repo.is_dir() or not tasks_dir.is_dir():
        print(f"Evaluation files not found: expected {sample_repo} and {tasks_dir}", file=sys.stderr)
        return 2
    ids = [t.strip() for t in args.tasks.split(",") if t.strip()] if args.tasks else None
    try:
        tasks = load_tasks(tasks_dir, ids)
    except TaskError as e:
        print(f"Task error: {e}", file=sys.stderr)
        return 2
    if not tasks:
        print(f"No tasks found in {tasks_dir}", file=sys.stderr)
        return 2
    out_dir = Path(args.out) if args.out else Path("eval-results") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        cfg = eval_config(out_dir.resolve())
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    asyncio.run(run_eval(tasks, cfg, sample_repo, repeat=args.repeat, max_usd=args.max_usd))
    print(f"Results: {cfg.data_dir / 'results.json'}")
    return 0


def cmd_config() -> int:
    try:
        rows = effective_settings()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    for key, value, source in rows:
        print(f"{key} = {value}  [{source}]")
    ignored = ignored_repo_overrides(os.environ)
    if ignored is not None:
        print(
            f"Warning: REPO_LABELS is set, so the per-repository overrides in {ignored} are ignored",
            file=sys.stderr,
        )
    return 0


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
    ev = sub.add_parser("eval", help="run the pipeline on sample tickets and check the results (costs real money)")
    ev.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ev.add_argument("--repeat", type=int, default=1, help="runs per task (default 1)")
    ev.add_argument("--out", default="", help="output directory (default eval-results/<UTC timestamp>)")
    ev.add_argument("--max-usd", type=float, default=30.0, help="stop before a task once this much is spent")
    init = sub.add_parser("init", help="interactive setup: check credentials, write .env and lights-out.yaml")
    init.add_argument("--out", default=".", help="directory for .env and lights-out.yaml (default: current)")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    sub.add_parser("config", help="print the effective configuration and where each value comes from")
    sub.add_parser("report", help="regenerate today's HTML report")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "eval":  # needs no Trello or GitHub configuration
        return cmd_eval(args)
    if args.command == "init":  # runs before any configuration exists
        return cmd_init(args)
    if args.command == "config":  # needs no credentials
        return cmd_config()
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
        created, labels = setup_board(cfg)
        print(f"Created lists: {created}" if created else "All lists already exist.")
        print(f"Created labels: {labels}" if labels else "All repository labels already exist.")
        return 0
    if args.command == "bootstrap":
        try:
            route = _route_for(cfg, args.repo)
        except ConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 2
        done = bootstrap(cfg, route.label, route.repo)
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
