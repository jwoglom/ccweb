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
                     ("user", "tool_result"), ("assistant", "text"), ("assistant", "text")]
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
    # a live PR makes an otherwise idle session "ready for review"; a merged one doesn't
    idle = mk("idle-with-pr", 40)
    assert "idle-with-pr" in [s.id for s in home_sessions([idle], pr_states={"idle-with-pr": "draft"})]
    assert status_kind(idle, pr_state="draft") == "review" and status_kind(idle, pr_state="merged") == "idle"
    assert describe_status(idle, pr_state="merged")[2] == "PR merged"
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
            assert len(events.messages) == 6
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


def test_check_states_across_vocabularies():
    from ccweb.prinfo import check_counts, check_state, parse_check

    assert check_state("COMPLETED", "SUCCESS") == "success"
    assert check_state("COMPLETED", "FAILURE") == "failure"
    assert check_state("COMPLETED", "CANCELLED") == "failure"
    assert check_state("COMPLETED", "SKIPPED") == "skipped"
    assert check_state("IN_PROGRESS") == "pending" and check_state("QUEUED") == "pending"
    assert check_state("", "", "SUCCESS") == "success" and check_state("", "", "PENDING") == "pending"
    assert check_state("completed", "", "failure") == "failure"
    # gh's check runs and commit statuses, and the web's lowercase shape
    checks = [
        parse_check({"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u"}),
        parse_check({"__typename": "StatusContext", "context": "ci/legacy", "state": "FAILURE", "targetUrl": "t"}),
        parse_check({"name": "lint", "status": "in_progress"}),
        parse_check({"name": "docs", "status": "completed", "conclusion": "neutral"}),
    ]
    assert [c.state for c in checks] == ["success", "failure", "pending", "skipped"]
    assert checks[0].url == "u" and checks[1].url == "t"
    assert check_counts(checks) == (2, 1, 1)
    assert parse_check({"status": "x"}) is None and parse_check("nope") is None


def test_parse_branch_status_and_relevance():
    from ccweb.prinfo import best_pr_state, parse_branch_status, relevant

    pr_row = {
        "repo": "owner/repo", "branch": "claude/a", "branch_exists": True, "commits": 2, "default_branch": "dev",
        "session_id": "session_x",
        "pull_request": {"number": 5, "state": "draft", "title": "T", "url": "https://github.com/Owner/Repo/pull/5",
                         "additions": 3, "deletions": 1, "commits": 2, "base_ref": "dev", "checks": [],
                         "review_decision": "APPROVED"},
    }
    sid, status = parse_branch_status(pr_row)
    assert sid == "session_x" and status.pr.repo == "Owner/Repo" and status.pr.state == "draft"
    assert status.pr.review == "APPROVED" and status.pr.label == "Owner/Repo#5" and status.pr.is_live
    merged = parse_branch_status({**pr_row, "branch": "b", "pull_request": {**pr_row["pull_request"], "number": 4, "state": "merged", "url": "https://github.com/o/r/pull/4"}})[1]
    bare = parse_branch_status({"repo": "o/r", "branch": "claude/c", "branch_exists": True, "commits": 3})[1]
    gone = parse_branch_status({"repo": "o/r", "branch": "claude/d", "branch_exists": False, "commits": 0})[1]
    empty = parse_branch_status({"repo": "o/r", "branch": "claude/e", "branch_exists": True, "commits": 0})[1]
    assert parse_branch_status({"nope": 1}) is None and parse_branch_status("x") is None
    # live PRs first, then merged, then branches with commits; empty and missing branches are dropped; duplicates collapse
    ordered = relevant([merged, bare, gone, empty, status, status])
    assert [(s.pr.number if s.pr else s.branch) for s in ordered] == [5, 4, "claude/c"]
    assert best_pr_state([merged, bare]) == "merged" and best_pr_state([merged, status]) == "draft"
    assert best_pr_state([bare, gone]) == "" and best_pr_state([]) == ""


