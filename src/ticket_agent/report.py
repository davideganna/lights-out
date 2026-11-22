"""Activity report: one static HTML page per day, plus an index."""

from __future__ import annotations

import html
from pathlib import Path

from .store import Run, Store

STATUS = {
    "merged": ("Merged", "ok"),
    "pr_open": ("PR open", "warn"),
    "needs_info": ("Needs info", "warn"),
    "rejected": ("Declined", "muted"),  # older runs only
    "failed": ("Failed", "bad"),
    "interrupted": ("Interrupted", "bad"),
    "running": ("Running", "muted"),
}

CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--card:#fff;--line:#e7e5e4;
--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;
--card:#292524;--line:#44403c;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px}
h1{font-size:24px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 24px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--muted);font-size:13px}
.run{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.run h2{font-size:16px;margin:0 0 6px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.pill{font-size:12px;font-weight:600;padding:1px 8px;border-radius:99px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.meta{color:var(--muted);font-size:13px}.meta a{color:inherit}
p.s{margin:8px 0 0;white-space:pre-wrap}pre{white-space:pre-wrap;font-size:12px;color:var(--bad);margin:8px 0 0}
ul.days{padding:0;list-style:none}ul.days li{margin:6px 0}a{color:inherit}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{body}</main></body></html>")


def _run_html(r: Run) -> str:
    label, cls = STATUS.get(r.status, (r.status, "muted"))
    e = html.escape
    links = [f"<a href='{e(r.card_url)}'>Trello card</a>"]
    if r.pr_url:
        links.append(f"<a href='{e(r.pr_url)}'>Pull request</a>")
    started = r.started_at[11:16]
    ended = (r.finished_at or "")[11:16] or "…"
    parts = [
        f"<div class=run><h2><span class='pill {cls}'>{label}</span>#{e(r.card_short_id)} {e(r.title)}</h2>",
        f"<div class=meta>{started}–{ended} UTC · ${r.cost_usd:.2f} · {' · '.join(links)}"
        f"{' · stage: ' + e(r.stage) if r.status in ('failed', 'running', 'interrupted') and r.stage else ''}</div>",
    ]
    if r.summary:
        parts.append(f"<p class=s>{e(r.summary)}</p>")
    if r.error:
        parts.append(f"<pre>{e(r.error)}</pre>")
    parts.append("</div>")
    return "".join(parts)


def render_day(store: Store, day: str) -> str:
    runs = store.day_runs(day)
    count = lambda s: sum(1 for r in runs if r.status == s)
    tiles = [
        (count("merged"), "merged"),
        (count("needs_info") + count("rejected"), "sent back"),
        (count("failed") + count("interrupted") + count("pr_open"), "need attention"),
        (f"${sum(r.cost_usd for r in runs):.2f}", "spent"),
    ]
    body = (
        f"<h1>Lights Out — {html.escape(day)}</h1>"
        f"<p class=sub>{len(runs)} ticket(s) processed · <a href='index.html'>all days</a></p>"
        "<div class=tiles>" + "".join(f"<div class=tile><b>{v}</b><span>{k}</span></div>" for v, k in tiles)
        + "</div>" + ("".join(_run_html(r) for r in runs) or "<p class=sub>Nothing processed yet.</p>")
    )
    return _page(f"Lights Out {day}", body)


def write_reports(store: Store, out_dir: Path, day: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    page = out_dir / f"{day}.html"
    page.write_text(render_day(store, day))
    items = "".join(f"<li><a href='{n}.html'>{n}</a> · ${store.day_cost(n):.2f}</li>" for n in store.days())
    (out_dir / "index.html").write_text(
        _page("Lights Out reports", f"<h1>Lights Out reports</h1><ul class=days>{items}</ul>"))
    return page
