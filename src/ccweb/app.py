"""Terminal client laid out like claude.ai/code: sessions on the left, a home page or conversation on the right."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.cells import cell_len
from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, ContentSwitcher, Footer, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from . import claude_cli, gitinfo
from .api import (
    ApiError,
    AuthError,
    CloudApi,
    Environment,
    Message,
    Session,
    build_session_body,
    parse_repo,
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
BLUE = "#6f9be8"

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

NEEDS_INPUT = {"requires_action", "waiting", "blocked", "needs_input", "need_input", "input_required", "awaiting_input"}
WORKING = {"running", "working", "active", "busy", "in_progress", "processing", "pending"}
FAILED = {"failed", "error", "errored"}
REVIEW = {"review_ready", "ready_for_review"}

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PAGE = 20  # sidebar rows revealed per "Show more", as on the web
HOME_ROWS = 5
ATTENTION_WINDOW = timedelta(days=2)  # older unread questions drop to plain recency order on the home page
OPTIMISTIC_SECONDS = 8  # after a reply, show "working" until the server catches up

PERMISSION_MODES = [
    ("Auto", "auto"),
    ("Ask permissions", "default"),
    ("Accept edits", "acceptEdits"),
    ("Plan mode", "plan"),
]
EFFORTS = [("Default", ""), ("Low", "low"), ("Medium", "medium"), ("High", "high"), ("XHigh", "xhigh"), ("Max", "max")]
MODEL_RE = re.compile(r"claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(\[1m\])?")


def model_label(model: str) -> str:
    match = MODEL_RE.fullmatch(model)
    if not match:
        return model or "Default model"
    family, major, minor, long_context = match.groups()
    version = f"{major}.{minor}" if minor else major
    return f"{family.capitalize()} {version}" + (" (1M)" if long_context else "")


def label_for(options: list[tuple[str, str]], value: str, fallback: str = "") -> str:
    return next((label for label, v in options if v == value), fallback or value)


# ---------------------------------------------------------------------- status


def is_working(session: Session, optimistic: dict[str, float] | None = None) -> bool:
    if optimistic and optimistic.get(session.id, 0) > time.monotonic():
        return True
    return session.status.lower() in WORKING or session.bucket.lower() == "working"


def status_kind(session: Session, optimistic: dict[str, float] | None = None) -> str:
    """archived | working | needs_input | failed | review | idle"""
    if session.archived:
        return "archived"
    if is_working(session, optimistic):
        return "working"
    signals = {session.status.lower(), session.bucket.lower(), session.category.lower()}
    if signals & NEEDS_INPUT:
        return "needs_input"
    if signals & FAILED:
        return "failed"
    if signals & REVIEW:
        return "review"
    return "idle"


def describe_status(session: Session, optimistic: dict[str, float] | None = None, frame: int = 0) -> tuple[str, str, str]:
    """(glyph, colour, label). Like the web: a filled dot means unread, a hollow one means seen."""
    kind = status_kind(session, optimistic)
    dot = "●" if session.unread else "○"
    if kind == "archived":
        return "◌", MUTED, "Archived"
    if kind == "working":
        return SPINNER[frame % len(SPINNER)], SAGE, "Working"
    if kind == "needs_input":
        return dot, AMBER, "Needs input"
    if kind == "failed":
        return "✕", BRICK, "Failed"
    if kind == "review":
        return dot, BLUE, "Ready for review"
    if not session.from_server:
        return "○", MUTED, "Saved here, not loaded from claude.ai"
    if session.unread:
        return "●", BLUE, "Unread"
    label = "Completed" if session.bucket.lower() == "completed" else session.status.replace("_", " ").capitalize()
    return "○", MUTED, label or "Idle"


def relative_time(moment: datetime | None) -> str:
    if moment is None:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600), (86400 * 30, "d", 86400)):
        if seconds < limit:
            return f"{seconds // size}{unit}"
    local = moment.astimezone()
    return local.strftime("%b %-d") if local.year == datetime.now().year else local.strftime("%b %Y")


def ago(moment: datetime | None) -> str:
    text = relative_time(moment)
    return f"{text} ago" if text[:-1].isdigit() else text


FLOOR = datetime.min.replace(tzinfo=timezone.utc)


def by_recency(sessions) -> list[Session]:
    """Newest activity first, which is the order the web sidebar uses (the API itself returns creation order)."""
    return sorted(sessions, key=lambda s: s.last_activity or FLOOR, reverse=True)


def home_sessions(
    sessions: list[Session], optimistic: dict[str, float] | None = None, limit: int = HOME_ROWS
) -> list[Session]:
    """What the home page leads with: recent unread questions for you, then work ready for review, then the rest."""
    live = by_recency(s for s in sessions if not s.archived)
    recent = [s for s in live if s.last_activity and datetime.now(timezone.utc) - s.last_activity < ATTENTION_WINDOW]
    waiting = [s for s in recent if status_kind(s, optimistic) == "needs_input" and s.unread]
    review = [s for s in recent if status_kind(s, optimistic) == "review"]
    rest = [s for s in live if s not in waiting and s not in review]
    return (waiting + review + rest)[:limit]


def short_repo(repo: str) -> str:
    return repo.split("/")[-1] if repo else ""


def welcome_name() -> str:
    """First name from the local Claude Code sign-in, for the greeting. No network, nothing stored."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    for candidate in ([Path(base) / ".claude.json"] if base else []) + [Path.home() / ".claude.json"]:
        try:
            account = json.loads(candidate.read_text(encoding="utf-8")).get("oauthAccount") or {}
        except (OSError, ValueError):
            continue
        name = account.get("displayName") or account.get("fullName") or ""
        if isinstance(name, str) and name.strip():
            return name.strip().split()[0]
    return ""


