import asyncio
import json

from rich.console import Console

from ccweb.app import CloudSessionsApp
from ccweb.claude_cli import create_session_interactive, send_followup
from ccweb.config import load_config
from ccweb.store import Store
from fakes import FakeServer, make_fake_claude
from textual.widgets import Input, OptionList, RichLog, Static


def transcript_text(app) -> str:
    log = app.query_one("#transcript", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def setup(tmp_path, monkeypatch, token="test-token"):
    server = FakeServer()
    claude, calls = make_fake_claude(tmp_path)
    monkeypatch.setenv("CCWEB_BASE_URL", server.url)
    monkeypatch.setenv("CCWEB_ACCESS_TOKEN", token)
    monkeypatch.setenv("CCWEB_CLAUDE_BIN", str(claude))
    monkeypatch.delenv("CCWEB_ORG_UUID", raising=False)
    cfg = load_config(str(tmp_path / "missing.toml"))
    app = CloudSessionsApp(cfg, store=Store(tmp_path / "state.json"))
    return server, calls, app


def test_lists_reads_and_replies(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch)

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            for _ in range(50):
                await pilot.pause(0.05)
                if app.sessions:
                    break
            assert app.api_ok
            ids = [o.id for o in app.query_one("#sessions", OptionList).options if o.id]
            # needs-input first, archived hidden by default
            assert ids == ["session_01NeedsInputAAAA", "session_01RunningBBBBBBB"]
            assert app.selected == "session_01NeedsInputAAAA"
            for _ in range(50):
                await pilot.pause(0.05)
                if "mock the clock" in transcript_text(app):
                    break
            text = transcript_text(app)
            assert "Please fix the flaky test" in text
            assert "Bash  pytest -x tests/test_import.py" in text
            assert "1 failed, 12 passed" in text
            header = app.query_one("#session-header", Static).render()
            assert "example-org/example-repo" in str(header)

            app.query_one("#composer", Input).focus()
            await pilot.press(*"mock the clock")
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if calls.exists():
                    break
            await pilot.pause(0.3)
            logged = [json.loads(l) for l in calls.read_text().splitlines()]
            assert logged == [{"sid": "session_01NeedsInputAAAA", "msg": "mock the clock"}]

            await pilot.press("escape")
            await pilot.press("v")
            await pilot.pause(0.1)
            ids = [o.id for o in app.query_one("#sessions", OptionList).options if o.id]
            assert "session_01ArchivedCCCCC" in ids
            app.save_screenshot(str(tmp_path / "shot.svg"))

    asyncio.run(run())
    req = server.requests[0]["headers"]
    assert req["anthropic-beta"] == "ccr-byoc-2025-07-29"
    assert req["anthropic-version"] == "2023-06-01"
    assert req["x-organization-uuid"] == "11111111-2222-3333-4444-555555555555"
    assert req["user-agent"].startswith("ccweb/")
    server.close()


def test_unreachable_api_falls_back(tmp_path, monkeypatch):
    server, calls, app = setup(tmp_path, monkeypatch, token="wrong")
    app.store.add_known("session_01SavedHereEEEEE", title="Saved by hand")

    async def run():
        async with app.run_test(size=(140, 40)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if app.loaded_once:
                    break
            assert not app.api_ok
            banner = app.query_one("#banner", Static)
            assert banner.has_class("show")
            assert "HTTP 401" in str(banner.render())
            ids = [o.id for o in app.query_one("#sessions", OptionList).options if o.id]
            assert ids == ["session_01SavedHereEEEEE"]

    asyncio.run(run())
    server.close()


def test_send_and_create_via_cli(tmp_path):
    claude, calls = make_fake_claude(tmp_path)
    ok = send_followup(str(claude), "session_01RunningBBBBBBB", "hello\nthere")
    assert ok.ok and ok.url.endswith("session_01RunningBBBBBBB")
    bad = send_followup(str(claude), "session_01ArchivedCCCCC", "hi")
    assert not bad.ok and "archived" in bad.error
    missing = send_followup(str(tmp_path / "nope"), "session_01RunningBBBBBBB", "hi")
    assert not missing.ok and "PATH" in missing.error
    code, sid = create_session_interactive(str(claude), "do a thing", str(tmp_path))
    assert code == 0 and sid == "session_01CreatedDDDDDD"