def test_branch_status_grouping_respects_the_pair_limit():
    from ccweb.api import _groups

    def session(n, repos=2):
        return Session(id=f"s{n}", outcomes=[(f"o/r{j}", f"claude/b{n}") for j in range(repos)])

    groups = list(_groups([session(n, 4) for n in range(30)]))
    assert all(len(g) <= 12 for g in groups)  # 12 sessions x 4 pairs = 48 <= 50
    assert sum(len(g) for g in groups) == 30
    many = list(_groups([session(n, 1) for n in range(60)]))
    assert [len(g) for g in many] == [25, 25, 10]  # session cap, not the pair cap
    # sessions sharing a branch name across repos still count each (repo, branch) once
    shared = [Session(id=f"s{n}", outcomes=[("o/r", "claude/same")]) for n in range(5)]
    assert len(list(_groups(shared))) == 1


def test_branch_statuses_map_rows_to_sessions(tmp_path, monkeypatch):
    from fakes import FakeServer

    server = FakeServer()
    monkeypatch.setenv("CCWEB_BASE_URL", server.url)
    monkeypatch.setenv("CCWEB_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("CCWEB_ORG_UUID", "11111111-2222-3333-4444-555555555555")

    async def run():
        api = CloudApi(load_config(str(tmp_path / "missing.toml")))
        try:
            by_id = Session(id="session_01NeedsInputAAAA", outcomes=[("example-org/example-repo", "claude/fix-flaky-test")])
            by_branch = Session(id="session_01Unlisted", outcomes=[("example-org/uploader", "claude/other-work")])
            found = await api.branch_statuses([by_id, by_branch], review=True)
            assert [s.pr.number for s in found[by_id.id]] == [77]
            assert found[by_branch.id][0].pr is None and found[by_branch.id][0].commits == 2
        finally:
            await api.aclose()

    asyncio.run(run())
    body = [r for r in server.requests if r.get("body")][0]["body"]
    assert body["include_review_decision"] is True and len(body["repo_branches"]) == 2
    server.close()


def test_links_in_text_and_markdown():
    from ccweb.links import LinkContext, is_openable, linkify_markdown, linkify_text

    ctx = LinkContext(default_repo="o/r", pr_repos={525: "owner/other"})
    text = linkify_text("see https://example.com/a, owner/repo#12 and #525 (and #7).", ctx)
    found = {text.plain[span.start:span.end]: span.style for span in text.spans if getattr(span.style, "link", None)}
    assert set(found) == {"https://example.com/a", "owner/repo#12", "#525", "#7"}  # trailing comma and ")" stay out
    assert found["https://example.com/a"].link == "https://example.com/a"
    assert found["owner/repo#12"].link == "https://github.com/owner/repo/issues/12"
    assert found["#525"].link == "https://github.com/owner/other/issues/525"  # the session's own PR wins
    assert found["#7"].link == "https://github.com/o/r/issues/7"  # the session's only repository
    assert found["#7"].meta["@click"] == "app.open_link('https://github.com/o/r/issues/7')"
    # without a repository for it, a bare #7 is not guessed at, and file paths aren't mistaken for references
    bare = linkify_text("#7 and src/main.py#L10 and x/y/z#3", LinkContext())
    assert [bare.plain[s.start:s.end] for s in bare.spans if getattr(s.style, "link", None)] == []

    source = "Fixes owner/repo#5, not `owner/repo#6`, [x](https://e.com/owner/repo#7) or #525.\n```\nowner/repo#8 #525\n```"
    out = linkify_markdown(source, ctx)
    assert "[owner/repo#5](https://github.com/owner/repo/issues/5)" in out
    assert "[#525](https://github.com/owner/other/issues/525)." in out
    assert "`owner/repo#6`" in out and "owner/repo#8 #525\n```" in out  # code is left alone
    assert out.count("owner/repo#7") == 1 and "[x](https://e.com/owner/repo#7)" in out  # existing links aren't nested

    assert is_openable("https://x.test/a") and is_openable("mailto:a@b.test")
    assert not is_openable("file:///etc/passwd") and not is_openable("javascript:alert(1)") and not is_openable("../x.md")


def test_session_actions_hit_the_right_endpoints(tmp_path, monkeypatch):
    import pytest

    from ccweb.api import ApiError
    from fakes import FakeServer

    server = FakeServer()
    monkeypatch.setenv("CCWEB_BASE_URL", server.url)
    monkeypatch.setenv("CCWEB_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("CCWEB_ORG_UUID", "11111111-2222-3333-4444-555555555555")
    sid = "session_01NeedsInputAAAA"

    async def run():
        api = CloudApi(load_config(str(tmp_path / "missing.toml")))
        try:
            await api.rename_session(sid, "A better title")
            await api.mark_read(sid)
            await api.archive_session(sid)
            await api.delete_session(sid)  # answers 204 with no body
            assert (await api.share_status(sid))["effective_visibility"] == "private"
            for call in (api.rename_session, api.archive_session, api.mark_read, api.delete_session):
                args = ("t",) if call == api.rename_session else ()
                with pytest.raises(ApiError) as failure:
                    await call("session_01FailXXXXXXXXXX", *args)
                assert "HTTP 403" in str(failure.value) and "not allowed (forbidden)" in str(failure.value)
        finally:
            await api.aclose()

    asyncio.run(run())
    assert server.actions == [
        ("PUT", f"/v1/code/sessions/{sid}", {"title": "A better title"}),
        ("POST", f"/v1/code/sessions/{sid}/mark_read", {}),
        ("POST", f"/v1/code/sessions/{sid}/archive", {}),
        ("DELETE", f"/v1/sessions/{sid}", {}),
    ]
    server.close()


def test_ci_and_row_summaries():
    from ccweb.app import BRICK, AMBER, MUTED, SAGE, ci_summary, row_summary
    from ccweb.prinfo import BranchStatus, Check, PullRequest

    ok, bad, wait = Check("a", "success"), Check("b", "failure"), Check("c", "pending")
    assert ci_summary([]) is None
    assert ci_summary([ok, Check("d", "skipped")]) == ("CI passed", SAGE)
    assert ci_summary([ok, wait, wait]) == ("CI running 1/3", AMBER)
    assert ci_summary([ok, bad]) == ("CI failed", BRICK)
    assert ci_summary([ok, bad, wait]) == ("CI failing", BRICK)  # failed already, the rest still going

    def parts(session, kind="idle", prs=(), checks=()):
        shown = [BranchStatus(pr.repo, "b", True, 1, "dev", pr) for pr in prs]
        return [text for text, _ in row_summary(session, kind, shown, lambda pr: list(checks))]

    now = datetime.now(timezone.utc)
    quiet = Session(id="s", updated_at=now - timedelta(minutes=12), repo="o/r")
    draft = PullRequest("o/r", 525, state="draft")
    assert parts(quiet, prs=[draft], checks=[ok, wait]) == ["Draft #525", "CI running 1/2", "12m"]
    assert parts(quiet, prs=[PullRequest("o/r", 9, state="open")], checks=[ok]) == ["Open #9", "CI passed", "12m"]
    # merged and closed PRs say nothing about CI; extra PRs are counted
    assert parts(quiet, prs=[PullRequest("o/r", 4, state="merged")], checks=[bad]) == ["Merged #4", "12m"]
    assert parts(quiet, prs=[draft, PullRequest("o/r", 6, state="draft")], checks=[ok]) == ["Draft #525", "CI passed", "+1", "12m"]
    assert parts(quiet, kind="working", prs=[draft]) == ["Working", "Draft #525", "12m"]
    # a branch with commits but no PR yet
    branch_only = [BranchStatus("o/r", "claude/x", True, 2, "dev", None)]
    assert [t for t, _ in row_summary(quiet, "idle", branch_only, lambda pr: [])] == ["2 commits pushed, no PR", "12m"]
    # with no PR: the question you're being asked, else the last summary, else the repository
    asking = Session(id="s", repo="o/r", needs_action="Shall I\n begin?", summary="did a thing")
    assert parts(asking, kind="needs_input") == ["Shall I begin?"]
    assert parts(Session(id="s", repo="o/r", summary="did a thing")) == ["did a thing"]
    assert parts(Session(id="s", repo="o/r")) == ["r"]
    assert parts(Session(id="s")) == []


def test_gh_checks_batch_reads_graphql_and_survives_failures(monkeypatch):
    import json
    import subprocess

    from ccweb import prinfo

    calls = []

    def node(name, status, conclusion):
        return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion, "detailsUrl": "u"}

    def fake_run(argv, **kwargs):
        query = argv[-1]
        calls.append(query)
        data = {}
        if 'name: "TandemKit"' in query:
            data["p0"] = {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                node("build", "COMPLETED", "SUCCESS"), node("tests", "IN_PROGRESS", None),
                {"__typename": "StatusContext", "context": "legacy/ci", "state": "FAILURE", "targetUrl": "t"}]}}}}]}}}
        # p1 and p2 come back null or without CI, as for a repo gh can't read or a PR with no checks
        data["p1"] = None
        data["p2"] = {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}}}
        return subprocess.CompletedProcess(argv, 1, json.dumps({"data": data, "errors": [{"message": "partial"}]}), "")

    monkeypatch.setattr(prinfo, "gh_available", lambda: True)
    monkeypatch.setattr(prinfo.subprocess, "run", fake_run)
    refs = [("jwoglom/TandemKit", 525), ("no/access", 1), ("jwoglom/nochecks", 3), ("bad repo", 9), ("jwoglom/TandemKit", 525)]
    got = prinfo.gh_checks_batch(refs)
    assert list(got) == [("jwoglom/tandemkit", 525)]  # keyed case-insensitively; unreadable ones are left out
    assert [(c.name, c.state) for c in got[("jwoglom/tandemkit", 525)]] == [
        ("build", "success"), ("tests", "pending"), ("legacy/ci", "failure")]
    assert len(calls) == 1 and 'repository(owner: "bad' not in calls[0]  # a malformed repo never reaches the query

    calls.clear()
    many = [(f"o/r{i}", i) for i in range(45)]
    prinfo.gh_checks_batch(many, group_size=20)
    assert len(calls) == 3  # 20 + 20 + 5
    monkeypatch.setattr(prinfo, "gh_available", lambda: False)
    assert prinfo.gh_checks_batch(many) == {}


