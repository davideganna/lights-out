"""Deterministic risk flags: changes in a diff that weaken tests or CI. Pure, no I/O."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

_TEST_DEF = re.compile(r"^\s*(async\s+)?def test_|(?<![\w.])(it|test|describe)\s*\(")
_SKIP_TOKENS = ("pytest.mark.skip", "pytest.mark.xfail", "unittest.skip", ".skip(", "xit(", "xdescribe(", "t.Skip(")
_SUPPRESSIONS = ("# noqa", "# type: ignore", "eslint-disable")
_RULE_CODE = re.compile(r'^\s*"[A-Z]+\d*"\s*,?\s*$')
_THRESHOLD = re.compile(r"fail_under\s*=\s*([\d.]+)|--cov-fail-under[= ]([\d.]+)")
_TEST_NAMES = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")


@dataclass
class _FileDiff:
    path: str = ""
    deleted: bool = False
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _is_test_file(path: str) -> bool:
    parts = path.split("/")
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pat) for pat in _TEST_NAMES)


def _parse(diff: str) -> list[_FileDiff]:
    files: list[_FileDiff] = []
    cur: _FileDiff | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = _FileDiff()
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            if m:
                cur.path = m.group(2)
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:  # the --stat block before the first file
            continue
        if not in_hunk:
            if line.startswith("@@"):
                in_hunk = True
            elif line.startswith("deleted file mode"):
                cur.deleted = True
            elif line.startswith("rename to "):
                cur.path = line[len("rename to ") :]
            elif line.startswith("--- a/") and cur.deleted:
                cur.path = line[len("--- a/") :]
            elif line.startswith("+++ b/"):
                cur.path = line[len("+++ b/") :]
            continue
        if line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


def _thresholds(lines: list[str]) -> list[float]:
    out = []
    for line in lines:
        for m in _THRESHOLD.finditer(line):
            out.append(float(m.group(1) or m.group(2)))
    return out


def _num(x: float) -> str:
    return str(int(x)) if x == int(x) else str(x)


def find_risk_flags(diff: str) -> list[str]:
    """One readable flag per (rule, file) for changes that weaken tests, CI or checks, in diff order."""
    flags: list[str] = []

    def add(flag: str) -> None:
        if flag not in flags:
            flags.append(flag)

    for f in _parse(diff):
        path = f.path
        if f.deleted:
            if _is_test_file(path):
                add(f"Test file deleted: {path}")
            continue

        removed_defs = sum(1 for ln in f.removed if _TEST_DEF.search(ln))
        added_defs = sum(1 for ln in f.added if _TEST_DEF.search(ln))
        if removed_defs > added_defs:
            add(f"Test definitions removed in {path} ({removed_defs} removed, {added_defs} added)")

        skips = [t for t in _SKIP_TOKENS if any(t in ln for ln in f.added)]
        if skips:
            add(f"Skip/xfail marker added in {path}: {', '.join(f'`{t}`' for t in skips)}")

        if path.startswith(".github/workflows/") or path == "Makefile":
            add(f"CI or check command changed: {path}")

        sup = [t for t in _SUPPRESSIONS if any(t in ln for ln in f.added)]
        if path == "pyproject.toml" and any("ignore" in ln or _RULE_CODE.match(ln) for ln in f.added):
            sup.append("ruff ignore entry")
        if sup:
            add(f"Lint suppression added in {path}: {', '.join(sup)}")

        old, new = _thresholds(f.removed), _thresholds(f.added)
        if old and (not new or max(old) > min(new)):
            target = _num(min(new)) if new else "removed"
            add(f"Coverage threshold lowered in {path}: {_num(max(old))} → {target}")
    return flags


def format_risk_flags(flags: list[str]) -> str:
    """Markdown for prompts and PR bodies: "none" or one bullet per flag."""
    return "\n".join(f"- {f}" for f in flags) if flags else "none"
