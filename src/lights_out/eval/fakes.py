"""In-process stand-ins for Trello and GitHub, so the pipeline runs without either."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Ticket


class MemoryTracker:
    """The TrelloClient methods the pipeline uses, recording comments and moves."""

    def __init__(self, ticket: Ticket):
        self.ticket = ticket
        self.comments: list[str] = []
        self.moves: list[str] = []

    def get_ticket(self, card_id: str) -> Ticket:
        return self.ticket

    def comment(self, card_id: str, text: str) -> None:
        self.comments.append(text)

    def move(self, card_id: str, list_name: str, top: bool = True) -> None:
        self.moves.append(list_name)

    def queued_by(self, card_id: str, list_name: str, ignore: str | None = None) -> str | None:
        return None

    def me(self) -> str:
        return "lights-out"


@dataclass
class LocalPR:
    number: int
    url: str
    head_sha: str


class LocalGitHub:
    """Records the PR, reports CI as green and never merges (the branch stays in origin)."""

    def __init__(self) -> None:
        self.prs: list[dict] = []
        self.pr_comments: list[tuple[int, str]] = []

    def open_or_update_pr(self, **kw) -> LocalPR:
        self.prs.append(kw)
        return LocalPR(number=1, url="eval://pr/1", head_sha=kw.get("head_sha", ""))

    def wait_for_ci(self, sha, timeout_s=0, grace_s=0, on_poll=None):
        if on_poll:
            on_poll()
        return "success", []

    def ci_failure_logs(self, sha) -> list:
        return []

    def comment_pr(self, number: int, body: str) -> None:
        self.pr_comments.append((number, body))

    def merge(self, number: int, title: str = "") -> str:
        return ""

    def delete_branch(self, branch: str) -> None:
        pass
