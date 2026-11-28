"""Print one version's section of CHANGELOG.md: `python scripts/changelog_section.py <version> [path]`."""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).parents[1] / "CHANGELOG.md"


def section(text: str, version: str) -> str | None:
    """The lines under `## [<version>]` up to the next `## [` (heading excluded), or None if missing."""
    prefix = f"## [{version}]"
    body: list[str] | None = None
    for line in text.splitlines():
        if body is None:
            if line.startswith(prefix):
                body = []
        elif line.startswith("## ["):
            break
        else:
            body.append(line)
    if body is None:
        return None
    return "\n".join(body).strip("\n").strip()


def main(argv: list[str]) -> int:
    if not argv or len(argv) > 2:
        print("usage: changelog_section.py <version> [path]", file=sys.stderr)
        return 2
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    result = section(path.read_text(), argv[0])
    if result is None:
        print(f"No section for version {argv[0]} in CHANGELOG.md", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
