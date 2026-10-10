"""A fake sessions server and a fake `claude` binary for tests."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SESSIONS = {
    "data": [
        {
            "type": "session",
            "id": "session_01NeedsInputAAAA",
            "title": "Fix flaky CGM import test",
            "session_status": "requires_action",
            "created_at": "2026-10-10T10:00:00Z",
            "updated_at": "2026-10-10T11:00:00Z",
            "unread": True,
            "status_bucket": "blocked",
            "configured_model": "claude-sonnet-5-5",
            "permission_mode": "auto",
            "environment_id": "env_01DefaultAAAA",
            "post_turn_summary": {"status_category": "need_input", "needs_action": "Widen the timeout or mock the clock?"},
            "session_context": {
                "sources": [
                    {"type": "git_repository", "url": "https://github.com/example-org/example-repo", "revision": "dev"},
                    {"type": "git_repository", "url": "https://github.com/example-org/other-repo"},
                ],
                "effort_level": "medium",
                "outcomes": [{"type": "git_repository", "git_info": {"branches": ["claude/fix-flaky-test"]}}],
            },
        },
        {
            "id": "cse_01RunningBBBBBBB",
            "title": "Update API docs",
            "status": "running",
            "updated_at": 1760000000000,
            "repo": "example-org/docs",
        },
        {"id": "session_01ArchivedCCCCC", "title": "Old work", "session_status": "archived"},
        {
            "id": "session_01ReviewGGGGGGGG",
            "title": "Add retry to uploader",
            "session_status": "idle",
            "status_bucket": "review_ready",
            "updated_at": "2026-10-10T12:00:00Z",
            "unread": False,
            "session_context": {
                "outcomes": [{"type": "git_repository",
                              "git_info": {"type": "github", "repo": "example-org/uploader", "branches": ["claude/other-work"]}}],
            },
        },
    ],
    "has_more": True,
    "last_id": "session_01ArchivedCCCCC",
}

BRANCH_STATUS_BY_SESSION = {
    "session_01NeedsInputAAAA": {
        "repo": "example-org/example-repo",
        "branch": "claude/fix-flaky-test",
        "branch_exists": True,
        "commits": 2,
        "has_changes": True,
        "default_branch": "dev",
        "pull_request": {
            "number": 77,
            "state": "draft",
            "title": "Fix flaky CGM import test",
            "url": "https://github.com/example-org/example-repo/pull/77",
            "additions": 40,
            "deletions": 3,
            "commits": 2,
            "base_ref": "dev",
            "head_sha": "a" * 40,
            "checks": [],
            "deployments": [],
            "review_decision": "REVIEW_REQUIRED",
            "auto_merge_enabled": False,
        },
    },
    "session_01RunningBBBBBBB": {
        "repo": "example-org/docs",
        "branch": "claude/update-docs",
        "branch_exists": True,
        "commits": 1,
        "pull_request": {
            "number": 12,
            "state": "merged",
            "title": "Update API docs",
            "url": "https://github.com/example-org/docs/pull/12",
            "additions": 9,
            "deletions": 9,
            "commits": 1,
            "base_ref": "main",
            "checks": [],
        },
    },
}

SESSIONS_PAGE_2 = {
    "data": [
        {"id": "session_01OlderFFFFFFFFF", "title": "Ancient history", "session_status": "idle",
         "updated_at": "2026-01-01T00:00:00Z"},
    ],
    "has_more": False,
    "last_id": "session_01OlderFFFFFFFFF",
}

ENVIRONMENTS = {
    "environments": [
        {"kind": "anthropic_cloud", "environment_id": "env_01DefaultAAAA", "name": "Default", "state": "active"},
        {"kind": "anthropic_cloud", "environment_id": "env_01UnrestrictedB", "name": "Unrestricted", "state": "active"},
        {"kind": "bridge", "environment_id": "env_01BridgeCCCC", "name": "My laptop", "state": "active"},
    ],
    "has_more": False,
}

EVENTS = {
    "data": [
        {"uuid": "e1", "type": "user", "created_at": "2026-10-10T10:00:00Z",
         "message": {"role": "user", "content": "Please fix the flaky test"}},
        {"uuid": "e2", "type": "assistant", "created_at": "2026-10-10T10:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Looking at **the test** now."},
             {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -x tests/test_import.py"}}]}},
        {"uuid": "e3", "type": "user", "created_at": "2026-10-10T10:00:09Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "1 failed, 12 passed\nmore"}]}},
        {"uuid": "e4", "type": "assistant", "created_at": "2026-10-10T10:00:15Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Should I widen the timeout or mock the clock?"}]}},
    ]
}


class FakeServer:
    def __init__(self):
        self.requests: list[dict] = []
        self.created: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.headers.get("authorization") != "Bearer test-token":
                    return self._send(401, {"error": {"message": "bad token"}})
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/v1/sessions":
                    return self._send(200, SESSIONS_PAGE_2 if "after_id" in query else SESSIONS)
                if url.path == "/v1/environment_providers":
                    return self._send(200, ENVIRONMENTS)
                if url.path.startswith("/v1/sessions/") and url.path.endswith("/events"):
                    return self._send(200, {"data": [], "has_more": False} if "after_id" in query else EVENTS)
                if url.path.startswith("/v1/sessions/"):
                    sid = url.path.rsplit("/", 1)[-1]
                    match = next((s for s in SESSIONS["data"] if s["id"] == sid), None)
                    return self._send(200, match) if match else self._send(404, {"error": {"message": "no session"}})
                return self._send(404, {"error": {"message": "not found"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                outer.requests.append(
                    {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                )
                if self.headers.get("authorization") != "Bearer test-token":
                    return self._send(401, {"error": {"message": "bad token"}})
                if self.path == "/v1/code/github/get-batch-branch-status":
                    pairs = body.get("repo_branches", [])
                    if len(pairs) > 50:
                        return self._send(400, {"error": {"message": f"too many repo_branches: {len(pairs)} (max 50)"}})
                    rows = []
                    for sid in body.get("session_ids", []):
                        if sid in BRANCH_STATUS_BY_SESSION:
                            rows.append({**BRANCH_STATUS_BY_SESSION[sid], "session_id": sid})
                    for pair in pairs:  # a branch with commits but no PR yet, reported without a session
                        if pair["branch"] == "claude/other-work":
                            rows.append({"repo": pair["repo"], "branch": pair["branch"], "branch_exists": True,
                                         "commits": 2, "has_changes": True, "default_branch": "dev"})
                    return self._send(200, {"branch_statuses": rows})
                if self.path == "/v1/sessions":
                    if not body.get("environment_id"):
                        return self._send(400, {"error": {"message": "environment required", "reason": "no_env"}})
                    outer.created.append(body)
                    return self._send(200, {"id": "session_01CreatedDDDDDD"})
                return self._send(404, {"error": {"message": "not found"}})

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def make_fake_claude(directory: Path) -> tuple[Path, Path]:
    """A stand-in claude binary that logs follow-ups and prints documented JSON."""
    log = directory / "claude-calls.jsonl"
    script = directory / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        args = sys.argv[1:]
        log = {str(log)!r}
        if args[:2] == ["auth", "status"]:
            print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai",
                               "orgId": "11111111-2222-3333-4444-555555555555"}}))
            sys.exit(0)
        if "-p" in args and "--cloud" in args:
            sid = args[args.index("--cloud") + 1]
            msg = sys.stdin.read()
            with open(log, "a") as fh:
                fh.write(json.dumps({{"sid": sid, "msg": msg}}) + "\\n")
            if "Archived" in sid:
                print(json.dumps({{"ok": False, "session_id": sid,
                                   "error": f"cloud session {{sid}} is archived and cannot accept new messages"}}))
                sys.exit(1)
            print(json.dumps({{"ok": True, "session_id": sid, "url": "https://claude.ai/code/" + sid}}))
            sys.exit(0)
        if "--cloud" in args:
            print("\\x1b[32mCloning repository\\x1b[0m")
            print("View: \\x1b]8;;https://claude.ai/code/session_01CreatedDDDDDD\\x07https://claude.ai/code/session_01CreatedDDDDDD\\x1b]8;;\\x07")
            sys.exit(0)
        print("unexpected args", args, file=sys.stderr)
        sys.exit(3)
        """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log
