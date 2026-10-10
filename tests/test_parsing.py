import asyncio
from datetime import datetime, timedelta, timezone

from ccweb.api import (
    CloudApi,
    Session,
    build_session_body,
    extract_session_id,
    normalize_events,
    normalize_session,
    parse_repo,
    parse_session_ref,
)
from ccweb.app import by_recency, describe_status, home_sessions, model_label, status_kind
from ccweb.config import load_config
from ccweb.__main__ import shape
from fakes import EVENTS, SESSIONS


def test_session_refs():
    assert parse_session_ref("session_01DiUkqY2kzbUbDmW1w96rfi") == "session_01DiUkqY2kzbUbDmW1w96rfi"
    assert parse_session_ref("https://claude.ai/code/session_01DiUkqY2kzbUbDmW1w96rfi?from=cli&m=0") == "session_01DiUkqY2kzbUbDmW1w96rfi"
    assert parse_session_ref("claude.ai/code/cse_01DiUkqY2kzbUbDmW1w96rfi") == "session_01DiUkqY2kzbUbDmW1w96rfi"
    assert parse_session_ref("not a session") is None


def test_extract_from_ansi_output():
    out = "\x1b[1mSent\x1b[0m\nSession ID: \x1b[36msession_01AbcdefGhijklmn\x1b[0m\n"
    assert extract_session_id(out) == "session_01AbcdefGhijklmn"
    assert extract_session_id("nothing here") is None


def test_parse_repo_drops_credentials():
    assert parse_repo("https://user:secret@github.com/owner/repo.git") == "owner/repo"
    assert parse_repo("git@github.com:owner/repo.git") == "owner/repo"
    assert parse_repo("owner/repo") == "owner/repo"
    assert parse_repo("just text") == ""


def test_normalize_sessions_tolerates_shapes():
    a, b, c, _review = (normalize_session(o) for o in SESSIONS["data"])
    assert a.id == "session_01NeedsInputAAAA" and a.status == "requires_action"
    assert a.repo == "example-org/example-repo" and a.branch == "dev"
    assert b.id == "session_01RunningBBBBBBB" and b.repo == "example-org/docs" and b.updated_at is not None
    assert c.archived
    assert normalize_session({"title": "no id"}) is None


def test_normalize_events():
    msgs = normalize_events(EVENTS)
    kinds = [(m.role, m.kind) for m in msgs]
    assert kinds == [("user", "text"), ("assistant", "text"), ("assistant", "tool_use"),
                     ("user", "tool_result"), ("assistant", "text")]
    assert msgs[2].text == "Bash  pytest -x tests/test_import.py"
    assert msgs[3].text == "1 failed, 12 passed"
    reversed_payload = {"data": list(reversed(EVENTS["data"]))}
    assert normalize_events(reversed_payload)[0].text == "Please fix the flaky test"


def test_shape_hides_content():
    s = shape({"id": "session_x", "type": "session", "title": "secret project", "n": 3, "items": [{"a": "b"}]})
    assert s == {"id": "str(9)", "type": "session", "title": "str(14)", "n": "int", "items": ["1 items", {"a": "str(1)"}]}


def test_normalize_session_reads_web_fields():
    a = normalize_session(SESSIONS["data"][0])
    assert a.unread and a.bucket == "blocked" and a.category == "need_input"
    assert a.needs_action == "Widen the timeout or mock the clock?"
    assert (a.model, a.effort, a.permission_mode, a.environment_id) == ("claude-sonnet-5-5", "medium", "auto", "env_01DefaultAAAA")
    assert a.sources == [("example-org/example-repo", "dev"), ("example-org/other-repo", "")]


def test_status_matches_the_web_dots():
    def session(**kw):
        return Session(id="s", **kw)

    # filled = unread, hollow = seen; amber for questions, blue for everything else
    assert describe_status(session(bucket="blocked", unread=True))[:2] == ("●", "#e0b354")
    assert describe_status(session(bucket="blocked", unread=False))[0] == "○"
    assert describe_status(session(bucket="review_ready", unread=True))[:2] == ("●", "#6f9be8")
    assert describe_status(session(bucket="completed", unread=True))[0] == "●"
    assert describe_status(session(bucket="completed", unread=False))[2] == "Completed"
    assert describe_status(session(bucket="working"))[2] == "Working"
    assert describe_status(session(status="pending"))[2] == "Working"
    assert status_kind(session(status="archived", archived=True)) == "archived"
    assert status_kind(session(status="requires_action")) == "needs_input"
    assert status_kind(session(status="idle", bucket="failed")) == "failed"


