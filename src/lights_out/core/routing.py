"""Route a card to a repository using its labels.

The board decides which repository a card is about: each repository has a
Trello label (configured in REPO_LABELS), and a card must carry exactly one of
them. Only repositories listed in REPO_LABELS can ever be targeted, so an
unrelated label such as "bug" can't send the agent somewhere unexpected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Ticket

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def is_repo(name: str) -> bool:
    """True if `name` looks like "owner/repo"."""
    return bool(_REPO.match(name))


class RoutingError(ValueError):
    """The card can't be routed; the message is written for the person who wrote the card."""


@dataclass(frozen=True)
class Route:
    label: str  # the Trello label that selected the repository ("" when the default was used)
    repo: str  # "owner/name"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}.git"


def parse_repo_labels(spec: str) -> dict[str, str]:
    """Parse "label=owner/repo,other=owner/other" into {label: repo}. Labels match case-insensitively."""
    mapping: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        label, sep, repo = item.partition("=")
        label, repo = label.strip(), repo.strip()
        if not sep or not label or not is_repo(repo):
            raise ValueError(f"REPO_LABELS entry {item!r} must look like 'label=owner/repo'")
        key = label.lower()
        if key in mapping:
            raise ValueError(f"REPO_LABELS lists the label {label!r} twice")
        mapping[key] = repo
    return mapping


def resolve(ticket: Ticket, repo_labels: dict[str, str], default_repo: str = "") -> Route:
    """Pick the repository for a card from its labels."""
    matches = {lbl.lower() for lbl in ticket.labels if lbl.lower() in repo_labels}
    if len(matches) == 1:
        label = matches.pop()
        return Route(label=label, repo=repo_labels[label])
    available = ", ".join(f"**{lbl}**" for lbl in sorted(repo_labels)) or "(none configured)"
    if len(matches) > 1:
        raise RoutingError(
            f"This card has more than one repository label ({', '.join(sorted(matches))}). "
            f"Keep exactly one of: {available}."
        )
    if default_repo:
        return Route(label="", repo=default_repo)
    raise RoutingError(
        f"This card has no repository label, so I don't know which repository it's about. Add one of: {available}."
    )
