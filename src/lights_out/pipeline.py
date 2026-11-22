"""One ticket, end to end: triage → implement → checks → review → PR → CI → merge."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import prompts
from .agents import AgentError, AgentsProtocol, BudgetExceeded
from .config import Config
from .github import GitHubClient
from .models import Ticket
from .store import Store
from .trello import TrelloClient
from .workspace import EmptyRepositoryError, Workspace

log = logging.getLogger(__name__)

BOT = "🤖 **lights-out**"


class StageFailed(RuntimeError):
    def __init__(self, stage: str, message: str, detail: str = ""):
        super().__init__(message)
        self.stage = stage
        self.detail = detail


@dataclass
class Outcome:
    status: str  # merged | pr_open | needs_info | failed
    summary: str = ""
    pr_url: str | None = None
    error: str = ""


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        trello: TrelloClient,
        github: GitHubClient,
        store: Store,
        agents_factory: Callable[[Path, float], AgentsProtocol],
        workspace_factory: Callable[[Ticket], Workspace],
    ):
        self.cfg = cfg
        self.trello = trello
        self.github = github
        self.store = store
        self.agents_factory = agents_factory
        self.workspace_factory = workspace_factory

    async def process(self, stub: Ticket, day: str, budget_usd: float) -> Outcome:
        """Process one card. Never raises for ticket-level problems; returns the outcome."""
        lists = self.cfg.lists
        run_id = self.store.start(
            day=day, card_id=stub.id, card_short_id=stub.short_id, title=stub.title, card_url=stub.url
        )
        agents = self.agents_factory(self.cfg.runs_dir / str(run_id), budget_usd)
        ws: Workspace | None = None
        stage = "start"

        def set_stage(name: str) -> None:
            nonlocal stage
            stage = name
            self.store.stage(run_id, name, agents.spent_usd)
            log.info("[#%s] %s", stub.short_id, name)

        try:
            self.trello.comment(stub.id, f"{BOT} picked this up (run #{run_id}).")
            ticket = self.trello.get_ticket(stub.id)

            set_stage("workspace")
            ws = self.workspace_factory(ticket)
            ws.prepare(ticket)

            set_stage("triage")
            triage = await agents.triage(ticket, ws.path)
            if triage.decision != "proceed":
                return self._send_back(ticket, run_id, agents.spent_usd, triage.summary, triage.questions)
            self._move(ticket.id, lists.triaged)
            self.trello.comment(ticket.id, f"{BOT} plan:\n\n{triage.plan}")

            set_stage("implement")
            self._move(ticket.id, lists.in_progress)
            impl = await agents.implement(ticket, triage.plan, ws.path)
            session, summary = impl.session_id, impl.text

            set_stage("checks")
            session = await self._make_checks_pass(agents, ws, session, ticket.id)
            if not ws.has_changes():
                raise StageFailed("implement", "The agent finished without changing any files.")

            for round_no in range(self.cfg.max_review_rounds + 1):
                set_stage(f"review {round_no + 1}")
                self._move(ticket.id, lists.reviewing)
                review = await agents.review(ticket, ws.diff_vs_base(), ws.path)
                if review.approved:
                    break
                issues = "\n".join(f"- {i}" for i in review.issues) or review.summary
                if round_no == self.cfg.max_review_rounds:
                    raise StageFailed(
                        "review",
                        f"The reviewer still requested changes after {self.cfg.max_review_rounds} revision rounds.",
                        issues,
                    )
                set_stage(f"revise {round_no + 1}")
                self._move(ticket.id, lists.in_progress)
                fix = await agents.follow_up(
                    session, prompts.FIX_REVIEW.format(issues=issues, check_command=self.cfg.check_command), ws.path
                )
                session, summary = fix.session_id, fix.text
                session = await self._make_checks_pass(agents, ws, session, ticket.id)

            set_stage("deliver")
            return self._deliver(ticket, ws, run_id, agents.spent_usd, summary, review.summary, triage.plan)

        except EmptyRepositoryError:
            # Not the ticket's fault: put it back (it stays queued until the repo is fixed).
            self.trello.move(stub.id, lists.ready)
            self.store.finish(
                run_id,
                status="interrupted",
                cost_usd=agents.spent_usd,
                error="Repository is empty; run `lights-out bootstrap`.",
            )
            raise
        except Exception as e:  # noqa: BLE001 - every failure is reported on the card
            if isinstance(e, StageFailed):
                stage_name, detail = e.stage, e.detail
            else:
                stage_name, detail = stage, ""
                if not isinstance(e, (AgentError, BudgetExceeded)):
                    log.exception("[#%s] unexpected error", stub.short_id)
            msg = f"{e}" + (f"\n\n{detail}" if detail else "")
            self.trello.comment(
                stub.id,
                f"{BOT} couldn't finish this ticket (stage: {stage_name}).\n\n{msg}"
                f"\n\nMove the card back to **{lists.ready}** to retry after adjusting it.",
            )
            self._move(stub.id, lists.failed or lists.needs_info)
            self.store.finish(run_id, status="failed", cost_usd=agents.spent_usd, error=msg[:4000])
            return Outcome("failed", error=msg)
        finally:
            if ws is not None:
                ws.kill_agent_processes()

    def _move(self, card_id: str, list_name: str) -> None:
        """Move a card; an empty list name means that stage column is disabled."""
        if list_name:
            self.trello.move(card_id, list_name)

    async def _make_checks_pass(self, agents: AgentsProtocol, ws: Workspace, session: str, card_id: str) -> str:
        cmd = self.cfg.check_command
        self._move(card_id, self.cfg.lists.verifying)
        for attempt in range(self.cfg.max_fix_attempts + 1):
            result = ws.run_check(cmd, self.cfg.check_timeout_s)
            if result.ok:
                return session
            if attempt == self.cfg.max_fix_attempts:
                raise StageFailed(
                    "checks",
                    f"`{cmd}` still fails after {attempt} fix attempt(s).",
                    f"```\n{result.output[-3000:]}\n```",
                )
            fix = await agents.follow_up(
                session, prompts.FIX_CHECKS.format(check_command=cmd, output=result.output), ws.path
            )
            session = fix.session_id
        return session

    def _send_back(self, ticket: Ticket, run_id: int, cost: float, summary: str, questions: list[str]) -> Outcome:
        lists = self.cfg.lists
        qs = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
        self.trello.comment(
            ticket.id,
            f"{BOT} needs more information before implementing this.\n\n"
            f"{summary}\n\n{qs}\n\nReply in a comment, then move the card back to "
            f"**{lists.ready}**.",
        )
        self.trello.move(ticket.id, lists.needs_info)
        self.store.finish(run_id, status="needs_info", cost_usd=cost, summary=summary)
        return Outcome("needs_info", summary=summary)

    def _deliver(
        self, ticket: Ticket, ws: Workspace, run_id: int, cost: float, summary: str, review_summary: str, plan: str
    ) -> Outcome:
        lists, cfg = self.cfg.lists, self.cfg
        ws.kill_agent_processes()
        ws.commit(f"{ticket.title} (#{ticket.short_id})\n\nTrello: {ticket.url}")
        sha = ws.push(ticket.branch)
        body = (
            f"Implements Trello card [#{ticket.short_id}]({ticket.url}).\n\n## Summary\n{summary}\n\n"
            f"## Review\n{review_summary}\n\n<details><summary>Plan</summary>\n\n{plan}\n</details>\n\n"
            f"_Opened by lights-out. Merged automatically once CI passes._"
        )
        pr = self.github.open_or_update_pr(
            head=ticket.branch, base=cfg.base_branch, title=ticket.title, body=body, head_sha=sha
        )
        self._move(ticket.id, lists.pr_opened)
        self.trello.comment(ticket.id, f"{BOT} opened {pr.url}; waiting for CI.")
        self.store.stage(run_id, "ci", cost)
        ci, failed = self.github.wait_for_ci(sha, timeout_s=cfg.ci_timeout_s, grace_s=cfg.ci_grace_s)

        if ci == "success" or (ci == "none" and cfg.merge_without_ci):
            self.github.merge(pr.number, title=f"{ticket.title} (#{pr.number})")
            self.github.delete_branch(ticket.branch)
            self.trello.comment(ticket.id, f"{BOT} implemented and merged this: {pr.url}\n\n{summary}")
            self.trello.move(ticket.id, lists.done)
            self.store.finish(run_id, status="merged", cost_usd=cost, summary=summary, pr_url=pr.url)
            ws.cleanup()
            return Outcome("merged", summary=summary, pr_url=pr.url)

        reason = {
            "failure": f"CI failed ({', '.join(failed)})",
            "timeout": f"CI didn't finish within {cfg.ci_timeout_s // 60} minutes",
            "none": "the repository has no CI checks, and MERGE_WITHOUT_CI is off",
        }[ci]
        self.trello.comment(ticket.id, f"{BOT} opened {pr.url} but didn't merge it: {reason}. Please take a look.")
        self._move(ticket.id, lists.failed or lists.needs_info)
        self.store.finish(run_id, status="pr_open", cost_usd=cost, summary=summary, pr_url=pr.url, error=reason)
        return Outcome("pr_open", summary=summary, pr_url=pr.url, error=reason)