# ---------------------------------------------------------------------- screens


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


class ChoiceScreen(ModalScreen[str | None]):
    """Pick one value from a list, optionally typing one that isn't listed."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        heading: str,
        options: list[tuple[str, str, str]],
        current: str = "",
        custom: str = "",
        note: str = "",
    ):
        """options are (value, label, detail); custom is the placeholder for a free-text field, if wanted."""
        super().__init__()
        self.heading, self.options, self.current, self.custom, self.note = heading, options, current, custom, note

    @staticmethod
    def _key(value: str) -> str:
        return value or "__empty__"

    def _rows(self, needle: str = "") -> list[Option]:
        rows = []
        for value, label, detail in self.options:
            if needle and needle.lower() not in f"{label} {value}".lower():
                continue
            text = Text(no_wrap=True, overflow="ellipsis")
            text.append("● " if value == self.current else "  ", style=CLAY)
            text.append(label, style=SAND)
            if detail:
                text.append(f"  {detail}", style=MUTED)
            rows.append(Option(text, id=self._key(value)))
        return rows

    def compose(self) -> ComposeResult:
        with Vertical(id="choice"):
            yield Label(self.heading, id="form-heading")
            if self.note:
                yield Static(self.note, id="form-note")
            yield OptionList(*self._rows(), id="choices")
            if self.custom:
                yield Input(placeholder=self.custom, id="custom")

    def on_mount(self) -> None:
        choices = self.query_one("#choices", OptionList)
        keys = [self._key(v) for v, _, _ in self.options]
        if self._key(self.current) in keys:
            choices.highlighted = keys.index(self._key(self.current))
        elif keys:
            choices.highlighted = 0
        choices.focus()

    @on(OptionList.OptionSelected, "#choices")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        key = event.option.id or ""
        self.dismiss("" if key == "__empty__" else key)

    @on(Input.Changed, "#custom")
    def _filter(self, event: Input.Changed) -> None:
        """Typing narrows the list, so a long list (hundreds of branches) stays usable."""
        choices = self.query_one("#choices", OptionList)
        choices.clear_options()
        choices.add_options(self._rows(event.value.strip()))

    @on(Input.Submitted, "#custom")
    def _typed(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_KEYS = [
    ("n", "Home: start a new cloud session"),
    ("i", "Add a session by link or ID"),
    ("o / y", "Open on claude.ai / copy link"),
    ("t / c", "Teleport / set local checkout"),
    ("/", "Search sessions"),
    ("v", "Show or hide archived"),
    ("r", "Refresh now"),
    ("x", "Forget a locally saved session"),
    ("esc", "Back to the session list"),
    ("q", "Quit (ctrl+q works anywhere)"),
    ("f1", "This help, even while typing"),
]


class HelpScreen(ModalScreen[None]):
    """Key reference."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
        Binding("f1", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        text = Text()
        text.append("Keys\n\n", style=f"bold {SAND}")
        for key, what in HELP_KEYS:
            text.append(f"{key:<8}", style=f"bold {CLAY}")
            text.append(f"{what}\n", style=SAND)
        text.append("\nOn the home page, tab moves between the settings chips and enter starts the session.", style=MUTED)
        text.append("\nWhile typing a reply, enter sends and esc leaves the box.", style=MUTED)
        with Vertical(id="help"):
            yield Static(text)

    def on_click(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class Chip(Button):
    """A compact, focusable setting on the new-session form."""

    def __init__(self, label: str, what: str, index: int = -1, tip: str = "", classes: str = ""):
        super().__init__(label, compact=True, classes=f"chip {classes}".strip())
        self.what, self.index = what, index
        if tip:
            self.tooltip = tip


# ---------------------------------------------------------------------- app


class CloudSessionsApp(App):
    TITLE = "Cloud sessions"
    CSS = f"""
    Screen {{ layout: vertical; }}
    #body {{ height: 1fr; }}
    #sidebar {{ width: 42; background: $surface; padding: 0 1; border-right: tall $panel; }}
    #brand {{ padding: 1 1 0 1; color: {CLAY}; text-style: bold; }}
    #account {{ padding: 0 0 0 1; color: {MUTED}; height: auto; }}
    #new {{ width: 100%; margin: 1 0 0 0; }}
    #search {{ margin: 1 0 0 0; border: tall $panel; background: $background; }}
    #search:focus {{ border: tall {CLAY}; }}
    #list-heading {{ margin: 1 1 0 1; color: {MUTED}; }}
    #sessions {{ height: 1fr; background: $surface; border: none; padding: 0; }}
    #sessions:focus {{ border: none; }}
    #sessions > .option-list--option-highlighted {{ background: $panel; text-style: none; }}
    #sessions:focus > .option-list--option-highlighted {{ background: {CLAY} 28%; }}
    #sessions > .option-list--option-hover {{ background: $panel 60%; }}
    #main {{ padding: 0 2; }}
    #banner {{ display: none; height: auto; margin-top: 1; padding: 0 1; background: {BRICK} 18%; color: {SAND}; }}
    #banner.show {{ display: block; }}
    #views {{ height: 1fr; }}

    #welcome {{ padding: 2 0 1 0; text-style: bold; }}
    #home-label {{ color: {MUTED}; margin-bottom: 1; }}
    #attention {{ height: auto; max-height: 9; background: $background; border: none; padding: 0; }}
    #attention:focus {{ border: none; }}
    #attention > .option-list--option {{ background: $surface; }}
    #attention > .option-list--option-highlighted {{ background: $panel; text-style: none; }}
    #attention:focus > .option-list--option-highlighted {{ background: {CLAY} 28%; }}
    #home-spacer {{ height: 1fr; }}
    #newsession {{ height: auto; }}
    #chips {{ height: 1; margin-bottom: 1; }}
    #settings {{ height: 1; margin-top: 1; }}
    #settings-spacer {{ width: 1fr; }}
    #prompt {{ border: tall $panel; background: $surface; }}
    #prompt:focus {{ border: tall {CLAY}; }}
    #hint {{ color: {MUTED}; margin: 1 0; }}
    Chip {{ width: auto; min-width: 0; margin-right: 1; background: $panel; color: {SAND}; padding: 0 1; }}
    Chip:focus {{ background: {CLAY} 45%; text-style: none; }}
    Chip.remove {{ margin-left: 0; color: {MUTED}; padding: 0 1; background: $surface; }}
    Chip.plain {{ background: $background; color: {MUTED}; }}

    #session-header {{ height: auto; padding: 1 0; border-bottom: hkey $panel; }}
    #transcript {{ height: 1fr; background: $background; border: none; padding: 1 0 0 0; scrollbar-size-vertical: 1; }}
    #activity {{ height: 1; color: {SAGE}; }}
    #composer {{ margin: 0 0 1 0; border: tall $panel; background: $surface; }}
    #composer:focus {{ border: tall {CLAY}; }}

    #form, #choice {{ width: 76; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: tall {CLAY}; }}
    #help {{ width: 64; height: auto; padding: 1 2; background: $surface; border: tall {CLAY}; }}
    #choices {{ height: auto; max-height: 16; background: $surface; border: none; padding: 0; }}
    #custom {{ margin-top: 1; }}
    FormScreen, ChoiceScreen, HelpScreen {{ align: center middle; }}
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
        Binding("question_mark", "help", "Help"),
        Binding("f1", "help", "Help", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: dict, store: Store | None = None):
        super().__init__()
        self.cfg = cfg
        self.store = store or Store()
        self.api = CloudApi(cfg)
        self.claude_bin = cfg["claude"]["bin"]
        self.sessions: dict[str, Session] = {}
        self.older: dict[str, Session] = {}  # loaded through "Load more sessions"
        self.cursor: str | None = None
        self.has_more = False
        self.visible_limit = PAGE
        self.selected: str | None = None
        self.rendered_keys: set[str] = set()
        self.events_cursor: str | None = None
        self.events_error_for: str | None = None
        self.api_ok = False
        self.loaded_once = False
        self.show_archived = False
        self.filter_text = ""
        self.optimistic: dict[str, float] = {}
        self.frame = 0
        self.envs: list[Environment] = []
        self.draft = self._initial_draft()
        self._seeded = False
        self.creating = False
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
                yield Static("Ungrouped", id="list-heading")
                yield OptionList(id="sessions")
            with Vertical(id="main"):
                yield Static("", id="banner")
                with ContentSwitcher(id="views", initial="home"):
                    with Vertical(id="home"):
                        yield Static("", id="welcome")
                        yield Static("Sessions", id="home-label")
                        yield OptionList(id="attention")
                        yield Static("", id="home-spacer")
                        with Vertical(id="newsession"):
                            yield Horizontal(id="chips")
                            yield Input(placeholder="Describe a task or ask a question", id="prompt")
                            with Horizontal(id="settings"):
                                yield Chip("", "mode", classes="plain", tip="Permission mode")
                                yield Static("", id="settings-spacer")
                                yield Chip("", "model", classes="plain", tip="Model")
                                yield Chip("", "effort", classes="plain", tip="Effort")
                            yield Static("enter starts the session · tab moves between settings · esc, then ? lists keys", id="hint")
                    with Vertical(id="convo"):
                        yield Static("", id="session-header")
                        yield RichLog(id="transcript", wrap=True, min_width=20)
                        yield Static("", id="activity")
                        yield Input(placeholder="Reply to this session", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "ccweb"
        name = welcome_name()
        self.query_one("#welcome", Static).update(
            Text.assemble(("✱ ", CLAY), (f"Welcome back, {name}" if name else "Welcome back", SAND))
        )
        self._render_header()
        self._render_settings()
        self.call_later(self._rebuild_chips)
        if not shutil.which(self.claude_bin):
            self._banner(
                f"`{self.claude_bin}` isn't on your PATH. Install Claude Code or set CCWEB_CLAUDE_BIN; "
                "replying and teleporting need it."
            )
        self.refresh_sessions()
        self.load_environments()
        self.set_interval(float(self.cfg["poll"]["sessions_seconds"]), self.refresh_sessions)
        self.set_interval(float(self.cfg["poll"]["events_seconds"]), self.refresh_events)
        self.set_interval(0.12, self._tick)
        self.query_one("#prompt", Input).focus()

    async def on_unmount(self) -> None:
        await self.api.aclose()

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_attention)

    # ------------------------------------------------------------------ data

    @work(exclusive=True, group="sessions")
    async def refresh_sessions(self) -> None:
        try:
            page = await self.api.list_sessions_page()
            remote = page.sessions
            self.api_ok = True
            self._banner(None)
            if not self.older:
                self.cursor, self.has_more = page.last_id, page.has_more
        except (ApiError, AuthError) as exc:
            remote = []
            self.api_ok = False
            self._banner(
                f"Couldn't read your sessions from claude.ai/code ({exc}). "
                "Replying, teleport and open-on-web still work through the claude CLI. "
                "Run `ccweb probe` to check the endpoints."
            )
        self._merge_sessions(remote)

    def _merge_sessions(self, remote: list[Session]) -> None:
        merged = {**self.older, **{s.id: s for s in remote}}
        for sid, info in self.store.known.items():
            if sid not in merged:
                added = info.get("added_at")
                merged[sid] = Session(
                    id=sid,
                    title=info.get("title", ""),
                    from_server=False,
                    created_at=datetime.fromtimestamp(added, tz=timezone.utc) if self.api_ok and added else None,
                )
        # A session we just created may not be listed yet; keep its placeholder until the server returns it.
        for sid, session in self.sessions.items():
            if sid not in merged and self.api_ok and self.optimistic.get(sid, 0) > time.monotonic():
                merged[sid] = session
        self.sessions = merged
        self.loaded_once = True
        live = [s for s in remote if not s.archived]
        waiting = sum(1 for s in live if status_kind(s, self.optimistic) == "needs_input" and s.unread)
        working = sum(1 for s in live if status_kind(s, self.optimistic) == "working")
        account = Text(f"{len(live)} sessions" if self.api_ok else "claude.ai not reachable")
        if waiting:
            account.append(" · ", style=MUTED)
            account.append(f"{waiting} need you", style=AMBER)
        if working:
            account.append(" · ", style=MUTED)
            account.append(f"{working} working", style=SAGE)
        self.query_one("#account", Static).update(account)
        self._seed_draft()
        self._render_list()
        self._render_attention()
        self._render_header()
        self._render_activity()

    @work(exclusive=True, group="more")
    async def load_more(self) -> None:
        try:
            page = await self.api.list_sessions_page(after_id=self.cursor)
        except (ApiError, AuthError) as exc:
            self.notify(f"Couldn't load more sessions: {exc}", severity="error")
            return
        self.older.update({s.id: s for s in page.sessions})
        self.cursor, self.has_more = page.last_id, page.has_more
        self.visible_limit += PAGE
        self._merge_sessions([s for s in self.sessions.values() if s.from_server and s.id not in self.older])

    @work(exclusive=True, group="events")
    async def refresh_events(self) -> None:
        sid = self.selected
        if not sid or not self.api_ok:
            return
        try:
            fresh: list[Message] = []
            cursor = self.events_cursor
            for _ in range(20):  # a long session is several pages the first time, usually one afterwards
                page = await self.api.list_events_page(sid, cursor)
                fresh += page.messages
                cursor = page.last_id or cursor
                if not page.has_more or not page.messages:
                    break
        except (ApiError, AuthError) as exc:
            if self.selected == sid and self.events_error_for != sid:
                self.events_error_for = sid
                self._note(f"Couldn't load this conversation: {exc}")
            return
        if self.selected != sid:
            return
        self.events_cursor = cursor
        shown = [m for m in fresh if m.kind != "event" and m.key not in self.rendered_keys]
        if not shown and not self.rendered_keys and not fresh:
            self._note("No messages yet.")
            self.rendered_keys.add("__empty__")
        for message in shown:
            self.rendered_keys.add(message.key)
            self._write_message(message)
        try:  # the list only refreshes every so often; this keeps the open session's status current
            session = await self.api.get_session(sid)
        except (ApiError, AuthError):
            session = None
        if session is not None and self.selected == sid:
            self.sessions[sid] = session
            self._render_header()
            self._render_activity()
            self._update_row(sid)

    @work(exclusive=True, group="envs")
    async def load_environments(self) -> None:
        try:
            envs = await self.api.list_environments()
        except (ApiError, AuthError) as exc:
            self.notify(f"Couldn't load your cloud environments: {exc}", severity="warning", timeout=8)
            return
        self.envs = [e for e in envs if e.kind != "bridge"]  # Remote Control bridges can't run new cloud sessions
        self._seed_draft()
        self.call_later(self._rebuild_chips)

    def _seed_draft(self) -> None:
        """Once environments and sessions are both known, start the form from what you used last, like the web."""
        if self._seeded or not self.envs or not self.loaded_once:
            return
        self._seeded = True
        valid = {e.id for e in self.envs}
        recent = next(
            (s for s in by_recency(self.sessions.values()) if s.from_server and not s.archived and s.sources), None
        )
        if self.draft["env"] not in valid:
            wanted = self.cfg["new_session"].get("environment", "")
            by_pref = next((e for e in self.envs if wanted and wanted in (e.id, e.name)), None)
            by_name = next((e for e in self.envs if e.name == "Default"), None)
            by_history = recent.environment_id if recent and recent.environment_id in valid else ""
            self.draft["env"] = by_pref.id if by_pref else by_history or (by_name or self.envs[0]).id
        if recent and not self.draft["repos"] and "repos" not in self.store.prefs:
            self.draft["repos"] = [{"repo": repo, "branch": rev} for repo, rev in recent.sources if repo]
        for repo in {r["repo"] for r in self.draft["repos"] if not r["branch"]}:
            self.resolve_default_branch(repo)
        self.call_later(self._rebuild_chips)

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
        return by_recency(out)

    def _row(self, session: Session) -> Text:
        glyph, colour, _ = describe_status(session, self.optimistic, self.frame)
        row = Text(no_wrap=True)
        row.append(f"{glyph} ", style=colour)
        row.append(session.title or session.id, style=SAND)
        width = self.query_one("#sessions", OptionList).size.width
        if width > 6:  # the list wraps long rows itself, so cut them with an ellipsis like the web does
            row.truncate(width - 3, overflow="ellipsis")
        return row

    def _render_list(self) -> None:
        option_list = self.query_one("#sessions", OptionList)
        visible = self._visible_sessions()
        filtering = bool(self.filter_text)
        shown = visible if filtering else visible[: self.visible_limit]
        options: list[Option] = [Option(self._row(s), id=s.id) for s in shown]
        hidden = len(visible) - len(shown)
        if hidden > 0:
            options.append(Option(Text(f"  Show {min(PAGE, hidden)} more", style=MUTED), id="__more__"))
        if self.has_more and not filtering:
            options.append(Option(Text("Load more sessions", style=MUTED), id="__load__"))
        target = self.selected if self.selected in {s.id for s in shown} else None
        scroll = option_list.scroll_y
        self._suppress_highlight = True
        try:
            option_list.clear_options()
            option_list.add_options(options)
            if target:
                option_list.highlighted = option_list.get_option_index(target)
            option_list.scroll_to(y=scroll, animate=False)
        finally:
            self._suppress_highlight = False
        if not options and self.loaded_once:
            empty = "No matches." if filtering else "No sessions yet. Press n to start one."
            option_list.add_option(Option(Text(empty, style=MUTED), disabled=True))

    def _update_row(self, sid: str) -> None:
        session = self.sessions.get(sid)
        if session is None:
            return
        try:
            self.query_one("#sessions", OptionList).replace_option_prompt(sid, self._row(session))
        except Exception:  # row not on screen (filtered out, or not shown yet)
            pass

    def _attention_row(self, session: Session, width: int) -> Text:
        glyph, colour, label = describe_status(session, self.optimistic, self.frame)
        detail = session.needs_action or session.summary
        right = f"{short_repo(session.repo)}  {ago(session.last_activity)}  ›".strip()
        left = Text(no_wrap=True)
        left.append(f" {glyph} {label}  ", style=colour)
        left.append(session.title or session.id, style=f"bold {SAND}")
        room = width - cell_len(left.plain) - cell_len(right) - 4
        if detail and room > 8:
            detail = " ".join(detail.split())
            if cell_len(detail) > room:
                detail = detail[: max(0, room - 1)].rstrip() + "…"
            left.append(f"  {detail}", style=MUTED)
        left.append(" " * max(2, width - cell_len(left.plain) - cell_len(right) - 1))
        left.append(right, style=MUTED)
        return left

    def _render_attention(self) -> None:
        option_list = self.query_one("#attention", OptionList)
        rows = home_sessions(list(self.sessions.values()), self.optimistic)
        width = max(50, option_list.size.width or self.size.width - 46)
        option_list.clear_options()
        if not rows:
            if self.loaded_once:
                option_list.add_option(
                    Option(Text("  Nothing yet. Describe a task below to start one.", style=MUTED), disabled=True)
                )
            return
        option_list.add_options([Option(self._attention_row(s, width), id=s.id) for s in rows])

    def _render_header(self) -> None:
        header = self.query_one("#session-header", Static)
        session = self.sessions.get(self.selected or "")
        if session is None:
            self.sub_title = ""
            header.update("")
            return
        self.sub_title = session.title or session.id
        glyph, colour, label = describe_status(session, self.optimistic, self.frame)
        text = Text()
        text.append(session.title or "Untitled session", style=f"bold {SAND}")
        text.append("\n")
        text.append(f"{glyph} {label}", style=colour)
        facts = [
            model_label(session.model) if session.model else "",
            label_for(EFFORTS, session.effort, session.effort).lower() if session.effort else "",
            label_for(PERMISSION_MODES, session.permission_mode).lower() if session.permission_mode else "",
        ]
        if any(facts):
            text.append("    " + " · ".join(f for f in facts if f), style=MUTED)
        text.append("\n")
        sources = session.sources or ([(session.repo, "")] if session.repo else [])
        for index, (repo, revision) in enumerate(sources):
            text.append("" if index == 0 else "   ")
            text.append(repo or "repository unknown", style=SAND)
            if revision:
                text.append(f" ⎇ {revision}", style=MUTED)
        if not sources:
            text.append("no repository", style=MUTED)
        env = next((e.name for e in self.envs if e.id == session.environment_id), "")
        text.append(f"\n{session.id}", style=MUTED)
        if env:
            text.append(f"  ·  ☁ {env}", style=MUTED)
        checkout = self._checkout(session)
        text.append(
            f"    teleports into {checkout}" if checkout else "    press c to set the local checkout for teleport",
            style=MUTED,
        )
        header.update(text)

    def _render_activity(self) -> None:
        activity = self.query_one("#activity", Static)
        session = self.sessions.get(self.selected or "")
        if session is not None and status_kind(session, self.optimistic) == "working":
            activity.update(Text(f"{SPINNER[self.frame % len(SPINNER)]} Claude is working…", style=SAGE))
        else:
            activity.update("")

    def _tick(self) -> None:
        """Animate the spinners: working sessions' sidebar rows and the open conversation's status line."""
        self.frame += 1
        working = [s for s in self.sessions.values() if status_kind(s, self.optimistic) == "working"]
        if not working:
            return
        self._render_activity()
        for session in working:
            self._update_row(session.id)
        if self.selected in {s.id for s in working}:
            self._render_header()

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
                bar = Text()
                for i, line in enumerate(message.text.splitlines() or [""]):
                    bar.append("\n" if i else "")
                    bar.append("▎ ", style=CLAY)
                    bar.append(line, style=SAND)
                log.write(bar)
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

    def _show(self, view: str) -> None:
        self.query_one("#views", ContentSwitcher).current = view

    def _select(self, sid: str | None) -> None:
        if sid is None:
            self.selected = None
            self._show("home")
            self._render_header()
            return
        self._show("convo")
        if sid == self.selected:
            return
        self.selected = sid
        self.rendered_keys = set()
        self.events_cursor = None
        self.events_error_for = None
        self._after_tool = False
        self.query_one("#transcript", RichLog).clear()
        self._render_header()
        self._render_activity()
        if sid in self.sessions and not self.api_ok:
            self._note(
                "The conversation can't be loaded because claude.ai isn't reachable. "
                "You can still reply below, teleport (t) or open it on the web (o)."
            )
        self._sync_highlight()
        self.refresh_events()

    def _sync_highlight(self) -> None:
        option_list = self.query_one("#sessions", OptionList)
        if not self.selected:
            return
        try:
            index = option_list.get_option_index(self.selected)
        except Exception:  # not in the visible rows (filtered, or past "Show more")
            return
        self._suppress_highlight = True
        try:
            option_list.highlighted = index
        finally:
            self._suppress_highlight = False

    @on(OptionList.OptionHighlighted, "#sessions")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if not self._suppress_highlight and event.option.id and not event.option.id.startswith("__"):
            self._select(event.option.id)

    @on(OptionList.OptionSelected, "#sessions")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        sid = event.option.id
        if sid == "__more__":
            self.visible_limit += PAGE
            self._render_list()
        elif sid == "__load__":
            self.load_more()
        elif sid:
            self._select(sid)
            self.query_one("#composer", Input).focus()

    @on(OptionList.OptionSelected, "#attention")
    def _open_from_home(self, event: OptionList.OptionSelected) -> None:
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

    # ------------------------------------------------------------------ new session form

    def _initial_draft(self) -> dict:
        prefs = self.store.prefs
        defaults = self.cfg["new_session"]
        repos = [
            {"repo": r["repo"], "branch": r.get("branch", "")}
            for r in prefs.get("repos", [])
            if isinstance(r, dict) and r.get("repo")
        ]
        if not repos and "repos" not in prefs:
            local = gitinfo.local_repo(os.getcwd())
            repos = [{"repo": local, "branch": ""}] if local else []
        return {
            "env": prefs.get("env") or "",
            "repos": repos,
            "mode": prefs.get("mode") or defaults["permission_mode"],
            "model": prefs.get("model") or defaults["model"],
            "effort": prefs["effort"] if "effort" in prefs else defaults["effort"],
        }

    def _model_options(self) -> list[tuple[str, str]]:
        known = [(label, model_id) for label, model_id in self.cfg["new_session"]["models"]]
        seen = {model_id for _, model_id in known}
        used = [s.model for s in by_recency(self.sessions.values()) if s.model]
        extra = [(model_label(m), m) for m in dict.fromkeys(used) if m not in seen]
        current = self.draft["model"]
        if current and current not in seen and current not in {m for _, m in extra}:
            extra.insert(0, (model_label(current), current))
        return known + extra

    def _recent_repos(self) -> list[tuple[str, str]]:
        """(owner/repo, the branch it was last started from), most recently used first."""
        out: dict[str, str] = {}
        for session in by_recency(self.sessions.values()):
            for repo, revision in session.sources:
                if repo and repo not in out:
                    out[repo] = revision
        return list(out.items())

    def _render_settings(self) -> None:
        chips = {c.what: c for c in self.query("#settings Chip").results(Chip)}
        chips["mode"].label = label_for(PERMISSION_MODES, self.draft["mode"])
        chips["model"].label = model_label(self.draft["model"])
        chips["effort"].label = label_for(EFFORTS, self.draft["effort"], "Default")

    async def _rebuild_chips(self) -> None:
        row = self.query_one("#chips", Horizontal)
        await row.remove_children()
        env = next((e for e in self.envs if e.id == self.draft["env"]), None)
        widgets: list[Chip] = [Chip(f"☁ {env.name if env else 'Choose environment'}", "env", tip="Environment")]
        repos = self.draft["repos"]
        short = [short_repo(r["repo"]) for r in repos]
        for i, item in enumerate(repos):
            name = item["repo"] if short.count(short[i]) > 1 else short[i]
            widgets.append(Chip(f"</> {name}", "repo", i, tip=item["repo"]))
            widgets.append(Chip(f"⎇ {item['branch'] or 'default'}", "branch", i, tip="Branch to start from"))
            widgets.append(Chip("×", "remove", i, tip="Remove this repository", classes="remove"))
        widgets.append(Chip("+", "add", tip="Add a repository"))
        await row.mount(*widgets)
        self._render_settings()

    @on(Button.Pressed, ".chip")
    def _chip_pressed(self, event: Button.Pressed) -> None:
        chip = event.button
        if not isinstance(chip, Chip):
            return
        event.stop()
        what, i = chip.what, chip.index
        if what == "env":
            options = [(e.id, e.name, "" if e.kind == "anthropic_cloud" else e.kind) for e in self.envs]
            note = "" if options else "No environments loaded yet."
            self.push_screen(ChoiceScreen("Environment", options, self.draft["env"], note=note), self._set_draft("env"))
        elif what in {"repo", "add"}:
            taken = {r["repo"] for j, r in enumerate(self.draft["repos"]) if j != i}
            options = [(repo, repo, f"⎇ {rev}" if rev else "") for repo, rev in self._recent_repos() if repo not in taken]
            current = self.draft["repos"][i]["repo"] if what == "repo" else ""
            screen = ChoiceScreen(
                "Repository" if what == "repo" else "Add a repository",
                options,
                current,
                custom="Or type owner/repo or a GitHub URL",
                note="The cloud environment needs GitHub access to the repository.",
            )
            self.push_screen(screen, lambda value: self._set_repo(i if what == "repo" else None, value))
        elif what == "branch":
            self._pick_branch(i)
        elif what == "remove":
            del self.draft["repos"][i]
            self._draft_changed()
        elif what == "mode":
            options = [(v, label, "" if v == "default" else v) for label, v in PERMISSION_MODES]
            self.push_screen(ChoiceScreen("Permission mode", options, self.draft["mode"]), self._set_draft("mode"))
        elif what == "model":
            options = [(v, label, v) for label, v in self._model_options()]
            screen = ChoiceScreen("Model", options, self.draft["model"], custom="Or type a model ID")
            self.push_screen(screen, self._set_draft("model"))
        elif what == "effort":
            options = [(v, label, "" if v else "the model's own default") for label, v in EFFORTS]
            self.push_screen(ChoiceScreen("Effort", options, self.draft["effort"]), self._set_draft("effort"))

    def _set_draft(self, key: str):
        def apply(value: str | None) -> None:
            if value is None:
                return
            self.draft[key] = value
            self._draft_changed()

        return apply

    def _draft_changed(self) -> None:
        self.store.set_prefs(
            env=self.draft["env"],
            repos=self.draft["repos"],
            mode=self.draft["mode"],
            model=self.draft["model"],
            effort=self.draft["effort"],
        )
        self.call_later(self._rebuild_chips)

    def _set_repo(self, index: int | None, value: str | None) -> None:
        if not value:
            return
        repo = parse_repo(value)
        if not repo:
            self.notify("That doesn't look like owner/repo or a GitHub URL.", severity="error")
            return
        repos = self.draft["repos"]
        if any(r["repo"] == repo for j, r in enumerate(repos) if j != index):
            self.notify(f"{repo} is already in the list.", severity="warning")
            return
        branch = next((rev for r, rev in self._recent_repos() if r == repo), "")
        if index is None:
            repos.append({"repo": repo, "branch": branch})
        elif repos[index]["repo"] != repo:
            repos[index] = {"repo": repo, "branch": branch}
        self._draft_changed()
        if not branch:
            self.resolve_default_branch(repo)

    @work(group="default-branch")
    async def resolve_default_branch(self, repo: str) -> None:
        branch = await asyncio.to_thread(gitinfo.default_branch, repo)
        if not branch:
            return
        for item in self.draft["repos"]:
            if item["repo"] == repo and not item["branch"]:
                item["branch"] = branch
        self.call_later(self._rebuild_chips)  # not saved: only your own choices are remembered

    @work(group="branches")
    async def _pick_branch(self, index: int) -> None:
        repos = self.draft["repos"]
        if index >= len(repos):
            return
        repo = repos[index]["repo"]
        self.notify(f"Loading branches of {repo}…", timeout=2)
        default, branches = await asyncio.gather(
            asyncio.to_thread(gitinfo.default_branch, repo), asyncio.to_thread(gitinfo.list_branches, repo)
        )
        branches = gitinfo.order_branches(branches, default)
        options = [("", "Default branch", "whatever the repository's default is")] + [
            (b, b, "default" if b == default else "") for b in branches
        ]
        note = "" if branches else "Branch list unavailable (needs the gh CLI signed in). Type a branch name."

        def apply(value: str | None) -> None:
            if value is None or index >= len(self.draft["repos"]):
                return
            self.draft["repos"][index]["branch"] = value
            self._draft_changed()

        screen = ChoiceScreen(f"Branch of {repo}", options, repos[index]["branch"], custom="Search, or type a branch name", note=note)
        self.push_screen(screen, apply)

    @on(Input.Submitted, "#prompt")
    def _start_session(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt or self.creating:
            return
        if not self.draft["env"]:
            self.notify("Choose an environment first (the ☁ chip).", severity="warning")
            return
        self.creating = True
        self.notify("Starting session…", timeout=2)
        self.create_session(prompt, json.loads(json.dumps(self.draft)))

    @work(group="create")
    async def create_session(self, prompt: str, draft: dict) -> None:
        try:
            body = build_session_body(
                prompt,
                draft["env"],
                [(r["repo"], r["branch"]) for r in draft["repos"]],
                model=draft["model"],
                effort=draft["effort"],
                permission_mode=draft["mode"],
            )
            sid = await self.api.create_session(body)
        except (ApiError, AuthError) as exc:
            self.notify(f"Couldn't start the session: {exc}", severity="error", timeout=15)
            return
        finally:
            self.creating = False
        self.query_one("#prompt", Input).value = ""
        self._remember_created(sid, prompt, draft)

    def _remember_created(self, sid: str, prompt: str, draft: dict) -> None:
        sid = parse_session_ref(sid) or sid
        self.store.add_known(sid, title=prompt[:80], source="created")
        now = datetime.now(timezone.utc)
        self.optimistic[sid] = time.monotonic() + 30  # the server takes a moment to list a brand-new session
        self.sessions[sid] = Session(
            id=sid,
            title=prompt.splitlines()[0][:80],
            status="pending",
            created_at=now,
            updated_at=now,
            model=draft["model"],
            effort=draft["effort"],
            permission_mode=draft["mode"],
            environment_id=draft["env"],
            sources=[(r["repo"], r["branch"]) for r in draft["repos"]],
            repo=draft["repos"][0]["repo"] if draft["repos"] else "",
        )
        self.notify("Session started.")
        self._render_list()
        self._select(sid)
        self.refresh_sessions()

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
            self.optimistic[sid] = time.monotonic() + OPTIMISTIC_SECONDS
            self._render_activity()
            await asyncio.sleep(1.5)
            self.refresh_events()
            return
        self.notify(f"Not sent: {result.error}", severity="error", timeout=12)
        composer = self.query_one("#composer", Input)
        if not composer.value:
            composer.value = text

    @on(Button.Pressed, "#new")
    def action_new_session(self) -> None:
        self._select(None)
        self.query_one("#prompt", Input).focus()

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
        except Exception:  # SuspendNotSupported on terminals that can't hand over control
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

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh(self) -> None:
        self.refresh_sessions()
        self.load_environments()
        self.refresh_events()
