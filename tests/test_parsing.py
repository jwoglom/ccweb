from ccweb.api import extract_session_id, normalize_events, normalize_session, parse_repo, parse_session_ref
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
    a, b, c = (normalize_session(o) for o in SESSIONS["data"])
    assert a.id == "session_01NeedsInputAAAA" and a.status == "requires_action"
    assert a.repo == "example-org/example-repo" and a.branch == "claude/fix-flaky-test"
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
