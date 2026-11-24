"""Dashboard server: transcript route, path resolver and the transcript list in data.json."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from lights_out.core.config import Config
from lights_out.orchestration.scheduler import build_server, resolve_transcript
from lights_out.reporting.report import snapshot, transcripts, write_reports
from lights_out.reporting.store import Store


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    (d / "7").mkdir(parents=True)
    (d / "7" / "01-triage.md").write_text("# triage\nhello")
    return d


def test_resolve_valid(runs_dir):
    assert resolve_transcript(runs_dir, "/runs/7/01-triage.md") == (runs_dir / "7" / "01-triage.md").resolve()
    assert resolve_transcript(runs_dir, "/runs/7/01-triage.md?x=1#y") is not None


@pytest.mark.parametrize(
    "raw",
    [
        "/runs/7/../7/01-triage.md",
        "/runs/7/%2e%2e%2f01-triage.md",
        "/runs/7%2F01-triage.md",
        "/runs/7/%2e%2e/x.md",
        "/runs/abc/01-triage.md",
        "/runs/7/triage.md",
        "/runs/7/01-Triage.MD",
        "/runs/7/01-triage.txt",
        "/runs/7/02-fix.md",
        "/runs/7/",
        "/runs",
    ],
)
def test_resolve_rejects(runs_dir, raw):
    assert resolve_transcript(runs_dir, raw) is None


def test_http_server(tmp_path, runs_dir):
    cfg = Config(
        trello_api_key="k", trello_token="t", trello_board_id="b", github_token="g", data_dir=tmp_path, report_port=0
    )
    cfg.reports_dir.mkdir(parents=True)
    (cfg.reports_dir / "index.html").write_text("<html>dash</html>")
    (cfg.reports_dir / "data.json").write_text("{}")
    server = build_server(cfg, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type"), resp.read()
        finally:
            conn.close()

    try:
        status, ctype, body = get("/runs/7/01-triage.md")
        assert status == 200 and ctype == "text/plain; charset=utf-8" and body == b"# triage\nhello"
        assert get("/runs/7/01-triage.md", "HEAD")[0] == 200
        for bad in (
            "/runs/7/../../reports/data.json",
            "/runs/7/%2e%2e/x.md",
            "/runs/7%2F01-triage.md",
            "/runs/x/01-triage.md",
            "/runs/7/notes.txt",
        ):
            assert get(bad)[0] == 404, bad
        assert get("/")[0] == 200
        assert get("/data.json")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_snapshot_transcripts(tmp_path):
    store = Store(tmp_path / "s.db")
    a = store.start(day="n", card_id="c", card_short_id="1", title="a", card_url="u")
    b = store.start(day="n", card_id="d", card_short_id="2", title="b", card_url="u")
    runs = tmp_path / "runs"
    (runs / str(a)).mkdir(parents=True)
    for name in ("02-implement.md", "01-triage.md", "notes.txt"):
        (runs / str(a) / name).write_text("x")
    write_reports(store, tmp_path / "out", runs_dir=runs)
    data = json.loads((tmp_path / "out" / "data.json").read_text())
    by_id = {r["id"]: r for r in data["runs"]}
    assert by_id[a]["transcripts"] == ["01-triage.md", "02-implement.md"]
    assert by_id[b]["transcripts"] == []
    assert all(r["transcripts"] == [] for r in snapshot(store)["runs"])
    assert transcripts(None, a) == []
