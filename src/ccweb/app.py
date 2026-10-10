"""Terminal client laid out like claude.ai/code: sessions on the left, conversation on the right."""

from __future__ import annotations

import asyncio
import os
import shutil
import webbrowser
from datetime import datetime, timedelta, timezone

from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from . import claude_cli
from .api import (
    ApiError,
    AuthError,
    CloudApi,
    Message,
    Session,
    parse_session_ref,
    web_url,
)
from .store import Store

CLAY = "#d4835f"
SAND = "#e8e4dc"
MUTED = "#8f897f"
AMBER = "#e0b354"
SAGE = "#8db580"
BRICK = "#e06c5f"

THEME = Theme(
    name="ccweb",
    primary=CLAY,
    secondary=MUTED,
    accent=CLAY,
    warning=AMBER,
    error=BRICK,
    success=SAGE,
    foreground=SAND,
    background="#1f1e1c",
    surface="#262522",
    panel="#33312d",
    dark=True,
)

NEEDS_INPUT = {"requires_action", "waiting", "blocked", "needs_input", "input_required", "awaiting_input"}
WORKING = {"running", "working", "active", "busy", "in_progress", "processing"}
FAILED = {"failed", "error", "errored"}


def describe_status(session: Session) -> tuple[str, str, str]:
    """(glyph, colour, label) for a session's status."""
    status = session.status.lower()
    if session.archived:
        return "◌", MUTED, "Archived"
    if status in NEEDS_INPUT:
        return "●", AMBER, "Needs your input"
    if status in WORKING:
        return "●", SAGE, "Working"
    if status in FAILED:
        return "●", BRICK, "Failed"
    if not session.from_server:
        return "○", MUTED, "Saved here, not loaded from claude.ai"
    return "○", MUTED, session.status.replace("_", " ").capitalize() or "Idle"


def relative_time(moment: datetime | None) -> str:
    if moment is None:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600), (86400 * 30, "d", 86400)):
        if seconds < limit:
            return f"{seconds // size}{unit}"
    local = moment.astimezone()
    return local.strftime("%b %-d") if local.year == datetime.now().year else local.strftime("%b %Y")


def recency_group(moment: datetime | None) -> str:
    if moment is None:
        return "Saved on this machine"
    local = moment.astimezone()
    today = datetime.now().astimezone().date()
    if local.date() == today:
        return "Today"
    if local.date() == today - timedelta(days=1):
        return "Yesterday"
    if local.date() > today - timedelta(days=7):
        return "Previous 7 days"
    return "Older"


GROUP_ORDER = ["Needs your input", "Today", "Yesterday", "Previous 7 days", "Older", "Saved on this machine", "Archived"]


