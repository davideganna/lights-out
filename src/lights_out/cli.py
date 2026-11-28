"""Command line entry point: `lights-out <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .agents.agents import ClaudeAgents, check_claude
from .core.config import Config, ConfigError, effective_settings
from .core.routing import Route
from .integrations.alerts import Alerter
from .integrations.github import GitHubClient, GitHubError
from .integrations.github_auth import GitHubAppTokenProvider, TokenProvider, provider_from_config
from .integrations.trello import TrelloClient
from .orchestration.followup import PRWatcher
from .orchestration.health import Heartbeat
from .orchestration.pipeline import Pipeline
from .orchestration.scheduler import Scheduler, run_until_signal, serve_reports
from .orchestration.workspace import Workspace, bootstrap_repository
from .reporting.report import write_reports
from .reporting.store import Store

# What a GitHub or GitHub App call can raise: API and transport errors, malformed responses, a bad key.
_GITHUB_ERRORS = (GitHubError, httpx.HTTPError, KeyError, ValueError)


def _workspace(cfg: Config, name: str, route: Route, provider: TokenProvider) -> Workspace:
    return Workspace(
        cfg.workspaces_dir / name,
        repo_url=route.url,
        base_branch=cfg.base_branch,
        github_token=lambda: provider.token_for(route.repo),
        agent_user=cfg.agent_user,
        author_name=cfg.git_author_name,
        author_email=cfg.git_author_email,
    )


def _github_factory(provider: TokenProvider):
    return lambda repo: GitHubClient(lambda: provider.token_for(repo), repo)


def apply_bot_author(cfg: Config, provider: TokenProvider) -> None:
    """With a GitHub App and no GIT_AUTHOR_* setting, commit as the App's bot user."""
    if isinstance(provider, GitHubAppTokenProvider) and not cfg.git_author_configured:
        cfg.git_author_name, cfg.git_author_email = provider.bot_identity()


def _set_bot_author(cfg: Config, provider: TokenProvider) -> bool:
    try:
        apply_bot_author(cfg, provider)
    except _GITHUB_ERRORS as e:
        print(f"GitHub App error: {e}", file=sys.stderr)
        return False
    return True


def build(cfg: Config, provider: TokenProvider) -> tuple[TrelloClient, Store, Scheduler]:
    trello = TrelloClient(cfg.trello_api_key, cfg.trello_token, cfg.trello_board_id)
    store = Store(cfg.db_path)
    alerter = Alerter(cfg.alert_webhook_url, cfg.alert_events)
    heartbeat = Heartbeat()
    pipeline = Pipeline(
        cfg,
        trello,
        github_factory=_github_factory(provider),
        store=store,
        agents_factory=lambda transcript_dir, budget: ClaudeAgents(cfg, transcript_dir, budget),
        workspace_factory=lambda t, route: _workspace(cfg, t.id, route, provider),
        alerter=alerter,
        heartbeat=heartbeat,
    )
    watcher = PRWatcher(cfg, trello, store, _github_factory(provider), revise=pipeline.revise, alerter=alerter)
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


def _doctor_key_file_warnings(cfg: Config) -> None:
    try:
        st = os.stat(cfg.github_app_private_key_file)
    except OSError:
        return
    if st.st_mode & 0o077:
        print(
            f"GitHub App key: WARNING: {cfg.github_app_private_key_file} is accessible to group or others; "
            "chmod 600 it so the agent user can't read it"
        )
    if cfg.agent_user:
        try:
            import pwd

            if pwd.getpwnam(cfg.agent_user).pw_uid == st.st_uid:
                print(
                    f"GitHub App key: WARNING: {cfg.github_app_private_key_file} is owned by {cfg.agent_user}; "
                    "chown it to root so the agent can't read it"
                )
        except (KeyError, ImportError):
            pass


def _doctor_app_repo(provider: GitHubAppTokenProvider, label: str, repo_name: str, branch: str) -> bool:
    """App-mode checks for one repository; return False on a problem."""
    try:
        if not provider.is_installed(repo_name):
            print(f"GitHub [{label}]: {repo_name} App not installed")
            return False
        github = GitHubClient(lambda: provider.token_for(repo_name), repo_name)
        repo = github._req("GET", f"/repos/{repo_name}")
        can_push = provider.permissions(repo_name).get("contents") == "write"
        print(f"GitHub [{label}]: {repo['full_name']} (push={can_push}, default={repo['default_branch']})")
        ok = True
        if not can_push:
            ok = False
            print("  App cannot push to this repository (Contents: write missing)")
        return _doctor_branch_protection(github, branch) and ok
    except _GITHUB_ERRORS as e:
        print(f"GitHub [{label}]: {repo_name} FAILED: {e}")
        return False


def cmd_doctor(cfg: Config, provider: TokenProvider | None = None) -> int:
    ok = True
    provider = provider or provider_from_config(cfg)
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
    app = provider if isinstance(provider, GitHubAppTokenProvider) else None
    if app is None:
        print("GitHub auth: personal token")
    else:
        try:
            print(f"GitHub auth: App {app.slug()} (id {app.app_id})")
        except _GITHUB_ERRORS as e:
            ok = False
            print(f"GitHub auth: App FAILED: {e}")
        _doctor_key_file_warnings(cfg)
    for label, repo_name in _targets(cfg):
        if app is not None:
            ok = _doctor_app_repo(app, label, repo_name, cfg.base_branch) and ok
            continue
        github = _github_factory(provider)(repo_name)
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
    if args.command == "config":  # needs no credentials
        return cmd_config()
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    try:
        provider = provider_from_config(cfg)
    except OSError as e:
        print(f"Configuration error: cannot read the GitHub App key: {e.strerror}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        return cmd_doctor(cfg, provider)
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
        if not _set_bot_author(cfg, provider):
            return 2
        done = bootstrap_repository(_workspace(cfg, "_bootstrap", route, provider))
        print("Pushed the initial commit." if done else "Repository already has commits; nothing to do.")
        return 0

    if args.command in ("run", "run-once") and not _set_bot_author(cfg, provider):
        return 2
    trello, store, scheduler = build(cfg, provider)
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
