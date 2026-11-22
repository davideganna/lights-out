"""GitHub adapter: pull requests, CI status, merging."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal

import httpx

CIResult = Literal["success", "failure", "none", "timeout"]


class GitHubError(RuntimeError):
    pass


@dataclass
class PullRequest:
    number: int
    url: str
    head_sha: str


class GitHubClient:
    def __init__(self, token: str, repo: str, *, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
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
            raise GitHubError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        pr = self._req("POST", f"/repos/{self.repo}/pulls",
                       json={"head": head, "base": base, "title": title, "body": body})
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
        failed = [r["name"] for r in runs
                  if r["conclusion"] not in ("success", "neutral", "skipped")]
        return ("failure" if failed else "success"), failed

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
        res = self._req("PUT", f"/repos/{self.repo}/pulls/{number}/merge",
                        json={"merge_method": "squash", "commit_title": title})
        return res["sha"]

    def delete_branch(self, branch: str) -> None:
        try:
            self._req("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}")
        except GitHubError:
            pass  # already gone, or protected; not worth failing a merged ticket over
