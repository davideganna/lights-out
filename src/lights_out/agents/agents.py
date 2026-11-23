"""Claude agents (via the Claude Agent SDK): triage/planner, coder, reviewer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from ..core.config import Config
from ..core.models import Ticket
from . import prompts
from .risk import format_risk_flags

log = logging.getLogger(__name__)

READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
CODER_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "TodoWrite"]
AGENT_CLI_WRAPPER = "/usr/local/bin/claude-as-agent"
CLAUDE_CREDENTIAL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


class AgentError(RuntimeError):
    pass


class BudgetExceeded(AgentError):
    pass


def _process_settings(cfg: Config) -> tuple[dict[str, str], str | None]:
    """Env overrides and CLI path for the Claude process. Credentials come from the inherited environment."""
    env: dict[str, str] = {}
    cli_path = None
    if cfg.agent_user:
        env["HOME"] = f"/home/{cfg.agent_user}"
        cli_path = AGENT_CLI_WRAPPER
    return env, cli_path


def _redact(text: str) -> str:
    """Replace any configured credential value in text; tokens must never reach output or logs."""
    for var in CLAUDE_CREDENTIAL_VARS:
        value = os.environ.get(var, "")
        if value:
            text = text.replace(value, "***")
    return text


async def _check_claude_async(cfg: Config) -> str:
    if not any(os.environ.get(v) for v in CLAUDE_CREDENTIAL_VARS):
        raise AgentError(f"no Claude credentials: set {' or '.join(CLAUDE_CREDENTIAL_VARS)}")
    env, cli_path = _process_settings(cfg)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)  # the unprivileged agent user must be able to enter it
            options = ClaudeAgentOptions(
                model=cfg.planner_model,
                cwd=tmp,
                tools=[],
                allowed_tools=[],
                max_turns=1,
                max_budget_usd=0.05,
                env=env,
                cli_path=cli_path,
                stderr=lambda line: log.debug("claude: %s", _redact(line)),
            )
            result: ResultMessage | None = None
            async for msg in query(prompt="Reply with the word OK.", options=options):
                if isinstance(msg, ResultMessage):
                    result = msg
        if result is None:
            raise AgentError("Claude ended without a result")
        if result.is_error:
            raise AgentError(_redact("; ".join(result.errors or []) or result.subtype))
    except AgentError:
        raise
    except Exception as e:  # noqa: BLE001
        raise AgentError(_redact(str(e))) from None
    return cfg.planner_model


def check_claude(cfg: Config) -> str:
    """Verify Claude credentials with a one-turn request; returns the model name or raises AgentError."""
    return asyncio.run(_check_claude_async(cfg))


@dataclass
class AgentResult:
    text: str
    session_id: str
    cost_usd: float
    structured: Any = None


@dataclass
class TriageResult:
    decision: str  # proceed | needs_info
    summary: str
    questions: list[str]
    plan: str


@dataclass
class ReviewResult:
    approved: bool
    issues: list[str]
    summary: str


class AgentsProtocol(Protocol):
    """What the pipeline needs; tests substitute a fake."""

    spent_usd: float

    async def triage(self, ticket: Ticket, cwd: Path) -> TriageResult: ...
    async def implement(self, ticket: Ticket, plan: str, cwd: Path) -> AgentResult: ...
    async def follow_up(self, session_id: str, message: str, cwd: Path) -> AgentResult: ...
    async def review(
        self, ticket: Ticket, diff: str, cwd: Path, *, risk_flags: list[str] | None = None
    ) -> ReviewResult: ...


@dataclass
class ClaudeAgents:
    """Runs one ticket's agents, enforcing that ticket's budget across all calls."""

    cfg: Config
    transcript_dir: Path
    budget_usd: float
    spent_usd: float = 0.0
    _seq: int = field(default=0, init=False)

    def _remaining(self) -> float:
        remaining = self.budget_usd - self.spent_usd
        if remaining <= 0.05:
            raise BudgetExceeded(f"Ticket budget of ${self.budget_usd:.2f} used up")
        return remaining

    def _options(
        self,
        *,
        model: str,
        cwd: Path,
        tools: list[str],
        append: str,
        max_turns: int,
        output_schema: dict | None = None,
        resume: str | None = None,
        effort: str | None = None,
    ) -> ClaudeAgentOptions:
        env, cli_path = _process_settings(self.cfg)
        return ClaudeAgentOptions(
            model=model,
            cwd=str(cwd),
            tools=tools,
            allowed_tools=tools,
            permission_mode="bypassPermissions",
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
            setting_sources=["project"],  # loads the repo's CLAUDE.md
            max_turns=max_turns,
            max_budget_usd=self._remaining(),
            output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
            resume=resume,
            effort=effort,
            env=env,
            cli_path=cli_path,
            stderr=lambda line: log.debug("claude: %s", line),
        )

    async def _run(self, role: str, prompt: str, options: ClaudeAgentOptions) -> AgentResult:
        self._seq += 1
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript = self.transcript_dir / f"{self._seq:02d}-{role}.md"
        result: ResultMessage | None = None
        texts: list[str] = []
        with transcript.open("w") as f:
            f.write(f"# {role}\n\n## Prompt\n\n{prompt}\n\n## Transcript\n\n")
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            texts.append(block.text)
                            f.write(block.text + "\n\n")
                        elif isinstance(block, ToolUseBlock):
                            f.write(f"> **{block.name}** `{json.dumps(block.input)[:300]}`\n\n")
                    f.flush()
                elif isinstance(msg, ResultMessage):
                    result = msg
            if result is None:
                raise AgentError(f"{role}: agent ended without a result")
            cost = result.total_cost_usd or 0.0
            self.spent_usd += cost
            f.write(f"\n---\nresult: {result.subtype}, turns: {result.num_turns}, cost: ${cost:.4f}\n")
        log.info("%s finished: %s, %d turns, $%.4f", role, result.subtype, result.num_turns, cost)
        if result.is_error:
            if "budget" in result.subtype:
                raise BudgetExceeded(f"{role}: ticket budget exhausted")
            detail = "; ".join(result.errors or []) or result.subtype
            raise AgentError(f"{role} failed: {detail}")
        return AgentResult(
            text=result.result or "\n\n".join(texts),
            session_id=result.session_id,
            cost_usd=cost,
            structured=result.structured_output,
        )

    # --- roles -------------------------------------------------------------
    async def triage(self, ticket: Ticket, cwd: Path) -> TriageResult:
        opts = self._options(
            model=self.cfg.planner_model,
            cwd=cwd,
            tools=READ_ONLY_TOOLS,
            append=prompts.TRIAGE_SYSTEM,
            max_turns=40,
            output_schema=prompts.TRIAGE_SCHEMA,
            effort="high",
        )
        res = await self._run("triage", ticket.as_prompt(), opts)
        data = res.structured
        if not isinstance(data, dict):
            raise AgentError("triage returned no structured output")
        return TriageResult(
            decision=data["decision"],
            summary=data["summary"],
            questions=list(data.get("questions") or []),
            plan=data.get("plan") or "",
        )

    async def implement(self, ticket: Ticket, plan: str, cwd: Path) -> AgentResult:
        cmd = self.cfg.check_command
        opts = self._options(
            model=self.cfg.coder_model,
            cwd=cwd,
            tools=CODER_TOOLS,
            append=prompts.CODER_SYSTEM_APPEND.format(check_command=cmd),
            max_turns=self.cfg.coder_max_turns,
        )
        prompt = prompts.CODER_TASK.format(ticket=ticket.as_prompt(), plan=plan, check_command=cmd)
        return await self._run("implement", prompt, opts)

    async def follow_up(self, session_id: str, message: str, cwd: Path) -> AgentResult:
        cmd = self.cfg.check_command
        opts = self._options(
            model=self.cfg.coder_model,
            cwd=cwd,
            tools=CODER_TOOLS,
            append=prompts.CODER_SYSTEM_APPEND.format(check_command=cmd),
            max_turns=self.cfg.coder_max_turns,
            resume=session_id,
        )
        return await self._run("fix", message, opts)

    async def review(
        self, ticket: Ticket, diff: str, cwd: Path, *, risk_flags: list[str] | None = None
    ) -> ReviewResult:
        opts = self._options(
            model=self.cfg.reviewer_model,
            cwd=cwd,
            tools=READ_ONLY_TOOLS,
            append=prompts.REVIEW_SYSTEM,
            max_turns=40,
            output_schema=prompts.REVIEW_SCHEMA,
            effort="high",
        )
        prompt = prompts.REVIEW_TASK.format(
            ticket=ticket.as_prompt(),
            diff=diff,
            risk_flags=format_risk_flags(risk_flags or []),
            check_command=self.cfg.check_command,
        )
        res = await self._run("review", prompt, opts)
        data = res.structured
        if not isinstance(data, dict):
            raise AgentError("review returned no structured output")
        return ReviewResult(
            approved=bool(data["approved"]), issues=list(data.get("issues") or []), summary=data.get("summary", "")
        )