def test_grouping_model():
    from ccweb.groups import Grouping

    g = Grouping()
    a, b = g.new_group(" Alpha "), g.new_group("Beta")
    assert (a.name, [x.name for x in g.groups]) == ("Alpha", ["Alpha", "Beta"]) and a.id != b.id
    assert g.by_name("alpha") is a and g.by_name("nope") is None
    g.move("s1", a.id)
    g.move("s2", a.id)
    g.move("s1", b.id)  # in one group at most
    assert (a.members, b.members, g.group_of("s1")) == (["s2"], ["s1"], b)
    g.move("s1", None)
    g.move("s2", "no-such-group")  # an unknown group just means ungrouped
    assert a.members == [] and b.members == [] and g.group_of("s1") is None
    g.move("s3", a.id)
    g.rename(a.id, "Alpha 2")
    assert g.get(a.id).name == "Alpha 2"
    assert g.toggle_pin("p1") and g.toggle_pin("p2") and g.pinned == ["p2", "p1"]  # newest pin first
    assert not g.toggle_pin("p1") and g.pinned == ["p2"]
    g.pinned.append("s3")
    g.forget("s3")
    assert g.pinned == ["p2"] and a.members == []
    g.move("s4", b.id)
    g.delete(b.id)  # the group goes; its sessions are simply ungrouped again
    assert [x.name for x in g.groups] == ["Alpha 2"] and g.group_of("s4") is None
    again = Grouping.from_json(g.to_json())
    assert again.to_json() == g.to_json()
    # whatever is in the file, a bad entry doesn't take the rest down
    messy = Grouping.from_json({"groups": [{"id": "x", "name": None, "members": ["a", 3]}, "junk", {"name": "no id"}], "pinned": ["p", 1], "grouped": False})
    assert [(x.id, x.name, x.members) for x in messy.groups] == [("x", "Untitled", ["a"])] and messy.pinned == ["p"] and not messy.grouped
    assert Grouping.from_json(None).groups == [] and Grouping.from_json("nope").grouped


