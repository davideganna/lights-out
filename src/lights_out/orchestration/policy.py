"""Merge policy: decide whether a change needs a human before it is merged. Pure, no I/O."""

from __future__ import annotations

import re
from functools import lru_cache

from ..core.config import Config
from .workspace import FileChange


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    segments = pattern.split("/")
    out = ""
    last = len(segments) - 1
    for i, seg in enumerate(segments):
        if seg == "**":
            # zero or more whole segments (including their trailing slash)
            out += ".*" if i == last else "(?:[^/]+/)*"
            continue
        out += "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in seg)
        if i != last:
            out += "/"
    return re.compile(out)


def pattern_matches(pattern: str, path: str) -> bool:
    """Glob match anchored at the repository root. `*`/`?` stay within a segment; `**` spans segments."""
    return _compile(pattern).fullmatch(path) is not None


def merge_policy(changes: list[FileChange], cfg: Config) -> list[str]:
    """Reasons a human must approve this change (empty = safe to auto-merge)."""
    reasons: list[str] = []
    for change in changes:
        for pattern in cfg.human_review_paths:
            if pattern_matches(pattern, change.path):
                reasons.append(f"touches {change.path} (matches {pattern})")
                break
    limit = cfg.max_auto_merge_lines
    if limit > 0:
        total = sum(c.added + c.deleted for c in changes)
        if total > limit:
            reasons.append(f"{total} changed lines (limit {limit})")
    return reasons
