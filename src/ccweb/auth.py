"""Read the access token from your existing Claude Code login.

The token is read fresh when needed, kept in memory only, and never written
to disk or logs. ccweb does not run its own OAuth flow: when the token has
expired it asks the claude CLI to refresh it, then reads it again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class AuthError(Exception):
    """No usable Claude Code login. Messages never contain token material."""


def _claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def _read_keychain(service: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def read_access_token(cfg: dict) -> str:
    env_token = os.environ.get("CCWEB_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token

    raw = _read_keychain(cfg["auth"]["keychain_service"])
    if raw is None:
        cred_file = _claude_config_dir() / ".credentials.json"
        if cred_file.exists():
            raw = cred_file.read_text(encoding="utf-8")
    if raw is None:
        raise AuthError("no Claude Code login found; run `claude auth login` with your claude.ai account")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise AuthError("couldn't parse the stored Claude Code credentials") from None

    token = _find_key(data, "accessToken")
    if not isinstance(token, str) or not token:
        raise AuthError("the stored login has no access token; run `claude auth login`")
    return token


def _find_org_uuid(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and "org" in key.lower() and UUID_RE.match(value):
                return value
        for value in obj.values():
            found = _find_org_uuid(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_org_uuid(value)
            if found:
                return found
    return None


def read_org_uuid(cfg: dict) -> str | None:
    env_org = os.environ.get("CCWEB_ORG_UUID", "").strip()
    if env_org:
        return env_org
    # `claude auth status` prints JSON (documented); look for an org UUID in it.
    try:
        result = subprocess.run(
            [cfg["claude"]["bin"], "auth", "status"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.stdout.strip():
            found = _find_org_uuid(json.loads(result.stdout))
            if found:
                return found
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    for candidate in (_claude_config_dir() / ".claude.json", Path.home() / ".claude.json"):
        try:
            found = _find_org_uuid(json.loads(candidate.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if found:
            return found
    return None


def nudge_cli_refresh(claude_bin: str) -> None:
    """Run a cheap claude command so the CLI refreshes its own access token."""
    try:
        subprocess.run(
            [claude_bin, "auth", "status"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
