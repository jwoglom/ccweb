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

SESSIONS = {
    "data": [
        {
            "type": "session",
            "id": "session_01NeedsInputAAAA",
            "title": "Fix flaky CGM import test",
            "session_status": "requires_action",
            "created_at": "2026-10-10T10:00:00Z",
            "updated_at": "2026-10-10T11:00:00Z",
            "session_context": {
                "sources": [{"type": "git_repository", "url": "https://github.com/example-org/example-repo"}],
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
    ]
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
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.headers.get("authorization") != "Bearer test-token":
                    return self._send(401, {"error": {"message": "bad token"}})
                if self.path == "/v1/sessions":
                    return self._send(200, SESSIONS)
                if self.path.startswith("/v1/sessions/") and self.path.endswith("/events"):
                    return self._send(200, EVENTS)
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
