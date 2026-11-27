"""Tracker-independent data models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Comment:
    author: str
    text: str
    date: str
    username: str = ""


@dataclass
class PRFeedback:
    """A human comment on a PR that asks for changes."""

    kind: str  # feedback-comment | feedback-review | feedback-line
    id: str
    user: str
    body: str
    path: str | None = None
    line: int | None = None
    at: str = ""

    def quote(self) -> str:
        where = ""
        if self.path:
            where = f" ({self.path}:{self.line})" if self.line else f" ({self.path})"
        return f"@{self.user}{where}: {self.body.strip()}"


@dataclass
class Ticket:
    id: str
    short_id: str
    title: str
    description: str
    url: str
    labels: list[str] = field(default_factory=list)
    checklists: list[str] = field(default_factory=list)  # "[x] item" lines
    comments: list[Comment] = field(default_factory=list)  # oldest first

    @property
    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return s[:40].rstrip("-") or "ticket"

    @property
    def branch(self) -> str:
        return f"agent/{self.short_id}-{self.slug}"

    def as_prompt(self) -> str:
        parts = [f"# Ticket #{self.short_id}: {self.title}", f"URL: {self.url}"]
        if self.labels:
            parts.append("Labels: " + ", ".join(self.labels))
        parts.append("\n## Description\n" + (self.description.strip() or "(empty)"))
        if self.checklists:
            parts.append("\n## Checklist / acceptance criteria\n" + "\n".join(self.checklists))
        if self.comments:
            convo = "\n\n".join(f"**{c.author}** ({c.date}):\n{c.text}" for c in self.comments)
            parts.append("\n## Comment thread (oldest first)\n" + convo)
        return "\n".join(parts)
