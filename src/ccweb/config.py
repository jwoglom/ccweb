"""Configuration: defaults, optional TOML overrides, environment overrides."""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

# The read endpoints below are NOT documented by Anthropic. They are a best
# guess at what the claude CLI's teleport picker calls. Verify them with
# `ccweb probe` or tools/mitm_capture.py and override in config.toml.
DEFAULTS: dict[str, Any] = {
    "api": {
        "base_url": "https://api.anthropic.com",
        "anthropic_version": "2023-06-01",
        # Documented for the cloud-environment admin endpoints; assumed here.
        "anthropic_beta": "ccr-byoc-2025-07-29",
        "send_org_header": True,
        "list_sessions": "GET /v1/sessions",
        "list_events": "GET /v1/sessions/{id}/events",
        "timeout_seconds": 20,
    },
    "poll": {"sessions_seconds": 30, "events_seconds": 5},
    "claude": {
        "bin": "claude",
        # Team/Enterprise self-hosted environment (ccpool_...). When set, new
        # sessions are created non-interactively with the documented
        # `claude -p ... --environment` path.
        "environment_id": "",
    },
    "auth": {"keychain_service": "Claude Code-credentials"},
}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ccweb"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ccweb"


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    cfg_path = Path(path or os.environ.get("CCWEB_CONFIG") or config_dir() / "config.toml")
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            cfg = _merge(cfg, tomllib.load(fh))
    env_map = {
        "CCWEB_CLAUDE_BIN": ("claude", "bin"),
        "CCWEB_BASE_URL": ("api", "base_url"),
        "CCWEB_ENVIRONMENT_ID": ("claude", "environment_id"),
    }
    for env, (section, key) in env_map.items():
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]
    return cfg


def parse_endpoint(spec: str) -> tuple[str, str]:
    """'GET /v1/sessions/{id}' -> ('GET', '/v1/sessions/{id}')."""
    parts = spec.split(None, 1)
    if len(parts) == 1:
        return "GET", parts[0]
    return parts[0].upper(), parts[1].strip()
