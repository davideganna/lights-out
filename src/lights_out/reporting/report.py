"""Dashboard: an interactive page served on REPORT_PORT, fed by a JSON snapshot of the run log.

The page (templates/dashboard.html) is static and self-contained: no external scripts, so it works
offline. It polls data.json, which is rewritten after every stage change and every finished card.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .store import Store

DASHBOARD = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


# Transcript files written by the agents: NN-role.md. The dashboard server serves exactly these names.
TRANSCRIPT_RE = re.compile(r"^[0-9]{2}-[a-z0-9 _-]+\.md$")


def transcripts(runs_dir: Path | None, run_id: int) -> list[str]:
    """Sorted names of a run's transcript files; empty when there is no folder."""
    if runs_dir is None:
        return []
    folder = runs_dir / str(run_id)
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and TRANSCRIPT_RE.fullmatch(p.name))


def snapshot(store: Store, runs_dir: Path | None = None) -> dict:
    """Everything the dashboard needs, as plain JSON-able data."""
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs": [asdict(r) | {"transcripts": transcripts(runs_dir, r.id)} for r in store.all_runs()],
        "events": store.all_events(),
    }


def write_reports(store: Store, out_dir: Path, day: str | None = None, runs_dir: Path | None = None) -> Path:
    """Write data.json and the dashboard page. `day` is accepted for older callers and ignored."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "data.json.tmp"
    tmp.write_text(json.dumps(snapshot(store, runs_dir), separators=(",", ":")))
    os.replace(tmp, out_dir / "data.json")  # atomic: the page never reads a half-written file
    page = out_dir / "index.html"
    shutil.copyfile(DASHBOARD, page)
    return page
