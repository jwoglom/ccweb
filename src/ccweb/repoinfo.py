"""What the side panels show: a session's branch changes, its commits, and the repository's files.

All of it comes from the gh CLI (compare, commit, tree and contents), so it needs gh installed and
signed in. Parsing is kept apart from the calls so it can be checked without a network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .prinfo import BranchStatus, PullRequest, _gh, gh_available, relevant

MAX_FILE_CHARS = 400_000


@dataclass
class Target:
    """One comparison the panels can show: what a branch changed relative to the branch it started from."""

    repo: str
    base: str
    head: str
    pr: PullRequest | None = None

    @property
    def label(self) -> str:
        return f"{self.repo.split('/')[-1]}: {self.base} → {self.head}"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.repo.lower(), self.base, self.head)


@dataclass
class ChangedFile:
    path: str
    status: str = "modified"  # added | removed | modified | renamed
    additions: int = 0
    deletions: int = 0
    patch: str = ""  # empty for binary or very large files


@dataclass
class Commit:
    sha: str
    message: str
    author: str = ""
    date: datetime | None = None


@dataclass
class Comparison:
    files: list[ChangedFile] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    error: str = ""


def targets_for(session_sources: list[tuple[str, str]], statuses: list[BranchStatus]) -> list[Target]:
    """The comparisons for a session: each PR or pushed branch, against the branch it started from."""
    revisions = {repo.lower(): revision for repo, revision in session_sources if revision}
    seen: set = set()
    out: list[Target] = []
    for status in relevant(statuses):
        if not status.branch:
            continue
        base = (status.pr.base if status.pr and status.pr.base else "") or revisions.get(status.repo.lower()) or status.default_branch or "main"
        target = Target(status.repo, base, status.branch, status.pr)
        if target.key not in seen:
            seen.add(target.key)
            out.append(target)
    return out


# --------------------------------------------------------------------------- parsing


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_files(rows: Any) -> list[ChangedFile]:
    return [
        ChangedFile(
            path=str(f["filename"]),
            status=str(f.get("status") or "modified"),
            additions=int(f.get("additions") or 0),
            deletions=int(f.get("deletions") or 0),
            patch=str(f.get("patch") or ""),
        )
        for f in rows or []
        if isinstance(f, dict) and f.get("filename")
    ]


def parse_compare(data: Any) -> Comparison:
    if not isinstance(data, dict):
        return Comparison(error="gh returned something unexpected")
    commits = []
    for c in data.get("commits") or []:
        detail = c.get("commit") if isinstance(c, dict) and isinstance(c.get("commit"), dict) else {}
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        commits.append(
            Commit(
                sha=str(c.get("sha") or ""),
                message=str(detail.get("message") or "").splitlines()[0] if detail.get("message") else "",
                author=str(author.get("name") or ""),
                date=_when(author.get("date")),
            )
        )
    return Comparison(parse_files(data.get("files")), commits, int(data.get("ahead_by") or 0), int(data.get("behind_by") or 0))


def parse_tree(data: Any) -> tuple[list[str], bool]:
    """(file paths, truncated). Git trees list directories too; only files matter here."""
    if not isinstance(data, dict):
        return [], False
    paths = sorted(e["path"] for e in data.get("tree") or [] if isinstance(e, dict) and e.get("type") == "blob" and e.get("path"))
    return paths, bool(data.get("truncated"))


# --------------------------------------------------------------------------- gh calls


def _json(args: list[str], timeout: float = 60) -> tuple[Any, str]:
    if not gh_available():
        return None, "needs the gh CLI: install it and run `gh auth login`"
    ok, out = _gh(args, timeout=timeout)
    if not ok:
        return None, out
    try:
        return json.loads(out), ""
    except json.JSONDecodeError:
        return None, "gh returned something unexpected"


def _clean_error(message: str) -> str:
    return "that branch isn't there (not pushed, or no access)" if "Not Found" in message or "404" in message else message


def gh_compare(repo: str, base: str, head: str) -> Comparison:
    data, error = _json(["api", f"repos/{repo}/compare/{quote(base, safe='/')}...{quote(head, safe='/')}"])
    return Comparison(error=_clean_error(error)) if error else parse_compare(data)


def gh_commit_files(repo: str, sha: str) -> tuple[list[ChangedFile], str]:
    data, error = _json(["api", f"repos/{repo}/commits/{quote(sha)}"])
    if error:
        return [], _clean_error(error)
    return parse_files(data.get("files") if isinstance(data, dict) else None), ""


def gh_tree(repo: str, ref: str) -> tuple[list[str], bool, str]:
    data, error = _json(["api", f"repos/{repo}/git/trees/{quote(ref, safe='')}?recursive=1"])
    if error:
        return [], False, _clean_error(error)
    paths, truncated = parse_tree(data)
    return paths, truncated, ""


def gh_file(repo: str, path: str, ref: str) -> tuple[str, str]:
    """(text, error). Binary files and very large ones are described, not shown."""
    if not gh_available():
        return "", "needs the gh CLI: install it and run `gh auth login`"
    ok, out = _gh(["api", "-H", "Accept: application/vnd.github.raw+json", f"repos/{repo}/contents/{quote(path)}?ref={quote(ref, safe='')}"])
    if not ok:
        return "", _clean_error(out)
    if "\x00" in out[:8000]:
        return "", "a binary file"
    if len(out) > MAX_FILE_CHARS:
        return out[:MAX_FILE_CHARS] + "\n… cut here; open the file on GitHub to see the rest", ""
    return out, ""
