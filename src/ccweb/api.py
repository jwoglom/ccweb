"""Read-only client for claude.ai/code sessions, plus tolerant response parsing.

The endpoints are undocumented, so parsing never assumes one exact schema:
it looks for the fields it needs under a few likely names and skips the rest.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from . import __version__
from .auth import AuthError, nudge_cli_refresh, read_access_token, read_org_uuid
from .config import parse_endpoint

USER_AGENT = f"ccweb/{__version__} (personal client)"


class ApiError(Exception):
    def __init__(self, status: int | None, method: str, path: str, detail: str):
        self.status, self.method, self.path, self.detail = status, method, path, detail
        code = f"HTTP {status}" if status else "network error"
        super().__init__(f"{code} on {method} {path}: {detail}")


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------

ID_RE = re.compile(r"\b(?:session|cse)_[A-Za-z0-9]{12,}\b")
URL_RE = re.compile(r"claude\.ai/code/((?:session|cse)_[A-Za-z0-9]+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def norm_id(session_id: str) -> str:
    # The docs note cse_... and session_... forms name the same session.
    return "session_" + session_id[4:] if session_id.startswith("cse_") else session_id


def parse_session_ref(text: str) -> str | None:
    """Accept a bare ID or a claude.ai/code URL, with or without scheme/query."""
    text = text.strip()
    match = URL_RE.search(text) or ID_RE.search(text)
    if not match:
        return None
    return norm_id(match.group(1) if match.re is URL_RE else match.group(0))


def extract_session_id(output: str) -> str | None:
    """Find the session a `claude --cloud` run created, from its terminal output."""
    for text in (ANSI_RE.sub("", output), output):
        urls = URL_RE.findall(text)
        if urls:
            return norm_id(urls[-1])
        ids = ID_RE.findall(text)
        if ids:
            return norm_id(ids[-1])
    return None


def web_url(session_id: str) -> str:
    return f"https://claude.ai/code/{session_id}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def items(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "sessions", "events", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


REPO_PATH_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
REPO_URL_RE = re.compile(r"(?:github\.com|gitlab\.com|bitbucket\.org)[/:]([\w.-]+/[\w.-]+?)(?:\.git)?(?:[/?#]|$)")
REPO_KEYS = {"repo", "repository", "full_name", "repo_name", "url", "html_url", "clone_url"}
BRANCH_KEYS = {"branch", "branch_name", "head_branch", "ref", "revision", "branches"}


def parse_repo(value: str) -> str:
    match = REPO_URL_RE.search(value)
    if match:
        return match.group(1)
    return value if REPO_PATH_RE.match(value) else ""


def _walk(obj: Any, depth: int = 0):
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value, depth + 1)


def _find_repo_and_branch(obj: dict) -> tuple[str, str]:
    repo = branch = ""
    for key, value in _walk(obj):
        lk = str(key).lower()
        if not repo and lk in REPO_KEYS and isinstance(value, str):
            repo = parse_repo(value)
        if not branch and lk in BRANCH_KEYS:
            candidate = value[0] if isinstance(value, list) and value else value
            if isinstance(candidate, dict):
                candidate = candidate.get("name") or ""
            if isinstance(candidate, str) and candidate and not re.fullmatch(r"[0-9a-f]{40}", candidate):
                branch = candidate.removeprefix("refs/heads/")
        if repo and branch:
            break
    return repo, branch


@dataclass
class Session:
    id: str
    title: str = ""
    status: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    repo: str = ""
    branch: str = ""
    archived: bool = False
    from_server: bool = True
    unread: bool = False
    bucket: str = ""  # status_bucket: blocked, review_ready, completed, working, failed
    category: str = ""  # post_turn_summary.status_category
    needs_action: str = ""  # what Claude is waiting on, when it is waiting
    summary: str = ""  # post_turn_summary.status_detail
    model: str = ""
    effort: str = ""
    permission_mode: str = ""
    environment_id: str = ""
    environment_kind: str = ""
    sources: list[tuple[str, str]] = field(default_factory=list)  # (owner/repo, base revision)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def last_activity(self) -> datetime | None:
        return self.updated_at or self.created_at


def normalize_session(obj: dict) -> Session | None:
    raw_id = obj.get("id") or obj.get("session_id") or obj.get("uuid")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    status = next(
        (obj[k] for k in ("session_status", "status", "state") if isinstance(obj.get(k), str)), ""
    )
    title = next(
        (obj[k] for k in ("title", "name", "summary") if isinstance(obj.get(k), str) and obj[k]), ""
    )
    repo, branch = _find_repo_and_branch(obj)
    context = obj.get("session_context") if isinstance(obj.get("session_context"), dict) else {}
    summary = obj.get("post_turn_summary") if isinstance(obj.get("post_turn_summary"), dict) else {}
    sources = []
    for source in context.get("sources") or []:
        if isinstance(source, dict) and isinstance(source.get("url"), str):
            sources.append((parse_repo(source["url"]), str(source.get("revision") or "")))
    archived = bool(obj.get("archived") or obj.get("archived_at")) or status.lower() == "archived"
    return Session(
        id=norm_id(raw_id),
        title=title,
        status=status,
        created_at=parse_ts(obj.get("created_at")),
        updated_at=parse_ts(obj.get("updated_at") or obj.get("last_activity_at")),
        repo=repo,
        branch=branch,
        archived=archived,
        unread=bool(obj.get("unread")),
        bucket=str(obj.get("status_bucket") or ""),
        category=str(summary.get("status_category") or ""),
        needs_action=str(summary.get("needs_action") or ""),
        summary=str(summary.get("status_detail") or ""),
        model=str(obj.get("configured_model") or context.get("model") or ""),
        effort=str(context.get("effort_level") or ""),
        permission_mode=str(obj.get("permission_mode") or context.get("permission_mode") or ""),
        environment_id=str(obj.get("environment_id") or ""),
        environment_kind=str(obj.get("environment_kind") or ""),
        sources=sources,
        raw=obj,
    )


@dataclass
class Message:
    key: str
    role: str
    kind: str  # text | tool_use | tool_result | event
    text: str
    ts: datetime | None = None


def _summarize_input(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
            if isinstance(value.get(key), str):
                return value[key]
        value = json.dumps(value, ensure_ascii=False)
    text = str(value or "")
    return text if len(text) <= 100 else text[:97] + "..."


def _tool_result_text(content: Any) -> str:
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    text = str(content or "").strip()
    first = text.splitlines()[0] if text else "(no output)"
    return first if len(first) <= 100 else first[:97] + "..."


def normalize_events(payload: Any) -> list[Message]:
    out: list[Message] = []
    for index, event in enumerate(items(payload)):
        if not isinstance(event, dict):
            continue
        key = str(event.get("uuid") or event.get("id") or event.get("event_id") or f"idx{index}")
        ts = parse_ts(event.get("created_at") or event.get("timestamp"))
        etype = str(event.get("type") or "")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        role = str(message.get("role") or event.get("role") or etype or "event")
        content = message.get("content", event.get("content"))
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        produced = False
        for block_index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            bkey = f"{key}:{block_index}"
            if btype == "text" and str(block.get("text", "")).strip():
                out.append(Message(bkey, role, "text", block["text"], ts))
            elif btype == "tool_use":
                name = block.get("name") or "tool"
                out.append(Message(bkey, role, "tool_use", f"{name}  {_summarize_input(block.get('input'))}", ts))
            elif btype == "tool_result":
                out.append(Message(bkey, role, "tool_result", _tool_result_text(block.get("content")), ts))
            else:
                continue
            produced = True
        if not produced and etype and etype not in {"user", "assistant"}:
            out.append(Message(key, role, "event", etype, ts))
    # Show oldest first, whichever order the server used.
    stamped = [m.ts for m in out if m.ts]
    if len(stamped) >= 2 and stamped[0] > stamped[-1]:
        out.reverse()
    return out


@dataclass
class Environment:
    id: str
    name: str
    kind: str = ""


def normalize_environment(obj: dict) -> Environment | None:
    env_id = obj.get("environment_id") or obj.get("id")
    if not isinstance(env_id, str) or not env_id:
        return None
    return Environment(id=env_id, name=str(obj.get("name") or env_id), kind=str(obj.get("kind") or ""))


def branch_slug(prompt: str) -> str:
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:6]
    return "-".join(words)[:30].strip("-") or "task"


def build_session_body(
    prompt: str,
    environment_id: str,
    repos: list[tuple[str, str]],
    model: str = "",
    effort: str = "",
    permission_mode: str = "",
    title: str = "",
) -> dict:
    """The body the claude CLI posts to create a cloud session.

    `repos` is [(owner/repo, base branch or "" for the repo's default)]. Each repo
    gets a source (what to clone) and an outcome branch (where Claude's work goes).
    """
    work_branch = f"claude/{branch_slug(prompt)}-{secrets.token_hex(3)}"
    events: list[dict] = []
    if permission_mode:
        events.append(
            {
                "type": "event",
                "data": {
                    "type": "control_request",
                    "request_id": f"set-mode-{uuid.uuid4()}",
                    "request": {"subtype": "set_permission_mode", "mode": permission_mode},
                },
            }
        )
    events.append(
        {
            "type": "event",
            "data": {
                "uuid": str(uuid.uuid4()),
                "session_id": "",
                "type": "user",
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": prompt},
            },
        }
    )
    context: dict = {
        "sources": [
            {"type": "git_repository", "url": f"https://github.com/{repo}", **({"revision": branch} if branch else {})}
            for repo, branch in repos
        ],
        "outcomes": [
            {"type": "git_repository", "git_info": {"type": "github", "repo": repo, "branches": [work_branch]}}
            for repo, _ in repos
        ],
    }
    if model:
        context["model"] = model
    if effort:
        context["effort_level"] = effort
    body: dict = {"title": title or prompt.strip().splitlines()[0][:80], "events": events, "session_context": context}
    # Self-hosted runner pools use a different key than regular environments.
    body["self_hosted_runner_pool_id" if environment_id.startswith("ccpool_") else "environment_id"] = environment_id
    return body


@dataclass
class SessionPage:
    sessions: list[Session]
    has_more: bool = False
    last_id: str | None = None


@dataclass
class EventPage:
    messages: list[Message]
    has_more: bool = False
    last_id: str | None = None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class CloudApi:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._token: str | None = None
        self._org: str | None = None
        self._org_loaded = False
        self._client = httpx.AsyncClient(
            base_url=cfg["api"]["base_url"],
            timeout=float(cfg["api"]["timeout_seconds"]),
            headers={"user-agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def org_uuid(self) -> str | None:
        return self._org

    async def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = await asyncio.to_thread(read_access_token, self.cfg)
        if not self._org_loaded:
            self._org = await asyncio.to_thread(read_org_uuid, self.cfg)
            self._org_loaded = True
        api = self.cfg["api"]
        headers = {
            "authorization": f"Bearer {self._token}",
            "anthropic-version": api["anthropic_version"],
            "accept": "application/json",
        }
        if api.get("anthropic_beta"):
            headers["anthropic-beta"] = api["anthropic_beta"]
        if self._org and api.get("send_org_header"):
            headers["x-organization-uuid"] = self._org
        return headers

    async def raw(
        self, endpoint_key: str, *, query: dict | None = None, body: Any = None, **params: str
    ) -> tuple[int, Any, str, str]:
        """Return (status, parsed JSON or text, method, path). Raises AuthError."""
        method, template = parse_endpoint(self.cfg["api"][endpoint_key])
        path = template.format(**{k: quote(v, safe="") for k, v in params.items()})
        resp = None
        for attempt in range(2):
            try:
                resp = await self._client.request(
                    method, path, headers=await self._headers(), params=query or None, json=body
                )
            except httpx.HTTPError as exc:
                raise ApiError(None, method, path, type(exc).__name__) from None
            if resp.status_code == 401 and attempt == 0:
                self._token = None
                await asyncio.to_thread(nudge_cli_refresh, self.cfg["claude"]["bin"])
                continue
            break
        assert resp is not None
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, body, method, path

    async def request(self, endpoint_key: str, *, query: dict | None = None, body: Any = None, **params: str) -> Any:
        status, body, method, path = await self.raw(endpoint_key, query=query, body=body, **params)
        if status >= 400:
            raise ApiError(status, method, path, _error_detail(body))
        if isinstance(body, str):
            raise ApiError(status, method, path, "response wasn't JSON")
        return body

    async def list_sessions_page(self, after_id: str | None = None) -> SessionPage:
        payload = await self.request("list_sessions", query={"after_id": after_id} if after_id else None)
        sessions = [normalize_session(o) for o in items(payload) if isinstance(o, dict)]
        meta = payload if isinstance(payload, dict) else {}
        last_id = meta.get("last_id") if isinstance(meta.get("last_id"), str) else None
        return SessionPage([s for s in sessions if s is not None], meta.get("has_more") is True, last_id)

    async def list_sessions(self) -> list[Session]:
        return (await self.list_sessions_page()).sessions

    async def get_session(self, session_id: str) -> Session | None:
        payload = await self.request("get_session", id=session_id)
        return normalize_session(payload) if isinstance(payload, dict) else None

    async def list_events_page(self, session_id: str, after_id: str | None = None) -> EventPage:
        """One page of events, oldest first. The server's default page is only 50 events."""
        query: dict = {"limit": 500}
        if after_id:
            query["after_id"] = after_id
        payload = await self.request("list_events", query=query, id=session_id)
        meta = payload if isinstance(payload, dict) else {}
        last_id = meta.get("last_id") if isinstance(meta.get("last_id"), str) else None
        return EventPage(normalize_events(payload), meta.get("has_more") is True, last_id)

    async def list_events(self, session_id: str) -> list[Message]:
        return (await self.list_events_page(session_id)).messages

    async def list_environments(self) -> list[Environment]:
        out: list[Environment] = []
        after_id = None
        for _ in range(10):
            query: dict = {"limit": 100}
            if after_id:
                query["after_id"] = after_id
            payload = await self.request("list_environments", query=query)
            rows = payload.get("environments") if isinstance(payload, dict) else None
            out.extend(e for e in map(normalize_environment, rows or []) if e is not None)
            after_id = payload.get("last_id") if isinstance(payload, dict) else None
            if not (isinstance(payload, dict) and payload.get("has_more") is True and rows and after_id):
                break
        return out

    async def create_session(self, body: dict) -> str:
        """Create a cloud session; returns its ID. The request body mirrors what `claude --cloud` sends."""
        payload = await self.request("create_session", body=body)
        found = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(found, str) and isinstance(payload, dict) and isinstance(payload.get("session"), dict):
            found = payload["session"].get("id")
        if not isinstance(found, str) or not found:
            raise ApiError(None, "POST", "create_session", "the response didn't include a session ID")
        return norm_id(found)


def _error_detail(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            reason = f" ({err['reason']})" if isinstance(err.get("reason"), str) else ""
            return (str(err["message"]) + reason)[:240]
        if isinstance(body.get("message"), str):
            return body["message"][:200]
    text = str(body).strip().replace("\n", " ")
    return text[:200] or "empty response"


__all__ = [
    "ApiError",
    "AuthError",
    "CloudApi",
    "Environment",
    "Message",
    "Session",
    "build_session_body",
    "extract_session_id",
    "normalize_events",
    "normalize_session",
    "parse_session_ref",
    "web_url",
]
