import asyncio
import json
from datetime import datetime, timedelta, timezone

from rich.console import Console
from textual.widgets import Button, ContentSwitcher, Input, Markdown, OptionList, Static

from ccweb import gitinfo, prinfo
from ccweb.api import Session
from ccweb.app import (
    ChoiceScreen,
    Chip,
    CloudSessionsApp,
    ConfirmScreen,
    FormScreen,
    HelpScreen,
    MenuScreen,
    PullRequestScreen,
    ShareScreen,
)
from ccweb.prinfo import BranchStatus, Check, Details, PullRequest
from ccweb.claude_cli import create_session_interactive, send_followup
from ccweb.config import load_config
from ccweb.store import Store
from fakes import FakeServer, make_fake_claude

NEEDS = "session_01NeedsInputAAAA"
RUNNING = "session_01RunningBBBBBBB"
REVIEW = "session_01ReviewGGGGGGGG"
ARCHIVED = "session_01ArchivedCCCCC"


def transcript_text(app) -> str:
    return app.transcript_text()


def plain(widget: Static) -> str:
    rendered = widget.render()
    if hasattr(rendered, "plain"):
        return rendered.plain
    console = Console(width=200, record=True, file=open("/dev/null", "w"))
    console.print(rendered)
    return console.export_text()


def sidebar_ids(app) -> list[str]:
    return [o.id for o in app.query_one("#sessions", OptionList).options if o.id and not o.id.startswith("__")]


def setup(tmp_path, monkeypatch, token="test-token"):
    server = FakeServer()
    claude, calls = make_fake_claude(tmp_path)
    monkeypatch.setenv("CCWEB_BASE_URL", server.url)
    monkeypatch.setenv("CCWEB_ACCESS_TOKEN", token)
    monkeypatch.setenv("CCWEB_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("HOME", str(tmp_path))  # no real ~/.claude.json for the greeting
    monkeypatch.delenv("CCWEB_ORG_UUID", raising=False)
    monkeypatch.setattr(gitinfo, "local_repo", lambda cwd: "")
    monkeypatch.setattr(gitinfo, "default_branch", lambda repo: "main")
    monkeypatch.setattr(gitinfo, "list_branches", lambda repo: ["main", "dev", "feature/x"])
    monkeypatch.setattr(prinfo, "gh_available", lambda: False)  # tests never shell out to the real gh
    cfg = load_config(str(tmp_path / "missing.toml"))
    app = CloudSessionsApp(cfg, store=Store(tmp_path / "state.json"))
    return server, calls, app


async def wait_for(pilot, condition, tries=80):
    for _ in range(tries):
        await pilot.pause(0.05)
        if condition():
            return
    raise AssertionError("timed out waiting for condition")


def test_lists_reads_and_replies(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.sessions)
            assert app.api_ok
            # Opens on the home page, with nothing selected.
            assert app.selected is None
            assert app.query_one("#views", ContentSwitcher).current == "home"
            # Sidebar: newest activity first (the web's order), archived hidden by default.
            assert sidebar_ids(app) == [REVIEW, NEEDS, RUNNING]
            option_ids = [o.id for o in app.query_one("#sessions", OptionList).options]
            assert option_ids[-1] == "__load__"  # the server has another page
            # Home leads with sessions that need you, then ones ready for review.
            home = [o.id for o in app.query_one("#attention", OptionList).options]
            assert home == [NEEDS, REVIEW, RUNNING]
            await pilot.pause(0.1)

            app.query_one("#sessions", OptionList).focus()
            for _ in range(4):
                await pilot.press("down")
                await pilot.pause(0.05)
                if app.selected == NEEDS:
                    break
            assert app.selected == NEEDS
            assert app.query_one("#views", ContentSwitcher).current == "convo"
            await wait_for(pilot, lambda: "mock the clock" in transcript_text(app))
            text = transcript_text(app)
            assert "Please fix the flaky test" in text
            assert "Bash  pytest -x tests/test_import.py" in text
            assert "1 failed, 12 passed" in text
            header = plain(app.query_one("#session-header", Static))
            assert "example-org/example-repo ⎇ dev" in header
            assert "example-org/other-repo" in header
            assert "Sonnet 5.5" in header and "medium" in header and "auto" in header
            assert "Needs input" in header

            app.query_one("#composer", Input).focus()
            await pilot.press(*"mock the clock")
            await pilot.press("enter")
            await wait_for(pilot, calls.exists)
            await pilot.pause(0.3)
            logged = [json.loads(l) for l in calls.read_text().splitlines()]
            assert logged == [{"sid": NEEDS, "msg": "mock the clock"}]
            # After a reply the session shows as working until the server says otherwise.
            await wait_for(pilot, lambda: "working" in plain(app.query_one("#activity", Static)).lower())

            await pilot.press("escape")
            await pilot.press("v")
            await pilot.pause(0.1)
            assert ARCHIVED in sidebar_ids(app)
            app.save_screenshot(str(tmp_path / "shot.svg"))

    asyncio.run(run())
    sent = [r for r in server.requests if "headers" in r]
    req = sent[0]["headers"]
    assert req["anthropic-beta"] == "ccr-byoc-2025-07-29"
    assert req["anthropic-version"] == "2023-06-01"
    assert req["x-organization-uuid"] == "11111111-2222-3333-4444-555555555555"
    assert req["user-agent"].startswith("ccweb/")
    server.close()


def test_load_more_and_show_more(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.sessions)
            now = datetime.now(timezone.utc)
            for n in range(30):
                sid = f"session_01Filler{n:02d}XXXXXXX"
                app.sessions[sid] = Session(id=sid, title=f"Filler {n}", updated_at=now - timedelta(minutes=n))
            app._render_list()
            ids = [o.id for o in app.query_one("#sessions", OptionList).options]
            assert len([i for i in ids if not i.startswith("__")]) == 20
            assert "__more__" in ids and ids[-1] == "__load__"
            app.query_one("#sessions", OptionList).focus()
            app.visible_limit += 20
            app._render_list()
            assert len(sidebar_ids(app)) == 33  # 30 fillers + the 3 live fakes, 40 now allowed
            app.load_more()
            await wait_for(pilot, lambda: "session_01OlderFFFFFFFFF" in app.sessions)
            assert not app.has_more

    asyncio.run(run())
    server.close()


def test_unreachable_api_falls_back(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch, token="wrong")
    app.store.add_known("session_01SavedHereEEEEE", title="Saved by hand")

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.loaded_once)
            assert not app.api_ok
            banner = app.query_one("#banner", Static)
            assert banner.has_class("show")
            assert "HTTP 401" in str(banner.render())
            assert sidebar_ids(app) == ["session_01SavedHereEEEEE"]

    asyncio.run(run())
    server.close()


