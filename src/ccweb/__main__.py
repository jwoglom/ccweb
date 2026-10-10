"""ccweb command line: `ccweb` opens the TUI; `ls`, `probe` and `send` are for scripts and checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from . import claude_cli
from .api import ApiError, AuthError, CloudApi, items, normalize_events, normalize_session, parse_session_ref
from .config import load_config

ENUM_KEYS = {"type", "role", "status", "session_status", "state", "kind"}


def shape(value: Any, key: str = "", depth: int = 0) -> Any:
    """Describe JSON structure without its content (enum-like fields keep their value)."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [f"{len(value)} items", shape(value[0], key, depth + 1)] if value else []
    if isinstance(value, str):
        return value if key in ENUM_KEYS else f"str({len(value)})"
    return type(value).__name__ if value is not None else None


async def _probe(cfg: dict, session_ref: str | None, full: bool) -> int:
    api = CloudApi(cfg)
    try:
        print(f"Base URL: {cfg['api']['base_url']}")
        try:
            status, body, method, path = await api.raw("list_sessions")
        except AuthError as exc:
            print(f"Login: {exc}")
            return 1
        print(f"Org UUID header: {'sent' if api.org_uuid and cfg['api']['send_org_header'] else 'not sent'}")
        print(f"\n{method} {path} -> HTTP {status}")
        print(json.dumps(body if full else shape(body), indent=2)[:6000])
        sessions = [s for s in (normalize_session(o) for o in items(body) if isinstance(o, dict)) if s]
        print(f"\nParsed {len(sessions)} sessions.")
        for s in sessions[:5]:
            print(f"  {s.id}  status={s.status or '?'}  repo={s.repo or '?'}  title={'yes' if s.title else 'no'}")
        sid = parse_session_ref(session_ref) if session_ref else (sessions[0].id if sessions else None)
        if sid:
            status, body, method, path = await api.raw("list_events", id=sid)
            print(f"\n{method} {path} -> HTTP {status}")
            print(json.dumps(body if full else shape(body), indent=2)[:6000])
            if status < 400:
                messages = normalize_events(body)
                kinds: dict[str, int] = {}
                for m in messages:
                    kinds[f"{m.role}/{m.kind}"] = kinds.get(f"{m.role}/{m.kind}", 0) + 1
                print(f"\nParsed {len(messages)} conversation items: {kinds}")
        return 0
    except ApiError as exc:
        print(f"Request failed: {exc}")
        return 1
    finally:
        await api.aclose()


async def _ls(cfg: dict) -> int:
    api = CloudApi(cfg)
    try:
        sessions = await api.list_sessions()
    except (ApiError, AuthError) as exc:
        print(f"Couldn't list sessions: {exc}", file=sys.stderr)
        return 1
    finally:
        await api.aclose()
    for s in sessions:
        when = s.last_activity.astimezone().strftime("%Y-%m-%d %H:%M") if s.last_activity else "-"
        print(f"{s.id}\t{s.status or '-'}\t{when}\t{s.repo or '-'}\t{s.title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccweb", description="Terminal client for your Claude Code cloud sessions.")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("ls", help="list sessions (tab-separated)")
    probe = sub.add_parser("probe", help="check the read endpoints and show response shapes")
    probe.add_argument("--session", help="session ID or link to load events for")
    probe.add_argument("--full", action="store_true", help="print full responses, not just their shape")
    send = sub.add_parser("send", help="send a message to a session (claude -p --cloud)")
    send.add_argument("session")
    send.add_argument("message", nargs="?", help="message text; read from stdin if omitted")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "ls":
        return asyncio.run(_ls(cfg))
    if args.command == "probe":
        return asyncio.run(_probe(cfg, args.session, args.full))
    if args.command == "send":
        sid = parse_session_ref(args.session)
        if not sid:
            print("That doesn't look like a session link or ID.", file=sys.stderr)
            return 2
        text = args.message if args.message is not None else sys.stdin.read()
        result = claude_cli.send_followup(cfg["claude"]["bin"], sid, text)
        print("Sent." if result.ok else f"Not sent: {result.error}", file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    from .app import CloudSessionsApp

    CloudSessionsApp(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
