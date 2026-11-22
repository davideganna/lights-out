"""Command line entry point: `lights-out <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from .agents import ClaudeAgents
from .config import Config, ConfigError
from .github import GitHubClient
from .pipeline import Pipeline
from .report import write_reports
from .scheduler import Scheduler, serve_reports
from .store import Store
from .trello import TrelloClient
from .workspace import Workspace, bootstrap_repository


def _workspace(cfg: Config, name: str) -> Workspace:
    return Workspace(
        cfg.workspaces_dir / name,
        repo_url=cfg.repo_url,
        base_branch=cfg.base_branch,
        github_token=cfg.github_token,
        agent_user=cfg.agent_user,
        author_name=cfg.git_author_name,
        author_email=cfg.git_author_email,
    )


def build(cfg: Config) -> tuple[TrelloClient, Store, Scheduler]:
    trello = TrelloClient(cfg.trello_api_key, cfg.trello_token, cfg.trello_board_id)
    github = GitHubClient(cfg.github_token, cfg.github_repo)
    store = Store(cfg.db_path)
    pipeline = Pipeline(
        cfg,
        trello,
        github,
        store,
        agents_factory=lambda transcript_dir, budget: ClaudeAgents(cfg, transcript_dir, budget),
        workspace_factory=lambda t: _workspace(cfg, t.id),
    )
    return trello, store, Scheduler(cfg, trello, store, pipeline)


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
    github = GitHubClient(cfg.github_token, cfg.github_repo)
    try:
        repo = github._req("GET", f"/repos/{cfg.github_repo}")
        perms = repo.get("permissions", {})
        print(f"GitHub: {repo['full_name']} (push={perms.get('push')}, default branch={repo['default_branch']})")
        if not perms.get("push"):
            ok = False
            print("  token cannot push to this repository")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"GitHub: FAILED: {e}")
    print("OK" if ok else "Problems found")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lights-out")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="daemon: process cards as soon as they are queued, serve reports")
    sub.add_parser("run-once", help="process every Queued card now, then exit")
    sub.add_parser("doctor", help="check Trello and GitHub credentials")
    sub.add_parser("setup-board", help="create missing Trello lists")
    sub.add_parser("bootstrap", help="give an empty repository its first commit (CI, Makefile, CLAUDE.md)")
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
    if args.command == "setup-board":
        trello = TrelloClient(cfg.trello_api_key, cfg.trello_token, cfg.trello_board_id)
        created = trello.ensure_lists(cfg.lists.all())
        print(f"Created lists: {created}" if created else "All lists already exist.")
        return 0
    if args.command == "bootstrap":
        done = bootstrap_repository(_workspace(cfg, "_bootstrap"))
        print("Pushed the initial commit." if done else "Repository already has commits; nothing to do.")
        return 0

    trello, store, scheduler = build(cfg)
    if args.command == "report":
        print(write_reports(store, cfg.reports_dir, datetime.now().date().isoformat()))
        return 0
    if args.command == "run-once":
        asyncio.run(scheduler.run_now())
        return 0
    if args.command == "run":
        serve_reports(cfg)
        asyncio.run(scheduler.run_forever())
    return 0


if __name__ == "__main__":
    sys.exit(main())
