"""GitHub adapter: pull requests, CI status, merging."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

CIResult = Literal["success", "failure", "none", "timeout"]


LOG_CHARS = 8000


def _failed_runs(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r["status"] == "completed" and r["conclusion"] not in ("success", "neutral", "skipped")]


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class PullRequest:
    number: int
    url: str
    head_sha: str


@dataclass
class BranchProtection:
    readable: bool = True  # False when GitHub answered 403 (token cannot read protection)
    protected: bool = False
    classic: bool = False
    rulesets: bool = False
    required_checks: list[str] = field(default_factory=list)
    force_pushes_allowed: bool = False
    enforce_admins: bool = False


class GitHubClient:
    def __init__(
        self,
        token: str,
        repo: str,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.repo = repo
        self._sleep = sleep
        self._clock = clock
        self._http = http or httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _req(self, method: str, path: str, **kw):
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}", status=r.status_code)
        return r.json() if r.content else None

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        pr = self._req(
            "POST", f"/repos/{self.repo}/pulls", json={"head": head, "base": base, "title": title, "body": body}
        )
        return PullRequest(number=pr["number"], url=pr["html_url"], head_sha=pr["head"]["sha"])

    def find_open_pr(self, head: str) -> PullRequest | None:
        owner = self.repo.split("/")[0]
        prs = self._req("GET", f"/repos/{self.repo}/pulls", params={"head": f"{owner}:{head}", "state": "open"})
        if not prs:
            return None
        pr = prs[0]
        return PullRequest(number=pr["number"], url=pr["html_url"], head_sha=pr["head"]["sha"])

    def open_or_update_pr(self, *, head: str, base: str, title: str, body: str, head_sha: str) -> PullRequest:
        """Open a PR, or reuse the open one for this branch (a re-run after a failure)."""
        existing = self.find_open_pr(head)
        if existing:
            self._req("PATCH", f"/repos/{self.repo}/pulls/{existing.number}", json={"title": title, "body": body})
            existing.head_sha = head_sha
            return existing
        return self.create_pr(head=head, base=base, title=title, body=body)

    def comment_pr(self, number: int, body: str) -> None:
        self._req("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})

    def ci_status(self, sha: str) -> tuple[CIResult | Literal["pending"], list[str]]:
        """Aggregate check runs for a commit. Returns (state, names of failed checks)."""
        data = self._req("GET", f"/repos/{self.repo}/commits/{sha}/check-runs", params={"per_page": 100})
        runs = data.get("check_runs", [])
        if not runs:
            return "none", []
        if any(r["status"] != "completed" for r in runs):
            return "pending", []
        failed = [r["name"] for r in _failed_runs(runs)]
        return ("failure" if failed else "success"), failed

    def ci_failure_logs(self, sha: str) -> list[tuple[str, str]]:
        """Failure output of a commit's failed check runs as (check name, text) pairs."""
        data = self._req("GET", f"/repos/{self.repo}/commits/{sha}/check-runs", params={"per_page": 100})
        out: list[tuple[str, str]] = []
        for run in _failed_runs(data.get("check_runs", [])):
            if (run.get("app") or {}).get("slug") == "github-actions":
                try:
                    # The log endpoint redirects to another host; httpx drops Authorization there.
                    r = self._http.request(
                        "GET", f"/repos/{self.repo}/actions/jobs/{run['id']}/logs", follow_redirects=True
                    )
                    if r.status_code == 200:
                        out.append((run["name"], r.text[-LOG_CHARS:]))
                        continue
                except httpx.HTTPError:
                    pass
            output = run.get("output") or {}
            parts = [output.get(k) for k in ("title", "summary", "text")]
            text = "\n\n".join(p for p in parts if p)
            note = "(The full log couldn't be read; this is the check run summary.)"
            out.append((run["name"], f"{text}\n\n{note}" if text else note))
        return out

    def wait_for_ci(self, sha: str, *, timeout_s: int, grace_s: int, poll_s: int = 20) -> tuple[CIResult, list[str]]:
        """Wait until all check runs finish.

        'none' means no check run appeared within grace_s (the repo has no CI)."""
        start = self._clock()
        while True:
            state, failed = self.ci_status(sha)
            elapsed = self._clock() - start
            if state in ("success", "failure"):
                return state, failed
            if state == "none" and elapsed >= grace_s:
                return "none", []
            if elapsed >= timeout_s:
                return "timeout", []
            self._sleep(poll_s)

    def merge(self, number: int, *, title: str) -> str:
        res = self._req(
            "PUT", f"/repos/{self.repo}/pulls/{number}/merge", json={"merge_method": "squash", "commit_title": title}
        )
        return res["sha"]

    def delete_branch(self, branch: str) -> None:
        try:
            self._req("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}")
        except GitHubError:
            pass  # already gone, or protected; not worth failing a merged ticket over

    def _get_raw(self, path: str, **kw) -> httpx.Response:
        """GET without raising on error statuses (callers map 403/404 themselves)."""
        return self._http.request("GET", path, **kw)

    def branch_protection(self, branch: str) -> BranchProtection:
        """Read classic branch protection and active rulesets for a branch and combine them."""
        classic_path = f"/repos/{self.repo}/branches/{branch}/protection"
        r = self._get_raw(classic_path)
        if r.status_code == 403:
            return BranchProtection(readable=False)
        if r.status_code >= 400 and r.status_code != 404:
            raise GitHubError(f"GitHub GET {classic_path} -> {r.status_code}: {r.text[:300]}")
        classic = r.status_code == 200
        checks: list[str] = []
        classic_blocks_force = False
        enforce_admins = False
        if classic:
            data = r.json() or {}
            rsc = data.get("required_status_checks") or {}
            checks += list(rsc.get("contexts") or [])
            checks += [c["context"] for c in rsc.get("checks") or [] if c.get("context")]
            classic_blocks_force = not (data.get("allow_force_pushes") or {}).get("enabled", False)
            enforce_admins = bool((data.get("enforce_admins") or {}).get("enabled", False))

        rules_path = f"/repos/{self.repo}/rules/branches/{branch}"
        rr = self._get_raw(rules_path, params={"per_page": 100})
        if rr.status_code == 403:
            return BranchProtection(readable=False)
        if rr.status_code >= 400 and rr.status_code != 404:
            raise GitHubError(f"GitHub GET {rules_path} -> {rr.status_code}: {rr.text[:300]}")
        rules = (rr.json() or []) if rr.status_code == 200 else []
        rulesets = bool(rules)
        rules_block_force = False
        for rule in rules:
            if rule.get("type") == "required_status_checks":
                params = rule.get("parameters") or {}
                checks += [c["context"] for c in params.get("required_status_checks") or [] if c.get("context")]
            elif rule.get("type") == "non_fast_forward":
                rules_block_force = True

        protected = classic or rulesets
        return BranchProtection(
            readable=True,
            protected=protected,
            classic=classic,
            rulesets=rulesets,
            required_checks=list(dict.fromkeys(checks)),
            force_pushes_allowed=protected and not (classic_blocks_force or rules_block_force),
            enforce_admins=enforce_admins,
        )
