"""Follow up on PRs left open for a human: sync hand merges/closes and merge on approval."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable

import httpx

from ..core.config import Config
from ..core.models import PRFeedback
from ..integrations.github import GitHubClient, GitHubError, PRState
from ..integrations.trello import TrelloClient, TrelloError
from ..reporting.report import write_reports
from ..reporting.store import Run, Store
from .pipeline import BOT, MAX_ATTEMPTS_PER_DAY, Outcome, today

log = logging.getLogger(__name__)

COMMAND = "/lights-out merge"

# Failures expected while following up on one PR: API errors and outages, an unexpected response
# shape, or the local database. They are logged and the PR is retried at the next poll.
FOLLOWUP_ERRORS = (GitHubError, TrelloError, httpx.HTTPError, KeyError, TypeError, ValueError, sqlite3.Error)


class PRWatcher:
    def __init__(
        self,
        cfg: Config,
        trello: TrelloClient,
        store: Store,
        github_factory: Callable[[str], GitHubClient],
        clock: Callable[[], float] = time.monotonic,
        revise: Callable[[Run, PRState, list[PRFeedback]], Awaitable[Outcome]] | None = None,
    ):
        self.revise = revise
        self.cfg = cfg
        self.trello = trello
        self.store = store
        self.github_factory = github_factory
        self.clock = clock
        self._last: float | None = None
        self._revised = False

    async def maybe_check(self) -> None:
        if self.store.running():
            return
        now = self.clock()
        if self._last is not None and now - self._last < self.cfg.pr_poll_seconds:
            return
        self._last = now
        await self.check_all()

    async def check_all(self) -> None:
        allowed = set(self.cfg.repo_labels.values())
        if self.cfg.default_repo:
            allowed.add(self.cfg.default_repo)
        changed = False
        for run in self.store.open_prs():
            if run.repo not in allowed:
                continue
            m = re.search(r"/pull/(\d+)", run.pr_url or "")
            if not m:
                continue
            try:
                self._revised = False
                if await self._check(run, int(m.group(1))):
                    changed = True
                if self._revised:
                    break  # a revision ran agents: stop here so the queue gets a turn
            except FOLLOWUP_ERRORS:  # one bad PR must not stop the others
                log.exception("Could not follow up on %s", run.pr_url)
        if changed:
            try:
                write_reports(self.store, self.cfg.reports_dir, runs_dir=self.cfg.runs_dir)
            except (OSError, sqlite3.Error):
                log.exception("Could not write reports")

    def _move(self, card_id: str, list_name: str) -> None:
        if list_name:
            self.trello.move(card_id, list_name)

    async def _check(self, run: Run, n: int) -> bool:
        """Returns True if the run changed."""
        lists = self.cfg.lists
        gh = self.github_factory(run.repo)
        pr = gh.get_pr(n)
        if pr.merged:
            who = pr.merged_by or "someone"
            self.trello.comment(run.card_id, f"{BOT} {pr.url} was merged by @{who}.")
            self._move(run.card_id, lists.done)
            self.store.resolve_pr(run.id, status="merged", event="merged_by_human")
            return True
        if pr.state == "closed":
            self.trello.comment(run.card_id, f"{BOT} {pr.url} was closed without merging.")
            self._move(run.card_id, lists.failed or lists.needs_info)
            self.store.resolve_pr(run.id, status="failed", event="pr_closed", error="PR closed without merging")
            return True
        return await self._check_open(run, gh, pr)

    def _find_approval(self, run: Run, gh: GitHubClient, pr: PRState) -> tuple[str, str, str] | None:
        """(kind, item id, user) of the first unhandled approval, or None."""
        t = gh.head_commit_time(pr.head_sha)
        for c in gh.issue_comments(pr.number):
            if (
                c["body"].strip() == COMMAND
                and not c["body"].startswith(BOT)
                and c["created_at"] > t
                and not self.store.is_handled(run.repo, "comment", str(c["id"]))
                and gh.can_write(c["user"])
            ):
                return "comment", str(c["id"]), c["user"]
        for r in gh.reviews(pr.number):
            if (
                r["state"] == "APPROVED"
                and r["user"] != pr.author
                and r["commit_id"] == pr.head_sha
                and not self.store.is_handled(run.repo, "review", str(r["id"]))
                and gh.can_write(r["user"])
            ):
                return "review", str(r["id"]), r["user"]
        return None

    def _collect_feedback(self, run: Run, gh: GitHubClient, pr: PRState) -> list[PRFeedback]:
        """Unhandled feedback from writers posted after the PR's latest commit."""
        t = gh.head_commit_time(pr.head_sha)
        candidates: list[PRFeedback] = []
        for c in gh.issue_comments(pr.number):
            if c["created_at"] > t:
                candidates.append(
                    PRFeedback("feedback-comment", str(c["id"]), c["user"], c["body"], at=c["created_at"])
                )
        for c in gh.review_comments(pr.number):
            if c["created_at"] > t:
                candidates.append(
                    PRFeedback(
                        "feedback-line",
                        str(c["id"]),
                        c["user"],
                        c["body"],
                        c.get("path"),
                        c.get("line"),
                        c["created_at"],
                    )
                )
        for r in gh.reviews(pr.number):
            if r["state"] in ("CHANGES_REQUESTED", "COMMENTED") and r["submitted_at"] > t:
                candidates.append(
                    PRFeedback("feedback-review", str(r["id"]), r["user"], r["body"], at=r["submitted_at"])
                )
        writers: dict[str, bool] = {}
        out: list[PRFeedback] = []
        for f in candidates:
            body = f.body.strip()
            if not body or body.startswith(BOT) or body == COMMAND:
                continue
            if self.store.is_handled(run.repo, f.kind, f.id):
                continue
            if f.user not in writers:
                writers[f.user] = gh.can_write(f.user)
            if writers[f.user]:
                out.append(f)
        out.sort(key=lambda f: f.at)
        return out

    async def _check_open(self, run: Run, gh: GitHubClient, pr: PRState) -> bool:
        found = self._find_approval(run, gh, pr)
        if found is None:
            if self.revise is None:
                return False
            feedback = self._collect_feedback(run, gh, pr)
            if not feedback:
                return False
            n = pr.number
            if self.store.revision_count(run.repo, n) >= self.cfg.max_pr_revisions:
                if not self.store.is_handled(run.repo, "revision-limit", str(n)):
                    gh.comment_pr(
                        n,
                        f"{BOT} has already revised this PR {self.cfg.max_pr_revisions} times "
                        f"(MAX_PR_REVISIONS), so a human needs to take over.",
                    )
                    self.store.mark_handled(run.repo, "revision-limit", str(n), run.id)
                return False
            day = today()
            left = self.cfg.max_day_usd - self.store.day_cost(day)
            if left < 0.5:
                log.info("Daily budget reached; not revising %s until tomorrow", pr.url)
                return False
            if self.store.attempts(run.card_id, day) >= MAX_ATTEMPTS_PER_DAY:
                log.info(
                    "Card #%s already attempted %d times today; not revising", run.card_short_id, MAX_ATTEMPTS_PER_DAY
                )
                return False
            await self.revise(run, pr, feedback)
            self._revised = True
            return True
        kind, item_id, user = found
        self.store.mark_handled(run.repo, kind, item_id, run.id)
        state, failed = gh.ci_status(pr.head_sha)
        if not (state == "success" or (state == "none" and self.cfg.merge_without_ci)):
            names = f" (failed: {', '.join(failed)})" if failed else ""
            gh.comment_pr(
                pr.number,
                f"{BOT} did not merge this: CI on {pr.head_sha[:7]} is {state}{names}. "
                f"Post `{COMMAND}` again once CI is green.",
            )
            return False
        try:
            gh.merge(pr.number, title=f"{pr.title} (#{pr.number})")
        except GitHubError as e:
            gh.comment_pr(pr.number, f"{BOT} did not merge this: {e}")
            return False
        gh.delete_branch(pr.head_ref)
        gh.comment_pr(pr.number, f"{BOT} merged this on @{user}'s approval.")
        self.trello.comment(run.card_id, f"{BOT} merged {pr.url} on @{user}'s approval.\n\n{run.summary}")
        self._move(run.card_id, self.cfg.lists.done)
        self.store.resolve_pr(run.id, status="merged", event="merged_on_approval")
        return True
