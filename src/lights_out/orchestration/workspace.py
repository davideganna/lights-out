"""Per-ticket git workspace.

Git plumbing (clone, commit, push) runs as the orchestrator user and is the only
code that holds the GitHub token. Anything that executes repository code (the
Claude agent and the check command) runs as the unprivileged AGENT_USER.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.models import Ticket


class WorkspaceError(RuntimeError):
    pass


class EmptyRepositoryError(WorkspaceError):
    pass


@dataclass
class CheckResult:
    ok: bool
    exit_code: int
    output: str  # tail of combined stdout/stderr

    @property
    def summary(self) -> str:
        return "passed" if self.ok else f"failed (exit {self.exit_code})"


@dataclass
class FileChange:
    path: str
    added: int
    deleted: int


def _tail(text: str, limit: int = 12000) -> str:
    return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]


def run_as(user: str, cmd: list[str]) -> list[str]:
    """Prefix a command so it runs as `user` (no-op when user is empty)."""
    if not user:
        return cmd
    return ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", "--", *cmd]


class Workspace:
    def __init__(
        self,
        path: Path,
        *,
        repo_url: str,
        base_branch: str,
        github_token: str,
        agent_user: str = "",
        author_name: str = "lights-out",
        author_email: str = "lights-out@users.noreply.github.com",
    ):
        self.path = path
        self.repo_url = repo_url
        self.base_branch = base_branch
        self.agent_user = agent_user
        self.author = (author_name, author_email)
        self._token = github_token

    # --- git helpers -------------------------------------------------------
    def _git_env(self) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if self._token and self.repo_url.startswith("https://github.com/"):
            basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            # Passed via env (not argv) so other users can't read it from /proc/*/cmdline.
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
                }
            )
        return env

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        cmd = ["git", "-c", "safe.directory=*", *args]
        r = subprocess.run(cmd, cwd=cwd or self.path, env=self._git_env(), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {r.stderr.strip()[:500]}")
        return r.stdout

    def git_code(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
        """Run git without raising; returns (exit code, stdout)."""
        cmd = ["git", "-c", "safe.directory=*", *args]
        r = subprocess.run(cmd, cwd=self.path, env={**self._git_env(), **(env or {})}, capture_output=True, text=True)
        return r.returncode, r.stdout

    def _hand_to_agent(self) -> None:
        if self.agent_user:
            subprocess.run(["chown", "-R", f"{self.agent_user}:{self.agent_user}", str(self.path)], check=True)

    def kill_agent_processes(self) -> None:
        """Stop anything the agent left running (dev servers, watchers) before we push."""
        if self.agent_user:
            subprocess.run(["pkill", "-KILL", "-u", self.agent_user], check=False)

    # --- lifecycle ---------------------------------------------------------
    def clone(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.git("clone", self.repo_url, str(self.path), cwd=self.path.parent)
        if self.git("rev-parse", "--verify", "HEAD", check=False).strip() == "":
            raise EmptyRepositoryError(
                "The repository has no commits. Run `lights-out bootstrap` once to create the initial commit."
            )
        self.git("checkout", self.base_branch)

    def prepare(self, ticket: Ticket) -> None:
        self.clone()
        self.git("checkout", "-B", ticket.branch)
        self._hand_to_agent()

    def run_check(self, command: str, timeout_s: int) -> CheckResult:
        self._hand_to_agent()
        env = {**os.environ, "CI": "true"}
        if self.agent_user:
            env["HOME"] = f"/home/{self.agent_user}"
        try:
            r = subprocess.run(
                run_as(self.agent_user, ["bash", "-lc", command]),
                cwd=self.path,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return CheckResult(False, -1, _tail(out + f"\n[timed out after {timeout_s}s]"))
        finally:
            self.kill_agent_processes()
        return CheckResult(r.returncode == 0, r.returncode, _tail(r.stdout + r.stderr))

    def stage_all(self) -> None:
        self.git("add", "-A")

    def diff_vs_base(self, limit: int = 80000) -> str:
        """Everything this branch changes relative to the base branch (committed or not)."""
        self.stage_all()
        stat = self.git("diff", "--cached", "--stat", f"origin/{self.base_branch}")
        full = self.git("diff", "--cached", f"origin/{self.base_branch}")
        self._hand_to_agent()
        if len(full) > limit:
            full = full[:limit] + "\n...(diff truncated; read the files for the rest)..."
        return (stat + "\n" + full).strip()

    def changed_files(self) -> list[FileChange]:
        """Files this branch changes relative to the base, with added/deleted line counts."""
        self.stage_all()
        out = self.git("diff", "--cached", "--numstat", "--no-renames", "-z", f"origin/{self.base_branch}")
        self._hand_to_agent()
        changes: list[FileChange] = []
        for line in out.split("\0"):
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            changes.append(
                FileChange(
                    path=path,
                    added=int(added) if added.isdigit() else 0,
                    deleted=int(deleted) if deleted.isdigit() else 0,
                )
            )
        return changes

    def has_changes(self) -> bool:
        self.stage_all()
        out = self.git("diff", "--cached", "--name-only", f"origin/{self.base_branch}").strip()
        self._hand_to_agent()
        return bool(out)

    def commit(self, message: str) -> None:
        self.stage_all()
        if self.git("diff", "--cached", "--name-only").strip():
            name, email = self.author
            self.git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", message)

    def push(self, branch: str) -> str:
        """Force-push to the agent's own branch (never the base branch). Returns head sha."""
        if branch == self.base_branch or not branch.startswith("agent/"):
            raise WorkspaceError(f"Refusing to push to {branch!r}")
        self.git("push", "--force", "origin", f"HEAD:refs/heads/{branch}")
        return self.git("rev-parse", "HEAD").strip()

    # --- rebasing onto a moved base branch ------------------------------------
    def base_moved(self) -> bool:
        """Fetch the base branch; True when origin/<base> is not yet part of HEAD."""
        self.git("fetch", "origin", self.base_branch)
        code, _ = self.git_code("merge-base", "--is-ancestor", f"origin/{self.base_branch}", "HEAD")
        return code != 0

    def _unmerged(self) -> list[str]:
        out = self.git("diff", "--name-only", "--diff-filter=U", check=False)
        return [f for f in out.splitlines() if f]

    def _rebase_cmd(self, *args: str) -> tuple[int, str]:
        name, email = self.author
        return self.git_code("-c", f"user.name={name}", "-c", f"user.email={email}", *args, env={"GIT_EDITOR": "true"})

    def _rebase_result(self, code: int, what: str) -> list[str]:
        self._hand_to_agent()
        if code == 0:
            return []
        files = self._unmerged()
        if not files:
            raise WorkspaceError(f"git {what} failed without conflicts")
        return files

    def rebase_onto_base(self) -> list[str]:
        """Rebase on origin/<base>. [] on success, else the conflicted files (rebase left in progress)."""
        code, _ = self._rebase_cmd("rebase", f"origin/{self.base_branch}")
        return self._rebase_result(code, "rebase")

    def conflict_markers(self, files: list[str]) -> list[str]:
        """Files that still contain conflict marker lines (a bare ======= is not counted)."""
        bad = []
        for f in files:
            path = self.path / f
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")
            if any(line.startswith(("<<<<<<< ", ">>>>>>> ", "|||||||")) for line in text.splitlines()):
                bad.append(f)
        return bad

    def continue_rebase(self) -> list[str]:
        """Stage the whole working tree and continue. [] when finished, else the newly conflicted files."""
        self.stage_all()  # the coder may also have edited files that were not in conflict
        code, _ = self._rebase_cmd("rebase", "--continue")
        return self._rebase_result(code, "rebase --continue")

    def abort_rebase(self) -> None:
        self.git("rebase", "--abort", check=False)
        self._hand_to_agent()

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "bootstrap"


def bootstrap_repository(ws: Workspace) -> bool:
    """Give an empty repository an initial commit with CI, a Makefile and agent conventions.

    Returns False if the repository already has commits (nothing done)."""
    if ws.path.exists():
        shutil.rmtree(ws.path)
    ws.path.parent.mkdir(parents=True, exist_ok=True)
    ws.git("clone", ws.repo_url, str(ws.path), cwd=ws.path.parent)
    if ws.git("rev-parse", "--verify", "HEAD", check=False).strip():
        return False
    ws.git("checkout", "-b", ws.base_branch)
    for src in TEMPLATES.rglob("*"):
        if src.is_file():
            dst = ws.path / src.relative_to(TEMPLATES)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
    ws.stage_all()
    name, email = ws.author
    ws.git(
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-q",
        "-m",
        "Bootstrap repository for lights-out (CI, Makefile, CLAUDE.md)",
    )
    ws.git("push", "origin", f"HEAD:refs/heads/{ws.base_branch}")
    return True
