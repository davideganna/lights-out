"""One ticket, end to end: triage → implement → checks → review → PR → CI → merge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..agents import prompts
from ..agents.agents import AgentError, AgentsProtocol, BudgetExceeded
from ..agents.risk import find_risk_flags, format_risk_flags
from ..core.config import Config
from ..core.models import Ticket
from ..core.routing import Route, RoutingError, resolve
from ..integrations.alerts import NullAlerter
from ..integrations.github import GitHubClient, GitHubError
from ..integrations.trello import TrelloClient
from ..reporting.report import write_reports
from ..reporting.store import Store
from .policy import merge_policy
from .workspace import EmptyRepositoryError, Workspace, WorkspaceError

log = logging.getLogger(__name__)

BOT = "🤖 **lights-out**"
MAX_REBASES = 2


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


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
        github_factory: Callable[[str], GitHubClient],
        store: Store,
        agents_factory: Callable[[Path, float], AgentsProtocol],
        workspace_factory: Callable[[Ticket, Route], Workspace],
        alerter=None,
    ):
        self.alerter = alerter or NullAlerter()
        self.cfg = cfg
        self.trello = trello
        self.github_factory = github_factory
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
        pr_url: str | None = None  # set once the PR exists, so a later failure alert can link it

        def on_pr(url: str) -> None:
            nonlocal pr_url
            pr_url = url

        def set_stage(name: str) -> None:
            nonlocal stage
            stage = name
            self.store.stage(run_id, name, agents.spent_usd)
            self._refresh_dashboard()
            log.info("[#%s] %s", stub.short_id, name)

        try:
            ticket = self.trello.get_ticket(stub.id)
            allowed = self.cfg.allowed_members
            if allowed:
                me = self.trello.me()
                who = self.trello.queued_by(stub.id, lists.ready, me)
                if who is None or who.lower() not in allowed:
                    reason = "Only cards queued by allowed members are processed; " + (
                        f"queued by @{who}" if who else "queued by an unknown member"
                    )
                    return self._send_back(ticket, run_id, agents.spent_usd, reason, [])
                ticket.comments = [
                    c for c in ticket.comments if c.username.lower() in allowed or c.username.lower() == me
                ]
            try:
                route = resolve(ticket, self.cfg.repo_labels, self.cfg.default_repo)
            except RoutingError as e:
                return self._send_back(ticket, run_id, agents.spent_usd, str(e), [])
            self.store.set_repo(run_id, route.repo)
            self.trello.comment(stub.id, f"{BOT} picked this up (run #{run_id}), working on `{route.repo}`.")
            github = self.github_factory(route.repo)

            set_stage("workspace")
            ws = self.workspace_factory(ticket, route)
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

            session = await self._make_checks_pass(agents, ws, session, ticket.id, set_stage)
            if not ws.has_changes():
                raise StageFailed("implement", "The agent finished without changing any files.")

            for round_no in range(self.cfg.max_review_rounds + 1):
                set_stage(f"review {round_no + 1}")
                self._move(ticket.id, lists.reviewing)
                # Scan the untruncated diff so padding can't hide a deletion from the flags.
                flags = find_risk_flags(ws.diff_vs_base(limit=10**9))
                review = await agents.review(ticket, ws.diff_vs_base(), ws.path, risk_flags=flags)
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
                session = await self._make_checks_pass(agents, ws, session, ticket.id, set_stage)

            set_stage("deliver")
            return await self._deliver(
                ticket,
                ws,
                github,
                run_id,
                agents,
                session,
                set_stage,
                summary,
                review.summary,
                triage.plan,
                flags,
                on_pr,
            )

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
        except asyncio.CancelledError:
            # Service shutting down: put the card back and record the run, then keep unwinding.
            try:
                self.trello.comment(
                    stub.id,
                    f"{BOT} was stopped (service shutting down) during stage '{stage}'; "
                    f"the card is back in **{lists.ready}** and will be retried.",
                )
                self.trello.move(stub.id, lists.ready)
            except Exception:  # noqa: BLE001
                log.exception("[#%s] could not put the card back during shutdown", stub.short_id)
            self.store.finish(
                run_id,
                status="interrupted",
                cost_usd=agents.spent_usd,
                error=f"Interrupted during stage '{stage}' (shutdown).",
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
            self.alerter.send(
                "failed",
                f'lights-out: card #{stub.short_id} "{stub.title}" failed at stage '
                f"{stage_name}: {_first_line(str(e))} — {stub.url}" + (f" PR: {pr_url}" if pr_url else ""),
            )
            return Outcome("failed", error=msg)
        finally:
            if ws is not None:
                ws.kill_agent_processes()

    def _move(self, card_id: str, list_name: str) -> None:
        """Move a card; an empty list name means that stage column is disabled."""
        if list_name:
            self.trello.move(card_id, list_name)

    def _refresh_dashboard(self) -> None:
        """Rewrite the dashboard data so the page follows a card live. Never fails a card."""
        try:
            write_reports(self.store, self.cfg.reports_dir, runs_dir=self.cfg.runs_dir)
        except Exception:  # noqa: BLE001
            log.exception("Could not refresh the dashboard data")

    async def _make_checks_pass(
        self,
        agents: AgentsProtocol,
        ws: Workspace,
        session: str,
        card_id: str,
        set_stage: Callable[[str], None],
    ) -> str:
        cmd = self.cfg.check_command
        self._move(card_id, self.cfg.lists.verifying)
        for attempt in range(self.cfg.max_fix_attempts + 1):
            set_stage("checks")
            result = ws.run_check(cmd, self.cfg.check_timeout_s)
            if result.ok:
                return session
            if attempt == self.cfg.max_fix_attempts:
                raise StageFailed(
                    "checks",
                    f"`{cmd}` still fails after {attempt} fix attempt(s).",
                    f"```\n{result.output[-3000:]}\n```",
                )
            set_stage("fix")
            fix = await agents.follow_up(
                session, prompts.FIX_CHECKS.format(check_command=cmd, output=result.output), ws.path
            )
            session = fix.session_id
        return session

    def _send_back(self, ticket: Ticket, run_id: int, cost: float, summary: str, questions: list[str]) -> Outcome:
        lists = self.cfg.lists
        qs = "".join(f"{i}. {q}\n" for i, q in enumerate(questions, 1))
        reply = "Reply in a comment, then move" if questions else "Fix the card, then move"
        self.trello.comment(
            ticket.id,
            f"{BOT} needs more information before implementing this.\n\n{summary}\n\n"
            + (f"{qs}\n" if qs else "")
            + f"{reply} the card back to **{lists.ready}**.",
        )
        self.trello.move(ticket.id, lists.needs_info)
        self.store.finish(run_id, status="needs_info", cost_usd=cost, summary=summary)
        self.alerter.send(
            "needs_info",
            f'lights-out: card #{ticket.short_id} "{ticket.title}" moved to Needs Info: '
            f"{_first_line(summary)} — {ticket.url}",
        )
        return Outcome("needs_info", summary=summary)

    async def _deliver(
        self,
        ticket: Ticket,
        ws: Workspace,
        github: GitHubClient,
        run_id: int,
        agents: AgentsProtocol,
        session: str,
        set_stage: Callable[[str], None],
        summary: str,
        review_summary: str,
        plan: str,
        risk_flags: list[str],
        on_pr: Callable[[str], None] = lambda url: None,
    ) -> Outcome:
        lists, cfg = self.cfg.lists, self.cfg
        ci_fixes = 0
        rebases = 0
        merge_retry_used = False
        while True:
            ws.kill_agent_processes()
            ws.commit(f"{ticket.title} (#{ticket.short_id})\n\nTrello: {ticket.url}")
            reasons = merge_policy(ws.changed_files(), cfg)
            sha = ws.push(ticket.branch)
            approval = "## Needs human approval\n" + "".join(f"- {r}\n" for r in reasons) + "\n" if reasons else ""
            footer = (
                "_Opened by lights-out. Needs human approval before merging._"
                if reasons
                else "_Opened by lights-out. Merged automatically once CI passes._"
            )
            body = (
                f"Implements Trello card [#{ticket.short_id}]({ticket.url}).\n\n## Summary\n{summary}\n\n"
                f"## Review\n{review_summary}\n\n## Risk flags\n{format_risk_flags(risk_flags)}\n\n"
                f"{approval}"
                f"<details><summary>Plan</summary>\n\n{plan}\n</details>\n\n"
                f"{footer}"
            )
            pr = github.open_or_update_pr(
                head=ticket.branch, base=cfg.base_branch, title=ticket.title, body=body, head_sha=sha
            )
            on_pr(pr.url)
            self._move(ticket.id, lists.pr_opened)
            self.trello.comment(ticket.id, f"{BOT} opened {pr.url}; waiting for CI.")
            set_stage("ci")
            ci, failed = github.wait_for_ci(sha, timeout_s=cfg.ci_timeout_s, grace_s=cfg.ci_grace_s)
            cost = agents.spent_usd

            if ci == "success" or (ci == "none" and cfg.merge_without_ci):
                if reasons:
                    bullets = "".join(f"- {r}\n" for r in reasons)
                    github.comment_pr(
                        pr.number,
                        f"{BOT} did not merge this: it needs human approval.\n\n{bullets}\n"
                        "A human must review and merge this PR.",
                    )
                    self.trello.comment(
                        ticket.id,
                        f"{BOT} opened {pr.url} and CI passed, but it needs human approval before merging:\n\n"
                        f"{bullets}\nA human must review and merge it.",
                    )
                    self._move(ticket.id, lists.pr_opened)
                    reason = "needs human approval: " + "; ".join(reasons)
                    self.store.finish(
                        run_id, status="pr_open", cost_usd=cost, summary=summary, pr_url=pr.url, error=reason
                    )
                    self._alert_pr_open(ticket, reason, pr.url)
                    return Outcome("pr_open", summary=summary, pr_url=pr.url, error=reason)
                if ws.base_moved():
                    session, summary, review_summary, risk_flags, rebases = await self._rebase(
                        ticket, ws, agents, session, set_stage, summary, review_summary, risk_flags, rebases
                    )
                    continue
                try:
                    github.merge(pr.number, title=f"{ticket.title} (#{pr.number})")
                except GitHubError as e:
                    if e.status in (405, 409) and not merge_retry_used:
                        merge_retry_used = True
                        session, summary, review_summary, risk_flags, rebases = await self._rebase(
                            ticket, ws, agents, session, set_stage, summary, review_summary, risk_flags, rebases
                        )
                        continue
                    raise
                github.delete_branch(ticket.branch)
                self.trello.comment(ticket.id, f"{BOT} implemented and merged this: {pr.url}\n\n{summary}")
                self.trello.move(ticket.id, lists.done)
                self.store.finish(run_id, status="merged", cost_usd=cost, summary=summary, pr_url=pr.url)
                ws.cleanup()
                return Outcome("merged", summary=summary, pr_url=pr.url)

            if ci == "failure" and ci_fixes < cfg.max_ci_fix_attempts:
                ci_fixes += 1
                set_stage(f"ci fix {ci_fixes}")
                self._move(ticket.id, lists.in_progress)
                logs = "\n\n".join(f"### {name}\n```\n{text}\n```" for name, text in github.ci_failure_logs(sha))
                fix = await agents.follow_up(
                    session, prompts.FIX_CI.format(logs=logs, check_command=cfg.check_command), ws.path
                )
                session, summary = fix.session_id, fix.text
                session = await self._make_checks_pass(agents, ws, session, ticket.id, set_stage)
                set_stage(f"ci review {ci_fixes}")
                self._move(ticket.id, lists.reviewing)
                flags = find_risk_flags(ws.diff_vs_base(limit=10**9))
                review = await agents.review(ticket, ws.diff_vs_base(), ws.path, risk_flags=flags)
                if not review.approved:
                    issues = "\n".join(f"- {i}" for i in review.issues) or review.summary
                    raise StageFailed("review", "The reviewer rejected the CI fix.", issues)
                review_summary, risk_flags = review.summary, flags
                set_stage("deliver")
                continue

            reason = {
                "failure": f"CI failed ({', '.join(failed)})"
                + (f" after {ci_fixes} CI fix attempt(s)" if cfg.max_ci_fix_attempts > 0 else ""),
                "timeout": f"CI didn't finish within {cfg.ci_timeout_s // 60} minutes",
                "none": "the repository has no CI checks, and MERGE_WITHOUT_CI is off",
            }[ci]
            self.trello.comment(ticket.id, f"{BOT} opened {pr.url} but didn't merge it: {reason}. Please take a look.")
            self._move(ticket.id, lists.failed or lists.needs_info)
            self.store.finish(run_id, status="pr_open", cost_usd=cost, summary=summary, pr_url=pr.url, error=reason)
            self._alert_pr_open(ticket, reason, pr.url)
            return Outcome("pr_open", summary=summary, pr_url=pr.url, error=reason)

    def _alert_pr_open(self, ticket: Ticket, reason: str, pr_url: str) -> None:
        self.alerter.send(
            "pr_open",
            f'lights-out: card #{ticket.short_id} "{ticket.title}": PR opened but not merged ({reason}) '
            f"— {ticket.url} PR: {pr_url}",
        )

    async def _rebase(
        self,
        ticket: Ticket,
        ws: Workspace,
        agents: AgentsProtocol,
        session: str,
        set_stage: Callable[[str], None],
        summary: str,
        review_summary: str,
        risk_flags: list[str],
        rebases: int,
    ) -> tuple[str, str, str, list[str], int]:
        """Rebase onto the moved base branch and re-check. Returns the updated state (and rebase count)."""
        if rebases >= MAX_REBASES:
            raise StageFailed("rebase", f"The base branch kept moving; gave up after {MAX_REBASES} rebases.")
        rebases += 1
        set_stage("rebase")
        ws.kill_agent_processes()
        try:
            conflicts = ws.rebase_onto_base()
        except WorkspaceError as e:
            ws.abort_rebase()
            raise StageFailed("rebase", f"The rebase failed: {e}") from e
        if conflicts:
            fix = await agents.follow_up(
                session,
                prompts.RESOLVE_CONFLICTS.format(
                    files="\n".join(f"- {f}" for f in conflicts), check_command=self.cfg.check_command
                ),
                ws.path,
            )
            session = fix.session_id
            ws.kill_agent_processes()
            left = ws.conflict_markers(conflicts)
            if left:
                ws.abort_rebase()
                raise StageFailed(
                    "rebase",
                    "Conflict markers remain after the coder's resolution.",
                    "\n".join(f"- {f}" for f in left),
                )
            try:
                again = ws.continue_rebase()
            except WorkspaceError as e:
                ws.abort_rebase()
                raise StageFailed("rebase", f"Could not continue the rebase: {e}") from e
            if again:
                ws.abort_rebase()
                raise StageFailed(
                    "rebase",
                    "The rebase hit conflicts again on a later commit.",
                    "\n".join(f"- {f}" for f in again),
                )
        session = await self._make_checks_pass(agents, ws, session, ticket.id, set_stage)
        if conflicts:
            set_stage("rebase review")
            self._move(ticket.id, self.cfg.lists.reviewing)
            flags = find_risk_flags(ws.diff_vs_base(limit=10**9))
            review = await agents.review(ticket, ws.diff_vs_base(), ws.path, risk_flags=flags)
            if not review.approved:
                issues = "\n".join(f"- {i}" for i in review.issues) or review.summary
                raise StageFailed("review", "The reviewer rejected the conflict resolution.", issues)
            review_summary, risk_flags = review.summary, flags
        else:
            risk_flags = find_risk_flags(ws.diff_vs_base(limit=10**9))
        set_stage("deliver")
        return session, summary, review_summary, risk_flags, rebases