def test_send_and_create_via_cli(tmp_path):
    claude, calls = make_fake_claude(tmp_path)
    ok = send_followup(str(claude), RUNNING, "hello\nthere")
    assert ok.ok and ok.url.endswith(RUNNING)
    bad = send_followup(str(claude), ARCHIVED, "hi")
    assert not bad.ok and "archived" in bad.error
    missing = send_followup(str(tmp_path / "nope"), RUNNING, "hi")
    assert not missing.ok and "PATH" in missing.error
    code, sid = create_session_interactive(str(claude), "do a thing", str(tmp_path))
    assert code == 0 and sid == "session_01CreatedDDDDDD"


def test_help_screen_opens_and_closes(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("f1")  # works from inside the prompt box
            await pilot.pause(0.1)
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause(0.1)
            await pilot.press("escape", "question_mark")  # esc leaves the prompt, then ? opens help
            await pilot.pause(0.1)
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, HelpScreen)

    asyncio.run(run())
    server.close()


def chip(app, what, index=-1) -> Chip:
    return next(c for c in app.query(Chip) if c.what == what and c.index == index)


def test_new_session_defaults_and_create(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.envs)
            # Bridge (Remote Control) environments can't host new cloud sessions.
            assert [e.name for e in app.envs] == ["Default", "Unrestricted"]
            assert app.draft["env"] == "env_01DefaultAAAA"
            assert (app.draft["model"], app.draft["effort"], app.draft["mode"]) == ("claude-sonnet-5-5", "medium", "auto")
            await wait_for(pilot, lambda: app.sessions)

            # With no saved choices the form starts from your most recent session, like the web does.
            assert app.draft["env"] == "env_01DefaultAAAA"
            await pilot.pause(0.3)
            # (a repo with no recorded branch gets its default looked up, without being saved as your choice)
            assert app.draft["repos"] == [
                {"repo": "example-org/example-repo", "branch": "dev"},
                {"repo": "example-org/other-repo", "branch": "main"},
            ]
            assert "repos" not in app.store.prefs
            labels = [str(c.label) for c in app.query(Chip)]
            assert labels[:6] == ["☁ Default", "</> example-repo", "⎇ dev", "×", "</> other-repo", "⎇ main"]
            # A repo you've never used gets its default branch looked up.
            app._set_repo(None, "https://github.com/example-org/third-repo.git")
            await pilot.pause(0.3)
            assert app.draft["repos"][2] == {"repo": "example-org/third-repo", "branch": "main"}

            # Every setting is adjustable: mode, model and effort through their pickers.
            chip(app, "mode").press()
            await wait_for(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            for _ in range(3):
                await pilot.press("down")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not isinstance(app.screen, ChoiceScreen))
            assert app.draft["mode"] == "plan"

            chip(app, "effort").press()
            await wait_for(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await pilot.press("down", "enter")  # medium -> high
            await wait_for(pilot, lambda: not isinstance(app.screen, ChoiceScreen))
            assert app.draft["effort"] == "high"

            chip(app, "model").press()
            await wait_for(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await pilot.press("tab")
            await pilot.press(*"claude-opus-5-5", "enter")  # typed model ID
            await wait_for(pilot, lambda: not isinstance(app.screen, ChoiceScreen))
            assert app.draft["model"] == "claude-opus-5-5"

            chip(app, "env").press()
            await wait_for(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await pilot.press("down", "enter")
            await wait_for(pilot, lambda: not isinstance(app.screen, ChoiceScreen))
            assert app.draft["env"] == "env_01UnrestrictedB"

            chip(app, "branch", 1).press()
            await wait_for(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await pilot.press("down", "down", "enter")  # current is main; then dev, feature/x
            await wait_for(pilot, lambda: not isinstance(app.screen, ChoiceScreen))
            assert app.draft["repos"][1]["branch"] == "feature/x"

            await pilot.pause(0.2)
            chip(app, "remove", 0).press()
            await pilot.pause(0.3)
            assert [r["repo"] for r in app.draft["repos"]] == ["example-org/other-repo", "example-org/third-repo"]
            app._set_repo(None, "example-org/example-repo")  # back again: starts from the branch you last used
            await pilot.pause(0.2)

            app.query_one("#prompt", Input).focus()
            await pilot.press(*"Fix the flaky test", "enter")
            await wait_for(pilot, lambda: server.created)
            await wait_for(pilot, lambda: app.selected == "session_01CreatedDDDDDD")
            assert app.query_one("#views", ContentSwitcher).current == "convo"
            await wait_for(pilot, lambda: "working" in plain(app.query_one("#activity", Static)).lower())

    asyncio.run(run())
    body = server.created[0]
    assert body["environment_id"] == "env_01UnrestrictedB"
    ctx = body["session_context"]
    assert ctx["sources"] == [
        {"type": "git_repository", "url": "https://github.com/example-org/other-repo", "revision": "feature/x"},
        {"type": "git_repository", "url": "https://github.com/example-org/third-repo", "revision": "main"},
        {"type": "git_repository", "url": "https://github.com/example-org/example-repo", "revision": "dev"},
    ]
    assert [o["git_info"]["repo"] for o in ctx["outcomes"]] == [
        "example-org/other-repo",
        "example-org/third-repo",
        "example-org/example-repo",
    ]
    assert (ctx["model"], ctx["effort_level"]) == ("claude-opus-5-5", "high")
    mode, message = body["events"]
    assert mode["data"]["request"] == {"subtype": "set_permission_mode", "mode": "plan"}
    assert message["data"]["message"] == {"role": "user", "content": "Fix the flaky test"}
    assert body["title"] == "Fix the flaky test"
    # The choices are remembered for next time.
    saved = Store(tmp_path / "state.json").prefs
    assert (saved["mode"], saved["effort"], saved["env"]) == ("plan", "high", "env_01UnrestrictedB")
    server.close()


def test_new_session_error_keeps_the_prompt(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.envs)
            app.draft["env"] = ""  # nothing chosen: refuse locally rather than send a bad request
            app.query_one("#prompt", Input).focus()
            await pilot.press(*"hello", "enter")
            await pilot.pause(0.3)
            assert not server.created
            assert app.query_one("#prompt", Input).value == "hello"
            assert app.selected is None

    asyncio.run(run())
    server.close()


def test_pr_bar_checks_and_diff_screen(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(prinfo, "gh_available", lambda: True)
    monkeypatch.setattr(
        prinfo,
        "gh_details",
        lambda url: Details(
            checks=[
                Check("build", "success"),
                Check("tests", "failure"),
                Check("lint", "pending"),
            ],
            review="CHANGES_REQUESTED",
            mergeable="MERGEABLE",
            files=[("src/importer.py", 30, 2), ("tests/test_importer.py", 10, 1)],
        ),
    )
    diff = "diff --git a/src/importer.py b/src/importer.py\n+++ b/src/importer.py\n@@ -1,2 +1,3 @@\n context\n+added line\n-removed line"
    monkeypatch.setattr(prinfo, "gh_diff", lambda url, max_lines=4000: (diff, ""))

    async def run():
        async with app.run_test(size=(150, 45)) as pilot:
            await wait_for(pilot, lambda: app.sessions)
            app._select(NEEDS)
            await wait_for(pilot, lambda: "example-org/example-repo#77" in plain(app.query_one("#pr-bar", Static)))
            # CI comes from gh, since the web endpoint leaves checks empty; review state is the fresher gh one.
            await wait_for(pilot, lambda: "CI" in plain(app.query_one("#pr-bar", Static)))
            bar = plain(app.query_one("#pr-bar", Static))
            assert "Draft" in bar and "+40 −3" in bar
            assert "CI ✓1 ✗1 ◔1" in bar
            assert "changes requested" in bar
            assert "Fix flaky CGM import test" in bar
            assert "d shows checks and the diff" in bar
            header = plain(app.query_one("#session-header", Static))
            assert "Needs input" in header  # a draft PR doesn't override a pending question

            app.query_one("#sessions", OptionList).focus()
            await pilot.press("d")
            await wait_for(pilot, lambda: isinstance(app.screen, PullRequestScreen))
            await wait_for(pilot, lambda: "added line" in app.screen.plain_text())
            text = app.screen.plain_text()
            assert "example-org/example-repo#77  Draft" in text
            assert "into dev" in text and "mergeable" in text
            # failures are listed before passes
            assert text.index("✗ tests") < text.index("◔ lint") < text.index("✓ build")
            assert "Files changed (2)" in text and "src/importer.py" in text
            assert "+added line" in text and "-removed line" in text
            await pilot.press("escape")
            await wait_for(pilot, lambda: not isinstance(app.screen, PullRequestScreen))

    asyncio.run(run())
    server.close()


def test_pr_state_drives_sidebar_icon_and_home(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(150, 45)) as pilot:
            await wait_for(pilot, lambda: app.sessions and app.branches)
            # Batched lookup: by session id, and by (repo, branch) for a branch with no PR yet.
            assert app._pr_state(NEEDS) == "draft"
            assert app._pr_state(RUNNING) == "merged"
            assert app._pr_state(REVIEW) == ""
            assert [s.branch for s in prinfo.relevant(app.branches[REVIEW])] == ["claude/other-work"]

            quiet = Session(id="session_01Quiet", title="Quiet", status="idle", unread=False)
            app.sessions[quiet.id] = quiet
            for state, glyph, colour in (("draft", "⎇", "#b9b3a8"), ("merged", "⎇", "#a78bfa"), ("", "○", "#8f897f")):
                app.branches[quiet.id] = (
                    [BranchStatus("o/r", "b", True, 1, "main", PullRequest("o/r", 1, state=state))] if state else []
                )
                row = app._row(quiet)
                assert row.plain.startswith(glyph + " ")
                assert row.spans[0].style == colour
            # unread beats the PR icon, as on the web
            quiet.unread = True
            app.branches[quiet.id] = [BranchStatus("o/r", "b", True, 1, "main", PullRequest("o/r", 1, state="merged"))]
            assert app._row(quiet).plain.startswith("● ")

    asyncio.run(run())
    sent = [r for r in server.requests if r.get("path", "").startswith("/v1/code/github/get-batch-branch-status")]
    assert sent and all(len(r["body"]["repo_branches"]) <= 50 for r in sent)
    assert NEEDS in sent[0]["body"]["session_ids"] and sent[0]["body"]["discover_session_prs"] is True
    server.close()


def option_titles(app) -> list[str]:
    return [str(o.prompt) for o in app.query_one("#sessions", OptionList).options if o.id and not o.id.startswith("__")]


async def open_menu_for(pilot, app, sid):
    app.open_menu(sid)
    await wait_for(pilot, lambda: isinstance(app.screen, MenuScreen))


def test_menu_rename_mark_read_and_copy(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(150, 45)) as pilot:
            await wait_for(pilot, lambda: app.sessions)
            # m opens the menu for the highlighted sidebar row
            app.query_one("#sessions", OptionList).focus()
            for _ in range(4):
                await pilot.press("down")
                await pilot.pause(0.05)
                if app.selected == NEEDS:
                    break
            await pilot.press("m")
            await wait_for(pilot, lambda: isinstance(app.screen, MenuScreen))
            assert app.screen.heading == "Fix flaky CGM import test"
            enabled = {i.id: i.enabled for i in app.screen.items if i}
            assert enabled == {
                "open": True, "pin": False, "read": True, "rename": True, "share": True,
                "copy": True, "group": False, "archive": True, "delete": True,
            }

            # Pin and groups can't be done through anything ccweb can reach, so their keys do nothing
            await pilot.press("p")
            await pilot.pause(0.1)
            assert isinstance(app.screen, MenuScreen)

            await pilot.press("u")  # mark as read
            await wait_for(pilot, lambda: not isinstance(app.screen, MenuScreen))
            await wait_for(pilot, lambda: ("POST", f"/v1/code/sessions/{NEEDS}/mark_read", {}) in server.actions)
            await wait_for(pilot, lambda: not app.sessions[NEEDS].unread)

            await open_menu_for(pilot, app, NEEDS)
            assert not {i.id: i.enabled for i in app.screen.items if i}["read"]  # nothing left to mark
            await pilot.press("r")  # rename
            await wait_for(pilot, lambda: isinstance(app.screen, FormScreen))
            field = app.screen.query_one("#field-title", Input)
            assert field.value == "Fix flaky CGM import test"
            field.value = "Fix the CGM import flake"
            await pilot.press("enter")
            await wait_for(pilot, lambda: ("PUT", f"/v1/code/sessions/{NEEDS}", {"title": "Fix the CGM import flake"}) in server.actions)
            await wait_for(pilot, lambda: any("Fix the CGM import flake" in t for t in option_titles(app)))
            assert app.sessions[NEEDS].title == "Fix the CGM import flake"

            await open_menu_for(pilot, app, NEEDS)
            await pilot.press("c")  # copy link
            await wait_for(pilot, lambda: not isinstance(app.screen, MenuScreen))
            assert app.clipboard == f"https://claude.ai/code/{NEEDS}"

    asyncio.run(run())
    server.close()


def test_menu_archive_and_delete_ask_first(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(150, 45)) as pilot:
            await wait_for(pilot, lambda: app.sessions)
            app._select(RUNNING)
            await pilot.pause(0.2)

            await open_menu_for(pilot, app, RUNNING)
            await pilot.press("a")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert app.screen.focused.id == "cancel"  # the safe answer is the default
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not server.actions and RUNNING in sidebar_ids(app)

            await open_menu_for(pilot, app, RUNNING)
            await pilot.press("a")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            app.screen.query_one("#confirm", Button).press()
            await wait_for(pilot, lambda: ("POST", f"/v1/code/sessions/{RUNNING}/archive", {}) in server.actions)
            await wait_for(pilot, lambda: RUNNING not in sidebar_ids(app))
            assert app.selected is None and app.query_one("#views", ContentSwitcher).current == "home"

            await open_menu_for(pilot, app, NEEDS)
            await pilot.press("d")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert "can't be undone" in str(app.screen.query_one("#form-note", Static).render())
            app.screen.query_one("#confirm", Button).press()
            await wait_for(pilot, lambda: ("DELETE", f"/v1/sessions/{NEEDS}", {}) in server.actions)
            await wait_for(pilot, lambda: NEEDS not in app.sessions)

            # a failure leaves everything as it was and says why
            app.sessions["session_01FailXXXXXXXXXX"] = Session(id="session_01FailXXXXXXXXXX", title="Doomed")
            app.archive_task("session_01FailXXXXXXXXXX")
            await pilot.pause(0.5)
            assert not app.sessions["session_01FailXXXXXXXXXX"].archived
            assert any("not allowed" in str(n.message) for n in app._notifications)

    asyncio.run(run())
    server.close()


def test_right_click_and_share_screen(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(150, 45)) as pilot:
            await wait_for(pilot, lambda: app.sessions)
            assert sidebar_ids(app) == [REVIEW, NEEDS, RUNNING]
            await pilot.click("#sessions", offset=(4, 1), button=3)  # second row
            await wait_for(pilot, lambda: isinstance(app.screen, MenuScreen))
            assert app.screen.heading == "Fix flaky CGM import test"
            assert app.selected is None  # right-clicking doesn't open the session
            await pilot.press("escape")
            await pilot.pause(0.1)

            # on the open conversation, right-clicking the transcript opens that session's menu
            app._select(NEEDS)
            await wait_for(pilot, lambda: "mock the clock" in transcript_text(app))
            await pilot.click("#transcript", offset=(10, 2), button=3)
            await wait_for(pilot, lambda: isinstance(app.screen, MenuScreen))
            await pilot.press("escape")
            await pilot.pause(0.1)
            # and so does the ⋮ button
            next(c for c in app.query(Chip) if c.what == "menu").press()
            await wait_for(pilot, lambda: isinstance(app.screen, MenuScreen))
            await pilot.press("escape")
            await pilot.pause(0.1)

            await open_menu_for(pilot, app, NEEDS)
            app.screen.query_one("#menu-items", OptionList).highlighted = 6  # Share
            await pilot.press("enter")
            await wait_for(pilot, lambda: isinstance(app.screen, ShareScreen))
            await wait_for(pilot, lambda: "Only you" in str(app.screen.query_one("#share-state", Static).render()))
            assert "Everyone in your organization" in str(app.screen.query_one("#share-state", Static).render())
            await pilot.press("escape")

            # Open in > claude.ai, and the session's PR on GitHub
            opened = []
            monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
            await wait_for(pilot, lambda: app.branches.get(NEEDS))
            await open_menu_for(pilot, app, NEEDS)
            await pilot.press("enter")  # "Open in" is first
            await wait_for(pilot, lambda: isinstance(app.screen, MenuScreen) and app.screen.heading == "Open in")
            labels = [i.label for i in app.screen.items if i]
            assert labels == ["claude.ai in the browser", "Terminal (claude --teleport)", "GitHub example-org/example-repo#77"]
            app.screen.query_one("#menu-items", OptionList).highlighted = 4
            await pilot.press("enter")
            await wait_for(pilot, lambda: opened == ["https://github.com/example-org/example-repo/pull/77"])

    asyncio.run(run())
    server.close()


def test_links_open_on_click_and_text_can_be_selected(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))

    async def run():
        async with app.run_test(size=(150, 45)) as pilot:
            await wait_for(pilot, lambda: app.sessions)
            app._select(NEEDS)
            await wait_for(pilot, lambda: "for details" in transcript_text(app))
            await wait_for(pilot, lambda: app.branches.get(NEEDS))

            # Claude's reply is Markdown: the bare URL and owner/repo#77 are both links
            block = next(b for b in app.query("#transcript MarkdownParagraph") if "for details" in str(b._render()))
            line = str(block._render())
            await pilot.click(block, offset=(line.index("example.com") + 1, 0))
            await wait_for(pilot, lambda: opened == ["https://example.com/doc"])
            await pilot.click(block, offset=(line.index("repo#77") + 1, 0))
            await wait_for(pilot, lambda: opened[-1] == "https://github.com/example-org/example-repo/issues/77")

            # plain text is clickable too: the session id in the header, and the PR in the PR bar
            header = app.query_one("#session-header", Static)
            rows = str(header.render()).splitlines()
            y = next(i for i, r in enumerate(rows) if NEEDS in r)
            await pilot.click(header, offset=(rows[y].index(NEEDS) + 3, y + 1))  # +1 for the header's top padding
            await wait_for(pilot, lambda: opened[-1] == f"https://claude.ai/code/{NEEDS}")
            bar = app.query_one("#pr-bar", Static)
            text = str(bar.render())
            await pilot.click(bar, offset=(text.index("example-org") + 2, 0))
            await wait_for(pilot, lambda: opened[-1] == "https://github.com/example-org/example-repo/pull/77")
            # a href that isn't a web link is never followed
            before = len(opened)
            app.action_open_link("file:///etc/passwd")
            assert len(opened) == before

            # selecting text: drag across a message, then ctrl+c copies it
            target = next(b for b in app.query("#transcript MarkdownParagraph") if "widen the timeout" in str(b._render()))
            await pilot.mouse_down(target, offset=(0, 0))
            await pilot.hover(target, offset=(18, 0))
            await pilot.mouse_up(target, offset=(18, 0))
            await pilot.pause(0.1)
            selected = app.screen.get_selected_text() or ""
            assert selected.startswith("Should I widen the") and len(selected) >= 12
            # a click that ends a selection isn't also a click on a link
            count = len(opened)
            app.action_open_link("https://example.com/not-opened")
            assert len(opened) == count
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.clipboard == selected

    asyncio.run(run())
    server.close()
