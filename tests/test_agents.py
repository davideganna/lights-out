"""Each role passes its configured effort to the Claude options (Claude is never called)."""

from __future__ import annotations

import asyncio

from lights_out.agents.agents import AgentResult, ClaudeAgents
from lights_out.core.config import Config
from lights_out.core.models import Ticket


def build(tmp_path, monkeypatch, **kw):
    cfg = Config(trello_api_key="k", trello_token="t", trello_board_id="b", github_token="g", data_dir=tmp_path, **kw)
    agents = ClaudeAgents(cfg, tmp_path / "t", 5.0)
    calls: list[tuple[str, object]] = []

    async def fake_run(role, prompt, options):
        calls.append((role, options))
        structured = None
        if role == "triage":
            structured = {"decision": "ready", "summary": "s", "questions": [], "plan": "p"}
        elif role == "review":
            structured = {"approved": True, "summary": "s", "issues": []}
        return AgentResult(text="", session_id="sid", cost_usd=0.0, structured=structured)

    monkeypatch.setattr(agents, "_run", fake_run)
    return agents, calls


def efforts(agents, calls, tmp_path):
    ticket = Ticket(id="i", short_id="1", title="t", description="d", url="u")
    asyncio.run(agents.triage(ticket, tmp_path))
    asyncio.run(agents.implement(ticket, "plan", tmp_path))
    asyncio.run(agents.follow_up("sid", "fix", tmp_path))
    asyncio.run(agents.review(ticket, "diff", tmp_path))
    return {role: opts.effort for role, opts in calls}


def test_each_role_uses_its_own_effort(tmp_path, monkeypatch):
    agents, calls = build(tmp_path, monkeypatch, planner_effort="low", coder_effort="max", reviewer_effort="medium")
    assert efforts(agents, calls, tmp_path) == {"triage": "low", "implement": "max", "fix": "max", "review": "medium"}


def test_defaults_are_unchanged(tmp_path, monkeypatch):
    agents, calls = build(tmp_path, monkeypatch)
    assert efforts(agents, calls, tmp_path) == {"triage": "high", "implement": None, "fix": None, "review": "high"}


def test_empty_means_none(tmp_path, monkeypatch):
    agents, calls = build(tmp_path, monkeypatch, planner_effort="", coder_effort="", reviewer_effort="")
    assert efforts(agents, calls, tmp_path) == {"triage": None, "implement": None, "fix": None, "review": None}
