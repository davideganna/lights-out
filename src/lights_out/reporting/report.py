"""Dashboard: an interactive page served on REPORT_PORT, fed by a JSON snapshot of the run log.

The page (templates/dashboard.html) is static and self-contained: no external scripts, so it works
offline. It polls data.json, which is rewritten after every stage change and every finished card.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .store import Store

DASHBOARD = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


def snapshot(store: Store) -> dict:
    """Everything the dashboard needs, as plain JSON-able data."""
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs": [asdict(r) for r in store.all_runs()],
        "events": store.all_events(),
    }


def write_reports(store: Store, out_dir: Path, day: str | None = None) -> Path:
    """Write data.json and the dashboard page. `day` is accepted for older callers and ignored."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "data.json.tmp"
    tmp.write_text(json.dumps(snapshot(store), separators=(",", ":")))
    os.replace(tmp, out_dir / "data.json")  # atomic: the page never reads a half-written file
    page = out_dir / "index.html"
    shutil.copyfile(DASHBOARD, page)
    return page
