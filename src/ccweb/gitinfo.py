"""Best-effort repository lookups for the new-session form: the local checkout's GitHub repo and, through
the `gh` CLI when it's installed and signed in, a repo's branches. Everything returns empty on failure."""

from __future__ import annotations

import shutil
import subprocess

from .api import parse_repo


def _run(argv: list[str], cwd: str | None = None, timeout: float = 15) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def local_repo(cwd: str) -> str:
    """owner/repo for the GitHub remote of the checkout at cwd, or ''."""
    return parse_repo(_run(["git", "remote", "get-url", "origin"], cwd=cwd, timeout=5))


def default_branch(repo: str) -> str:
    if not shutil.which("gh"):
        return ""
    return _run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])


def list_branches(repo: str) -> list[str]:
    """All branch names (paged through), in the API's alphabetical order. See order_branches."""
    if not shutil.which("gh"):
        return []
    out = _run(["gh", "api", "--paginate", f"repos/{repo}/branches?per_page=100", "--jq", ".[].name"], timeout=45)
    return out.splitlines()


PREFERRED = ("main", "master", "dev", "develop", "development")


def order_branches(names: list[str], default: str = "") -> list[str]:
    """The default branch first, then the usual integration branches, then the rest, with the
    many branches that Claude cloud sessions leave behind (claude/..., ccr-...) last."""

    def rank(name: str) -> int:
        if name == default:
            return 0
        if name in PREFERRED:
            return 1 + PREFERRED.index(name)
        return 20 if name.startswith(("claude/", "ccr-")) else 10

    return sorted(dict.fromkeys(names), key=lambda n: (rank(n), n))