def test_store_keeps_groups(tmp_path):
    from ccweb.groups import Grouping
    from ccweb.store import Store

    path = tmp_path / "state.json"
    store = Store(path)
    assert store.grouping.groups == []
    grouping = store.grouping
    grouping.move("session_x", grouping.new_group("Alpha").id)
    grouping.toggle_pin("session_y")
    store.save_grouping(grouping)
    reloaded = Store(path).grouping
    assert [(g.name, g.members) for g in reloaded.groups] == [("Alpha", ["session_x"])] and reloaded.pinned == ["session_y"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"  # owner-only, like the rest of the state file


def test_plan_is_rebuilt_from_task_calls():
    from ccweb.tasks import TaskBoard, extract_task_ops
    from fakes import EVENTS_PLAN

    ops = extract_task_ops(EVENTS_PLAN)
    assert [o["op"] for o in ops] == ["create", "created", "create", "created", "create", "created", "update", "update"]
    board = TaskBoard()
    board.apply(ops)
    assert [(t.id, t.subject, t.status) for t in board.ordered] == [
        ("1", "Write the parser", "completed"), ("2", "Add tests", "in_progress"), ("3", "Ship it", "pending")]
    assert board.counts == (1, 3) and board.tasks["2"].active_form == "Adding tests"

    # pages arrive one at a time; a task confirmed under a different id is renumbered; deleting removes it
    board = TaskBoard()
    board.apply([{"op": "create", "use": "a", "subject": "First", "description": "", "active": ""}])
    board.apply([{"op": "create", "use": "b", "subject": "Second", "description": "", "active": ""},
                 {"op": "created", "use": "b", "id": "7"}, {"op": "update", "id": "7", "status": "completed", "subject": "Second!"}])
    assert [(t.id, t.subject, t.status) for t in board.ordered] == [("1", "First", "pending"), ("7", "Second!", "completed")]
    board.apply([{"op": "update", "id": "1", "status": "deleted"}, {"op": "update", "id": "99", "status": "completed"}])
    assert [t.id for t in board.ordered] == ["7"]
    # the older TodoWrite replaces the whole list
    board.apply([{"op": "todos", "todos": [{"content": "A", "status": "completed"}, {"content": "B", "status": "in_progress", "activeForm": "Doing B"}, {"content": "C", "status": "weird"}]}])
    assert [(t.subject, t.status) for t in board.ordered] == [("A", "completed"), ("B", "in_progress"), ("C", "pending")]
    assert extract_task_ops({"data": [{"message": {"content": "just text"}}, "junk"]}) == [] and extract_task_ops(None) == []


def test_comparison_targets_and_parsing(monkeypatch):
    from ccweb import repoinfo
    from ccweb.prinfo import BranchStatus, PullRequest
    from ccweb.repoinfo import parse_compare, parse_tree, targets_for

    pr = PullRequest("O/Repo", 5, state="draft", base="dev")
    statuses = [
        BranchStatus("o/other", "claude/x", True, 2, "main", None),
        BranchStatus("O/Repo", "claude/x", True, 3, "main", pr),
        BranchStatus("o/gone", "claude/y", False, 0, "main", None),  # not pushed: not offered
        BranchStatus("O/Repo", "claude/x", True, 3, "main", pr),  # a duplicate
    ]
    targets = targets_for([("o/other", "release")], statuses)
    # a PR's own base wins, then the branch the session started from, then the repo default; live PRs come first
    assert [(t.repo, t.base, t.head) for t in targets] == [("O/Repo", "dev", "claude/x"), ("o/other", "release", "claude/x")]
    assert targets[0].label == "Repo: dev → claude/x" and targets[0].pr is pr

    cmp = parse_compare({
        "ahead_by": 2, "behind_by": 1,
        "files": [{"filename": "a/b.py", "status": "added", "additions": 3, "deletions": 0, "patch": "@@ -0,0 +1,3 @@\n+x"},
                  {"filename": "img.png", "status": "modified", "additions": 0, "deletions": 0}, {"nofile": 1}],
        "commits": [{"sha": "abc1234567", "commit": {"message": "Fix it\n\nlong body", "author": {"name": "Ada", "date": "2026-10-10T10:00:00Z"}}}],
    })
    assert [(f.path, f.status, f.additions, bool(f.patch)) for f in cmp.files] == [("a/b.py", "added", 3, True), ("img.png", "modified", 0, False)]
    assert (cmp.commits[0].message, cmp.commits[0].author, cmp.ahead, cmp.behind) == ("Fix it", "Ada", 2, 1)
    assert cmp.commits[0].date.year == 2026 and parse_compare("junk").error
    assert parse_tree({"tree": [{"type": "tree", "path": "src"}, {"type": "blob", "path": "src/b.py"}, {"type": "blob", "path": "a.py"}], "truncated": True}) == (["a.py", "src/b.py"], True)

    # the gh calls: errors read plainly, binary and huge files are described rather than shown
    monkeypatch.setattr(repoinfo, "gh_available", lambda: True)
    monkeypatch.setattr(repoinfo, "_gh", lambda args, timeout=40: (False, "gh: Not Found (HTTP 404)"))
    assert "isn't there" in repoinfo.gh_compare("o/r", "dev", "claude/x").error
    monkeypatch.setattr(repoinfo, "_gh", lambda args, timeout=40: (True, "ab\x00cd"))
    assert repoinfo.gh_file("o/r", "x.bin", "dev") == ("", "a binary file")
    monkeypatch.setattr(repoinfo, "_gh", lambda args, timeout=40: (True, "x" * (repoinfo.MAX_FILE_CHARS + 10)))
    text, error = repoinfo.gh_file("o/r", "big.txt", "dev")
    assert not error and text.endswith("to see the rest") and len(text) < repoinfo.MAX_FILE_CHARS + 100
    seen = []
    monkeypatch.setattr(repoinfo, "_gh", lambda args, timeout=40: (seen.append(args) or (True, "{}")))
    repoinfo.gh_compare("o/r", "dev", "claude/feature x")
    assert seen[0][1] == "repos/o/r/compare/dev...claude/feature%20x"
    monkeypatch.setattr(repoinfo, "gh_available", lambda: False)
    assert "gh CLI" in repoinfo.gh_compare("o/r", "a", "b").error


def test_patch_and_tree_rendering():
    from textual.widgets import Tree

    from ccweb.panes import build_tree, render_file, render_patch
    from ccweb.repoinfo import ChangedFile

    patch = "@@ -10,3 +10,4 @@ def f():\n context\n-old\n+new\n+more\n\\ No newline at end of file"
    text = render_patch(ChangedFile("src/x.py", "modified", 2, 1, patch)).plain.splitlines()
    assert text[0].startswith("src/x.py") and "+2 −1" in text[0]
    body = [l for l in text if l.strip()][1:]
    assert body[0].startswith("@@ -10,3 +10,4 @@")
    assert [l[:6].strip() for l in body[1:5]] == ["10", "", "11", "12"]  # new-file numbers; none beside a removed line
    assert "No text diff" in render_patch(ChangedFile("a.png", "added", 0, 0, "")).plain
    shown = render_file("a.py", "one\ntwo\n\tthree", url="https://x.test/a.py").plain
    assert "1  one" in shown and "3      three" in shown  # tabs become spaces
    assert "Can't show" in render_file("a.bin", "", "a binary file").plain

    tree = Tree("t")
    build_tree(tree, [("docs/a.md", "docs/a.md", None), ("src/x/y/z.py", "src/x/y/z.py", None), ("src/x/y/w.py", "src/x/y/w.py", None), ("README.md", "README.md", None)], expand=True)
    labels = [str(n.label) for n in tree.root.children]
    assert labels == ["docs", "src/x/y", "README.md"]  # folders first; a chain of single-child folders is one row
    assert [str(n.label) for n in tree.root.children[1].children] == ["w.py", "z.py"]
    assert tree.root.children[2].data == "README.md" and not tree.root.children[2].allow_expand
