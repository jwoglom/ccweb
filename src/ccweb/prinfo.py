"""Pull request, CI and diff information for a session.

Two sources, both best effort:

* claude.ai's own branch-status endpoint (undocumented) says which PRs a session opened, their
  state and size, and what its work branches look like. It's batched, so the sidebar can use it.
* The `gh` CLI, when installed and signed in, adds what that endpoint leaves empty: CI check
  results, review state, the file list and the diff.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


@dataclass
class Check:
    name: str
    state: str  # success | failure | pending | skipped
    url: str = ""


@dataclass
class PullRequest:
    repo: str
    number: int
    title: str = ""
    url: str = ""
    state: str = "open"  # open | draft | merged | closed
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    base: str = ""
    head_sha: str = ""
    review: str = ""  # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | ""
    mergeable: str = ""
    auto_merge: bool = False
    checks: list[Check] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.state in {"open", "draft"}

    @property
    def label(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass
class BranchStatus:
    repo: str
    branch: str
    exists: bool = True
    commits: int = 0
    default_branch: str = ""
    pr: PullRequest | None = None


@dataclass
class Details:
    """What `gh` adds for one PR."""

    checks: list[Check] = field(default_factory=list)
    review: str = ""
    mergeable: str = ""
    files: list[tuple[str, int, int]] = field(default_factory=list)  # (path, additions, deletions)
    error: str = ""


# --------------------------------------------------------------------------- parsing

_OK = {"success", "passed", "pass", "completed_success"}
_BAD = {"failure", "failed", "fail", "error", "timed_out", "cancelled", "action_required", "startup_failure"}
_SKIP = {"neutral", "skipped", "stale"}


def check_state(status: str, conclusion: str = "", state: str = "") -> str:
    """Fold GitHub's several vocabularies (check runs, commit statuses, the web's) into four states."""
    status, conclusion, state = status.lower(), conclusion.lower(), state.lower()
    if conclusion:
        word = conclusion
    elif status in {"completed", "complete"}:
        word = state
    elif status:
        word = status  # queued, in_progress, waiting, pending
    else:
        word = state
    if word in _OK:
        return "success"
    if word in _BAD:
        return "failure"
    if word in _SKIP:
        return "skipped"
    return "pending"


def parse_check(obj: Any) -> Check | None:
    """A check from either source: gh's statusCheckRollup items or the web endpoint's `checks`."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("context") or obj.get("workflowName")
    if not isinstance(name, str) or not name:
        return None
    url = obj.get("detailsUrl") or obj.get("targetUrl") or obj.get("details_url") or obj.get("url") or ""
    return Check(
        name,
        check_state(str(obj.get("status") or ""), str(obj.get("conclusion") or ""), str(obj.get("state") or "")),
        url if isinstance(url, str) else "",
    )


def check_counts(checks: list[Check]) -> tuple[int, int, int]:
    """(passing, failing, pending); skipped and neutral checks count as passing."""
    failing = sum(c.state == "failure" for c in checks)
    pending = sum(c.state == "pending" for c in checks)
    return len(checks) - failing - pending, failing, pending


def parse_pr(obj: dict, fallback_repo: str = "") -> PullRequest | None:
    number = obj.get("number")
    if not isinstance(number, int):
        return None
    url = obj.get("url") if isinstance(obj.get("url"), str) else ""
    match = PR_URL_RE.search(url)
    state = str(obj.get("state") or "open").lower()
    if state not in {"open", "draft", "merged", "closed"}:
        state = "open"
    return PullRequest(
        repo=match.group(1) if match else fallback_repo,
        number=number,
        title=str(obj.get("title") or ""),
        url=url,
        state=state,
        additions=int(obj.get("additions") or 0),
        deletions=int(obj.get("deletions") or 0),
        commits=int(obj.get("commits") or 0),
        base=str(obj.get("base_ref") or ""),
        head_sha=str(obj.get("head_sha") or ""),
        review=str(obj.get("review_decision") or ""),
        auto_merge=bool(obj.get("auto_merge_enabled")),
        checks=[c for c in map(parse_check, obj.get("checks") or []) if c is not None],
    )


def parse_branch_status(obj: Any) -> tuple[str, BranchStatus] | None:
    """(session_id or "", status) for one row of the endpoint's `branch_statuses`."""
    if not isinstance(obj, dict) or not isinstance(obj.get("repo"), str):
        return None
    repo = obj["repo"]
    raw_pr = obj.get("pull_request")
    pr = parse_pr(raw_pr, repo) if isinstance(raw_pr, dict) else None
    status = BranchStatus(
        repo=pr.repo if pr and pr.repo else repo,
        branch=str(obj.get("branch") or ""),
        exists=obj.get("branch_exists") is not False,
        commits=int(obj.get("commits") or 0),
        default_branch=str(obj.get("default_branch") or ""),
        pr=pr,
    )
    return str(obj.get("session_id") or ""), status


def relevant(statuses: list[BranchStatus]) -> list[BranchStatus]:
    """What's worth showing for a session: its PRs (live ones first), then branches with commits but no PR."""
    seen: set = set()
    out = []
    for status in statuses:
        if status.pr is None and not (status.exists and status.commits > 0):
            continue
        key = status.pr.url or (status.pr.repo, status.pr.number) if status.pr else (status.repo.lower(), status.branch)
        if key in seen:
            continue
        seen.add(key)
        out.append(status)
    order = {"open": 0, "draft": 0, "merged": 1, "closed": 2}
    return sorted(out, key=lambda s: order.get(s.pr.state, 3) if s.pr else 4)


def best_pr_state(statuses: list[BranchStatus]) -> str:
    """The one PR state to summarise a session by: a live PR beats a merged one beats a closed one."""
    states = [s.pr.state for s in relevant(statuses) if s.pr]
    for wanted in ("open", "draft", "merged", "closed"):
        if wanted in states:
            return wanted
    return ""


# --------------------------------------------------------------------------- gh

GH_FIELDS = "statusCheckRollup,reviewDecision,mergeable,files"


def gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh(args: list[str], timeout: float = 40) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "the gh CLI isn't installed"
    except subprocess.TimeoutExpired:
        return False, "gh timed out"
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        return False, message[0] if message else f"gh exited with {proc.returncode}"
    return True, proc.stdout


def gh_details(url: str) -> Details:
    """CI checks, review state and changed files for the PR at url."""
    ok, out = _gh(["pr", "view", url, "--json", GH_FIELDS])
    if not ok:
        return Details(error=out)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return Details(error="gh returned something unexpected")
    checks = [c for c in map(parse_check, data.get("statusCheckRollup") or []) if c is not None]
    files = [
        (str(f.get("path")), int(f.get("additions") or 0), int(f.get("deletions") or 0))
        for f in data.get("files") or []
        if isinstance(f, dict)
    ]
    return Details(
        checks=checks,
        review=str(data.get("reviewDecision") or ""),
        mergeable=str(data.get("mergeable") or ""),
        files=files,
    )


def gh_diff(url: str, max_lines: int = 4000) -> tuple[str, str]:
    """(diff text, error). Long diffs are cut at max_lines with a note."""
    ok, out = _gh(["pr", "diff", url, "--color", "never"], timeout=60)
    if not ok:
        return "", out
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… diff cut at {max_lines} lines; open the PR to see the rest"]
    return "\n".join(lines), ""