class FormScreen(ModalScreen[dict | None]):
    """A small form: a heading, one or more text fields, confirm and cancel."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, heading: str, fields: list[tuple[str, str, str]], confirm: str, note: str = ""):
        super().__init__()
        self.heading, self.fields, self.confirm, self.note = heading, fields, confirm, note

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label(self.heading, id="form-heading")
            if self.note:
                yield Static(self.note, id="form-note")
            for key, label, default in self.fields:
                yield Label(label, classes="form-label")
                yield Input(value=default, id=f"field-{key}")
            with Horizontal(id="form-buttons"):
                yield Button(self.confirm, variant="primary", id="confirm")
                yield Button("Cancel", id="cancel")

    def _values(self) -> dict:
        return {key: self.query_one(f"#field-{key}", Input).value.strip() for key, _, _ in self.fields}

    @on(Button.Pressed, "#confirm")
    @on(Input.Submitted)
    def submit(self) -> None:
        self.dismiss(self._values())

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class CloudSessionsApp(App):
    TITLE = "Cloud sessions"
    CSS = f"""
    Screen {{ layout: vertical; }}
    #body {{ height: 1fr; }}
    #sidebar {{ width: 42; background: $surface; padding: 0 1; border-right: tall $panel; }}
    #brand {{ padding: 1 1 0 1; color: {SAND}; text-style: bold; }}
    #account {{ padding: 0 1; color: {MUTED}; }}
    #new {{ width: 100%; margin: 1 0 0 0; }}
    #search {{ margin: 1 0 0 0; }}
    #sessions {{ height: 1fr; background: $surface; border: none; margin-top: 1; padding: 0; }}
    #sessions:focus {{ border: none; }}
    #sessions > .option-list--option-highlighted {{ background: $panel; text-style: none; }}
    #sessions:focus > .option-list--option-highlighted {{ background: {CLAY} 28%; }}
    #sessions > .option-list--option-hover {{ background: $panel 60%; }}
    #main {{ padding: 0 2; }}
    #banner {{ display: none; height: auto; margin-top: 1; padding: 0 1; background: {BRICK} 18%; color: {SAND}; }}
    #banner.show {{ display: block; }}
    #session-header {{ height: auto; padding: 1 0; border-bottom: hkey $panel; }}
    #transcript {{ height: 1fr; background: $background; border: none; padding: 1 0 0 0; scrollbar-size-vertical: 1; }}
    #composer {{ margin: 1 0; }}
    #form {{ width: 76; height: auto; padding: 1 2; background: $surface; border: tall {CLAY}; }}
    FormScreen {{ align: center middle; }}
    #form-heading {{ text-style: bold; margin-bottom: 1; }}
    #form-note {{ color: {MUTED}; margin-bottom: 1; }}
    .form-label {{ color: {MUTED}; margin-top: 1; }}
    #form-buttons {{ height: auto; margin-top: 1; align-horizontal: right; }}
    #form-buttons Button {{ margin-left: 2; }}
    """

    BINDINGS = [
        Binding("n", "new_session", "New"),
        Binding("i", "add_session", "Add by link"),
        Binding("o", "open_web", "Open on web"),
        Binding("t", "teleport", "Teleport"),
        Binding("y", "copy_link", "Copy link"),
        Binding("c", "set_checkout", "Checkout path", show=False),
        Binding("slash", "search", "Search"),
        Binding("v", "toggle_archived", "Archived"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("x", "forget", "Forget", show=False),
        Binding("escape", "focus_sessions", "Sessions", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: dict, store: Store | None = None):
        super().__init__()
        self.cfg = cfg
        self.store = store or Store()
        self.api = CloudApi(cfg)
        self.claude_bin = cfg["claude"]["bin"]
        self.sessions: dict[str, Session] = {}
        self.selected: str | None = None
        self.rendered_keys: set[str] = set()
        self.events_error_for: str | None = None
        self.api_ok = False
        self.loaded_once = False
        self.show_archived = False
        self.filter_text = ""
        self._suppress_highlight = False
        self._after_tool = False

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("Cloud sessions", id="brand")
                yield Static("Loading…", id="account")
                yield Button("+ New session", id="new", variant="primary", compact=True)
                yield Input(placeholder="Search sessions", id="search")
                yield OptionList(id="sessions")
            with Vertical(id="main"):
                yield Static("", id="banner")
                yield Static("", id="session-header")
                yield RichLog(id="transcript", wrap=True, min_width=20)
                yield Input(placeholder="Reply to this session", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "ccweb"
        self._render_header()
        if not shutil.which(self.claude_bin):
            self._banner(
                f"`{self.claude_bin}` isn't on your PATH. Install Claude Code or set CCWEB_CLAUDE_BIN; "
                "sending, creating and teleporting need it."
            )
        self.refresh_sessions()
        self.set_interval(float(self.cfg["poll"]["sessions_seconds"]), self.refresh_sessions)
        self.set_interval(float(self.cfg["poll"]["events_seconds"]), self.refresh_events)
        self.query_one("#sessions", OptionList).focus()

    async def on_unmount(self) -> None:
        await self.api.aclose()

    # ------------------------------------------------------------------ data

    @work(exclusive=True, group="sessions")
    async def refresh_sessions(self) -> None:
        try:
            remote = await self.api.list_sessions()
            self.api_ok = True
            self._banner(None)
        except (ApiError, AuthError) as exc:
            remote = []
            self.api_ok = False
            self._banner(
                f"Couldn't read your sessions from claude.ai/code ({exc}). "
                "Sending, teleport and open-on-web still work through the claude CLI. "
                "Run `ccweb probe` to check the endpoints."
            )
        merged = {s.id: s for s in remote}
        for sid, info in self.store.known.items():
            if sid not in merged:
                added = info.get("added_at")
                merged[sid] = Session(
                    id=sid,
                    title=info.get("title", ""),
                    from_server=False,
                    created_at=datetime.fromtimestamp(added, tz=timezone.utc) if self.api_ok and added else None,
                )
        self.sessions = merged
        self.loaded_once = True
        live = sum(1 for s in remote if not s.archived)
        account = f"{live} on claude.ai" if self.api_ok else "claude.ai not reachable"
        self.query_one("#account", Static).update(account)
        self._render_list()
        self._render_header()

    @work(exclusive=True, group="events")
    async def refresh_events(self) -> None:
        sid = self.selected
        if not sid or not self.api_ok:
            return
        try:
            messages = await self.api.list_events(sid)
        except (ApiError, AuthError) as exc:
            if self.selected == sid and self.events_error_for != sid:
                self.events_error_for = sid
                self._note(f"Couldn't load this conversation: {exc}")
            return
        if self.selected != sid:
            return
        if not messages and not self.rendered_keys:
            self._note("No messages yet.")
            self.rendered_keys.add("__empty__")
            return
        for message in messages:
            if message.key not in self.rendered_keys:
                self.rendered_keys.add(message.key)
                self._write_message(message)

    # ------------------------------------------------------------------ rendering

    def _banner(self, text: str | None) -> None:
        banner = self.query_one("#banner", Static)
        banner.update(text or "")
        banner.set_class(bool(text), "show")

    def _visible_sessions(self) -> list[Session]:
        needle = self.filter_text.lower()
        out = []
        for session in self.sessions.values():
            if session.archived and not self.show_archived:
                continue
            haystack = f"{session.title} {session.repo} {session.id}".lower()
            if needle and needle not in haystack:
                continue
            out.append(session)
        return out

    def _group_for(self, session: Session) -> str:
        if session.archived:
            return "Archived"
        if session.status.lower() in NEEDS_INPUT:
            return "Needs your input"
        return recency_group(session.last_activity)

    def _row(self, session: Session) -> Text:
        glyph, colour, _ = describe_status(session)
        title = session.title or session.id
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append(f"{glyph} ", style=colour)
        row.append(title, style=SAND)
        row.append("\n  ")
        row.append(session.repo or "repository unknown", style=MUTED)
        when = relative_time(session.last_activity)
        if when:
            row.append(f"  {when}", style=MUTED)
        return row

    def _render_list(self) -> None:
        option_list = self.query_one("#sessions", OptionList)
        groups: dict[str, list[Session]] = {}
        for session in self._visible_sessions():
            groups.setdefault(self._group_for(session), []).append(session)
        floor = datetime.min.replace(tzinfo=timezone.utc)
        options: list[Option] = []
        for name in GROUP_ORDER:
            members = sorted(groups.get(name, []), key=lambda s: s.last_activity or floor, reverse=True)
            if not members:
                continue
            options.append(Option(Text(name, style=f"bold {MUTED}"), disabled=True))
            options.extend(Option(self._row(s), id=s.id) for s in members)
        visible_ids = [o.id for o in options if o.id]
        target = self.selected if self.selected in visible_ids else (visible_ids[0] if visible_ids else None)
        self._suppress_highlight = True
        try:
            option_list.clear_options()
            option_list.add_options(options)
            if target:
                option_list.highlighted = option_list.get_option_index(target)
        finally:
            self._suppress_highlight = False
        if target and self.selected is None:
            self._select(target)
        if not options and self.loaded_once:
            empty = "No matches." if self.filter_text else "No sessions yet. Press n to start one."
            option_list.add_option(Option(Text(empty, style=MUTED), disabled=True))

    def _render_header(self) -> None:
        header = self.query_one("#session-header", Static)
        session = self.sessions.get(self.selected or "")
        if session is None:
            header.update(
                Text.assemble(
                    ("Pick a session on the left", f"bold {SAND}"),
                    ("\nn starts a new cloud session in a local repo.  i adds one by its claude.ai/code link.", MUTED),
                )
            )
            return
        glyph, colour, label = describe_status(session)
        text = Text()
        text.append(session.title or "Untitled session", style=f"bold {SAND}")
        text.append("\n")
        text.append(f"{glyph} {label}", style=colour)
        if session.repo:
            text.append(f"    {session.repo}", style=SAND)
        if session.branch:
            text.append(f"  on {session.branch}", style=MUTED)
        text.append(f"\n{session.id}", style=MUTED)
        checkout = self._checkout(session)
        hint = f"    teleports into {checkout}" if checkout else "    press c to set the local checkout for teleport"
        text.append(hint, style=MUTED)
        header.update(text)

    def _note(self, text: str) -> None:
        self.query_one("#transcript", RichLog).write(Text(text, style=f"italic {MUTED}"))

    def _write_message(self, message: Message) -> None:
        log = self.query_one("#transcript", RichLog)
        if message.kind == "text" and self._after_tool:
            log.write(Text(""))
        self._after_tool = message.kind in {"tool_use", "tool_result"}
        if message.kind == "text":
            if message.role == "assistant":
                log.write(Text("Claude", style=f"bold {CLAY}"))
                log.write(Markdown(message.text))
            elif message.role == "user":
                log.write(Text("You", style=f"bold {SAND}"))
                log.write(Text(message.text, style=SAND))
            else:
                log.write(Text(f"{message.role}: {message.text}", style=MUTED))
            log.write(Text(""))
        elif message.kind == "tool_use":
            log.write(Text(f"  ⚙ {message.text}", style=MUTED, no_wrap=True, overflow="ellipsis"))
        elif message.kind == "tool_result":
            log.write(Text(f"    ↳ {message.text}", style=MUTED, no_wrap=True, overflow="ellipsis"))
        else:
            log.write(Text(f"  {message.text}", style=f"italic {MUTED}"))

    # ------------------------------------------------------------------ selection

    def _select(self, sid: str | None) -> None:
        if sid == self.selected:
            return
        self.selected = sid
        self.rendered_keys = set()
        self.events_error_for = None
        self._after_tool = False
        self.query_one("#transcript", RichLog).clear()
        self._render_header()
        session = self.sessions.get(sid or "")
        if session is not None and not self.api_ok:
            self._note(
                "The conversation can't be loaded because claude.ai isn't reachable. "
                "You can still reply below, teleport (t) or open it on the web (o)."
            )
        self.refresh_events()

    @on(OptionList.OptionHighlighted, "#sessions")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if not self._suppress_highlight and event.option.id:
            self._select(event.option.id)

    @on(OptionList.OptionSelected, "#sessions")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self._select(event.option.id)
            self.query_one("#composer", Input).focus()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self._render_list()

    @on(Input.Submitted, "#search")
    def _search_done(self) -> None:
        self.query_one("#sessions", OptionList).focus()

    # ------------------------------------------------------------------ actions

    def _current(self) -> Session | None:
        session = self.sessions.get(self.selected or "")
        if session is None:
            self.notify("Pick a session first.", severity="warning")
        return session

    def _checkout_key(self, session: Session) -> str:
        return session.repo or session.id

    def _checkout(self, session: Session) -> str:
        return self.store.checkout_for(self._checkout_key(session))

    @on(Input.Submitted, "#composer")
    def _send(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        session = self._current()
        if not text or session is None:
            return
        event.input.value = ""
        if not self.api_ok:
            self._write_message(Message("local", "user", "text", text))
        self.notify("Sending…", timeout=2)
        self.send_message(session.id, text)

    @work(group="send")
    async def send_message(self, sid: str, text: str) -> None:
        result = await asyncio.to_thread(claude_cli.send_followup, self.claude_bin, sid, text)
        if result.ok:
            self.notify("Sent to the cloud session.")
            await asyncio.sleep(1.5)
            self.refresh_events()
            return
        self.notify(f"Not sent: {result.error}", severity="error", timeout=12)
        composer = self.query_one("#composer", Input)
        if not composer.value:
            composer.value = text

    @on(Button.Pressed, "#new")
    def action_new_session(self) -> None:
        env_id = self.cfg["claude"].get("environment_id", "")
        note = (
            f"Creates the session on self-hosted environment {env_id}."
            if env_id
            else "Runs `claude --cloud` in that folder. The cloud VM clones the folder's GitHub remote at the "
            "current branch, so push local commits first."
        )
        form = FormScreen(
            "New cloud session",
            [("prompt", "Task for Claude", ""), ("cwd", "Local repository folder", os.getcwd())],
            "Start session",
            note,
        )
        self.push_screen(form, self._create_session)

    def _create_session(self, values: dict | None) -> None:
        if not values or not values.get("prompt"):
            return
        cwd = os.path.expanduser(values["cwd"] or os.getcwd())
        if not os.path.isdir(cwd):
            self.notify(f"{cwd} isn't a folder.", severity="error")
            return
        if self.cfg["claude"].get("environment_id"):
            self.create_selfhosted(values["prompt"], cwd)
            return
        try:
            with self.suspend():
                code, sid = claude_cli.create_session_interactive(self.claude_bin, values["prompt"], cwd)
        except SuspendNotSupported:
            self.notify("This terminal can't hand over control. Run `claude --cloud` yourself.", severity="error")
            return
        if sid:
            self._remember_created(sid, values["prompt"], cwd)
        elif code == 0:
            self.notify(
                "The session started, but its ID wasn't in the output. Press i and paste its claude.ai/code link.",
                severity="warning",
                timeout=12,
            )
        else:
            self.notify(f"claude --cloud exited with {code}.", severity="error")

    @work(group="create")
    async def create_selfhosted(self, prompt: str, cwd: str) -> None:
        env_id = self.cfg["claude"]["environment_id"]
        try:
            sid = await asyncio.to_thread(claude_cli.create_session_selfhosted, self.claude_bin, prompt, env_id, cwd)
        except (RuntimeError, OSError) as exc:
            self.notify(f"Couldn't create the session: {exc}", severity="error", timeout=12)
            return
        self._remember_created(sid, prompt, cwd)

    def _remember_created(self, sid: str, prompt: str, cwd: str) -> None:
        sid = parse_session_ref(sid) or sid
        self.store.add_known(sid, title=prompt[:80], source="created")
        self.store.set_checkout(sid, cwd)
        self.notify("Session created.")
        self.selected = None
        self.refresh_sessions()
        self.call_later(self._select, sid)

    def action_add_session(self) -> None:
        form = FormScreen(
            "Add a session",
            [("ref", "claude.ai/code link or session ID", "")],
            "Add",
            "Use this for a session that doesn't appear in the list.",
        )
        self.push_screen(form, self._add_session)

    def _add_session(self, values: dict | None) -> None:
        if not values:
            return
        sid = parse_session_ref(values.get("ref", ""))
        if not sid:
            self.notify("That doesn't look like a session link or ID.", severity="error")
            return
        self.store.add_known(sid)
        self.refresh_sessions()
        self.call_later(self._select, sid)

    def action_forget(self) -> None:
        session = self._current()
        if session is None:
            return
        if session.id not in self.store.known:
            self.notify("Only sessions saved on this machine can be forgotten here.", severity="warning")
            return
        self.store.remove_known(session.id)
        self.notify("Removed from this machine. The session itself is unchanged.")
        self.refresh_sessions()

    def action_open_web(self) -> None:
        session = self._current()
        if session is not None:
            asyncio.get_running_loop().run_in_executor(None, webbrowser.open, web_url(session.id))
            self.notify("Opened on claude.ai.")

    def action_copy_link(self) -> None:
        session = self._current()
        if session is not None:
            self.copy_to_clipboard(web_url(session.id))
            self.notify("Link copied.")

    def action_set_checkout(self) -> None:
        session = self._current()
        if session is None:
            return
        label = f"Local checkout of {session.repo}" if session.repo else "Local checkout for this session"
        form = FormScreen("Checkout for teleport", [("path", label, self._checkout(session) or os.getcwd())], "Save")
        self.push_screen(form, lambda values: self._save_checkout(session, values))

    def _save_checkout(self, session: Session, values: dict | None) -> None:
        if not values or not values.get("path"):
            return
        path = os.path.expanduser(values["path"])
        if not os.path.isdir(path):
            self.notify(f"{path} isn't a folder.", severity="error")
            return
        self.store.set_checkout(self._checkout_key(session), path)
        self._render_header()
        self.notify("Checkout saved.")

    def action_teleport(self) -> None:
        session = self._current()
        if session is None:
            return
        checkout = self._checkout(session)
        if not checkout:
            self.notify("Set the local checkout first (press c).", severity="warning")
            return
        try:
            with self.suspend():
                code = claude_cli.teleport(self.claude_bin, session.id, checkout)
        except SuspendNotSupported:
            self.notify(f"Run: claude --teleport {session.id}", severity="error")
            return
        self.notify("Back from teleport." if code == 0 else f"claude --teleport exited with {code}.")

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_sessions(self) -> None:
        self.query_one("#sessions", OptionList).focus()

    def action_toggle_archived(self) -> None:
        self.show_archived = not self.show_archived
        self._render_list()
        self.notify("Showing archived sessions." if self.show_archived else "Hiding archived sessions.")

    def action_refresh(self) -> None:
        self.refresh_sessions()
        self.refresh_events()