def test_order_and_home_selection():
    now = datetime.now(timezone.utc)
    mk = lambda sid, mins, **kw: Session(id=sid, updated_at=now - timedelta(minutes=mins), **kw)
    sessions = [
        mk("old", 500),
        mk("review", 30, bucket="review_ready"),
        mk("quiet-question", 5, bucket="blocked", unread=False),
        mk("question", 90, bucket="blocked", unread=True),
        mk("new", 1),
        mk("gone", 2, archived=True),
    ]
    assert [s.id for s in by_recency(sessions)] == ["new", "gone", "quiet-question", "review", "question", "old"]
    # unread questions, then review-ready work, then the rest by recency; archived never
    assert [s.id for s in home_sessions(sessions, limit=10)] == ["question", "review", "new", "quiet-question", "old"]
    assert len(home_sessions(sessions, limit=2)) == 2
    # a question left unread for days no longer jumps the queue
    stale = mk("stale-question", 4 * 24 * 60, bucket="blocked", unread=True)
    assert [s.id for s in home_sessions(sessions + [stale], limit=10)][-1] == "stale-question"


def test_model_labels():
    assert model_label("claude-sonnet-5-5") == "Sonnet 5.5"
    assert model_label("claude-opus-5") == "Opus 5"
    assert model_label("claude-opus-5-5[1m]") == "Opus 5.5 (1M)"
    assert model_label("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert model_label("something-custom") == "something-custom"


def test_build_session_body():
    body = build_session_body(
        "Fix the flaky test, please!",
        "env_1",
        [("o/a", "dev"), ("o/b", "")],
        model="claude-opus-5-5",
        effort="",
        permission_mode="auto",
    )
    assert body["environment_id"] == "env_1" and "self_hosted_runner_pool_id" not in body
    ctx = body["session_context"]
    assert ctx["sources"][0] == {"type": "git_repository", "url": "https://github.com/o/a", "revision": "dev"}
    assert "revision" not in ctx["sources"][1]  # blank branch means the repo's default
    assert "effort_level" not in ctx
    branches = {o["git_info"]["branches"][0] for o in ctx["outcomes"]}
    assert len(branches) == 1 and branches.pop().startswith("claude/fix-the-flaky-test-please-")
    assert [e["data"]["type"] for e in body["events"]] == ["control_request", "user"]
    pool = build_session_body("hi", "ccpool_9", [], permission_mode="")
    assert pool["self_hosted_runner_pool_id"] == "ccpool_9" and "environment_id" not in pool
    assert [e["data"]["type"] for e in pool["events"]] == ["user"]
    assert pool["session_context"]["sources"] == []


def test_api_paging_and_environments(tmp_path, monkeypatch):
    from fakes import FakeServer

    server = FakeServer()
    monkeypatch.setenv("CCWEB_BASE_URL", server.url)
    monkeypatch.setenv("CCWEB_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("CCWEB_ORG_UUID", "11111111-2222-3333-4444-555555555555")

    async def run():
        api = CloudApi(load_config(str(tmp_path / "missing.toml")))
        try:
            first = await api.list_sessions_page()
            assert first.has_more and first.last_id == "session_01ArchivedCCCCC"
            second = await api.list_sessions_page(after_id=first.last_id)
            assert [s.id for s in second.sessions] == ["session_01OlderFFFFFFFFF"] and not second.has_more
            events = await api.list_events_page("session_01NeedsInputAAAA")
            assert len(events.messages) == 5
            assert (await api.list_events_page("session_01NeedsInputAAAA", after_id="x")).messages == []
            assert [e.name for e in await api.list_environments()] == ["Default", "Unrestricted", "My laptop"]
            detail = await api.get_session("session_01NeedsInputAAAA")
            assert detail and detail.unread
        finally:
            await api.aclose()

    asyncio.run(run())
    server.close()


def test_branch_order_puts_useful_branches_first():
    from ccweb.gitinfo import order_branches

    names = ["claude/x-1", "zeta", "ccr-abc", "dev", "alpha", "main", "dev"]
    assert order_branches(names, "dev") == ["dev", "main", "alpha", "zeta", "ccr-abc", "claude/x-1"]
    assert order_branches(names)[:2] == ["main", "dev"]
