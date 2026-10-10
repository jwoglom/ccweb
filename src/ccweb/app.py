"""Terminal client laid out like claude.ai/code: sessions on the left, a home page or conversation on the right."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collections.abc import Callable
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, ContentSwitcher, Footer, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

import subprocess

from . import claude_cli, gitinfo, prinfo
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
from .groups import Group, Grouping
from .links import LinkContext, is_openable, link_style, linkify_markdown, linkify_text
from .prinfo import BranchStatus, Check, Details, PullRequest, best_pr_state, check_counts, ref_key, relevant
from .panes import ChangesPane, FilesPane, PlanPane
from .repoinfo import ChangedFile, Comparison, Target, gh_commit_files, gh_compare, gh_file, gh_tree, targets_for
from .store import Store
from .tasks import TaskBoard
from .widgets import Chip

from .palette import AMBER, BLUE, BRICK, CLAY, MUTED, PURPLE, SAGE, SAND  # noqa: F401  (re-exported for tests)
PR_COLOURS = {"open": SAGE, "draft": MUTED, "merged": PURPLE, "closed": BRICK}
PR_ICON_COLOURS = {"open": "#b9b3a8", "draft": "#b9b3a8", "merged": PURPLE, "closed": BRICK}  # the web's sidebar tints
CHECK_GLYPHS = {"success": ("✓", SAGE), "failure": ("✗", BRICK), "pending": ("◔", AMBER), "skipped": ("–", MUTED)}
REVIEW_LABELS = {"APPROVED": ("approved", SAGE), "CHANGES_REQUESTED": ("changes requested", BRICK), "REVIEW_REQUIRED": ("review required", AMBER)}

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


def status_kind(session: Session, optimistic: dict[str, float] | None = None, pr_state: str = "") -> str:
    """archived | working | needs_input | failed | review | idle. A live PR counts as ready for review, as on the web."""
    if session.archived:
        return "archived"
    if is_working(session, optimistic):
        return "working"
    signals = {session.status.lower(), session.bucket.lower(), session.category.lower()}
    if signals & NEEDS_INPUT:
        return "needs_input"
    if signals & FAILED:
        return "failed"
    if signals & REVIEW or pr_state in {"open", "draft"}:
        return "review"
    return "idle"


def describe_status(
    session: Session, optimistic: dict[str, float] | None = None, frame: int = 0, pr_state: str = ""
) -> tuple[str, str, str]:
    """(glyph, colour, label). Like the web: a filled dot means unread, a hollow one means seen."""
    kind = status_kind(session, optimistic, pr_state)
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
    if pr_state == "merged":
        return "○", PURPLE, "PR merged"
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
    sessions: list[Session],
    optimistic: dict[str, float] | None = None,
    limit: int = HOME_ROWS,
    pr_states: dict[str, str] | None = None,
) -> list[Session]:
    """What the home page leads with: recent unread questions for you, then work ready for review, then the rest."""
    live = by_recency(s for s in sessions if not s.archived)
    recent = [s for s in live if s.last_activity and datetime.now(timezone.utc) - s.last_activity < ATTENTION_WINDOW]
    pr_of = (pr_states or {}).get
    waiting = [s for s in recent if status_kind(s, optimistic, pr_of(s.id, "")) == "needs_input" and s.unread]
    review = [s for s in recent if status_kind(s, optimistic, pr_of(s.id, "")) == "review"]
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


def ci_summary(checks: list[Check]) -> tuple[str, str] | None:
    """One phrase for where CI stands, and its colour; None when there's no CI to speak of."""
    if not checks:
        return None
    passing, failing, pending = check_counts(checks)
    if failing and pending:
        return "CI failing", BRICK  # something has failed and the rest is still going
    if failing:
        return "CI failed", BRICK
    if pending:
        return f"CI running {passing}/{len(checks)}", AMBER
    return "CI passed", SAGE


def row_summary(
    session: Session, kind: str, shown: list[BranchStatus], checks_for: Callable[[PullRequest], list[Check]]
) -> list[tuple[str, str]]:
    """The second line of a sidebar row as (text, colour) parts: PR and CI state first, then what's useful without one."""
    parts: list[tuple[str, str]] = []
    if kind == "working":
        parts.append(("Working", SAGE))
    elif kind == "failed":
        parts.append(("Failed", BRICK))
    prs = [s.pr for s in shown if s.pr]
    if prs:
        pr = prs[0]
        parts.append((f"{pr.state.capitalize()} #{pr.number}", PR_COLOURS.get(pr.state, MUTED)))
        ci = ci_summary(checks_for(pr)) if pr.is_live else None
        if ci:
            parts.append(ci)
        if len(prs) > 1:
            parts.append((f"+{len(prs) - 1}", MUTED))  # more PRs than the one shown
    elif shown:
        noun = "commit" if shown[0].commits == 1 else "commits"
        parts.append((f"{shown[0].commits} {noun} pushed, no PR", MUTED))
    elif kind == "needs_input" and session.needs_action:
        parts.append((" ".join(session.needs_action.split()), AMBER))
    elif session.summary:
        parts.append((" ".join(session.summary.split()), MUTED))
    elif session.repo:
        parts.append((short_repo(session.repo), MUTED))
    when = relative_time(session.last_activity)
    if when:
        parts.append((when, MUTED))
    return parts


def pr_line(status: BranchStatus, details: Details | None = None, checks: list[Check] | None = None) -> Text:
    """One line about a PR, or about a pushed branch that has none yet."""
    line = Text(no_wrap=True, overflow="ellipsis")
    pr = status.pr
    if pr is None:
        line.append("⎇ ", style=MUTED)
        line.append(f"{status.repo} {status.branch}", style=link_style(f"https://github.com/{status.repo}/tree/{status.branch}", SAND))
        noun = "commit" if status.commits == 1 else "commits"
        line.append(f"  {status.commits} {noun} pushed, no PR yet", style=MUTED)
        return line
    colour = PR_COLOURS.get(pr.state, MUTED)
    line.append("⎇ ", style=colour)
    line.append(pr.label, style=link_style(pr.url, f"bold {SAND}") if pr.url else f"bold {SAND}")
    line.append(f"  {pr.state.capitalize()}", style=colour)
    if pr.additions or pr.deletions:
        line.append("  ")
        line.append(f"+{pr.additions}", style=SAGE)
        line.append(f" −{pr.deletions}", style=BRICK)
    found = (details.checks if details and details.checks else checks or pr.checks) if pr.is_live else []
    if found:
        passing, failing, pending = check_counts(found)
        line.append("  CI", style=MUTED)
        for glyph_key, count in (("success", passing), ("failure", failing), ("pending", pending)):
            if count:
                glyph, glyph_colour = CHECK_GLYPHS[glyph_key]
                line.append(f" {glyph}{count}", style=glyph_colour)
    review = (details.review if details and details.review else pr.review) if pr.is_live else ""
    if review in REVIEW_LABELS:
        label, review_colour = REVIEW_LABELS[review]
        line.append(f"  {label}", style=review_colour)
    if pr.auto_merge:
        line.append("  auto-merge", style=MUTED)
    if pr.title:
        line.append(f"  {pr.title}", style=MUTED)
    return line


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


@dataclass
class Echo:
    """A message you just sent, shown at once. The server records it only when the session wakes up and
    takes it in, which for an idle one can be a while; the real copy replaces this when it arrives."""

    text: str
    widgets: list
    status: Static


def same_text(text: str) -> str:
    return " ".join(text.split())


@dataclass
class MenuItem:
    id: str
    label: str
    key: str = ""  # shortcut letter, shown on the right and active while the menu is open
    enabled: bool = True
    submenu: bool = False
    danger: bool = False
    note: str = ""


class MenuScreen(ModalScreen[str | None]):
    """The web's session menu: a list of actions, each with an optional single-key shortcut."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, heading: str, items: list[MenuItem | None]):
        """None in items is a divider."""
        super().__init__()
        self.heading, self.items = heading, items

    def compose(self) -> ComposeResult:
        width = 40
        rows: list = []
        for item in self.items:
            if item is None:
                rows.append(Option(Text("─" * width, style="#4a4742"), disabled=True))
                continue
            tail = "›" if item.submenu else item.key.upper()
            text = Text(no_wrap=True, overflow="ellipsis")
            style = BRICK if item.danger else (SAND if item.enabled else MUTED)
            text.append(item.label, style=style)
            if item.note:
                text.append(f"  {item.note}", style=MUTED)
            pad = max(1, width - cell_len(text.plain) - cell_len(tail))
            text.append(" " * pad + tail, style=MUTED)
            rows.append(Option(text, id=item.id, disabled=not item.enabled))
        with Vertical(id="menu"):
            yield Label(self.heading, id="menu-heading")
            yield OptionList(*rows, id="menu-items")

    def on_mount(self) -> None:
        options = self.query_one("#menu-items", OptionList)
        options.highlighted = next((i for i, o in enumerate(options.options) if not o.disabled and o.id), None)
        options.focus()

    @on(OptionList.OptionSelected, "#menu-items")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event: events.Key) -> None:
        if len(event.key) == 1:
            for item in self.items:
                if item and item.key and item.enabled and item.key.lower() == event.key.lower():
                    event.stop()
                    self.dismiss(item.id)
                    return

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Ask before something that can't be undone from here."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, heading: str, message: str, confirm: str, danger: bool = False):
        super().__init__()
        self.heading, self.message, self.confirm, self.danger = heading, message, confirm, danger

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label(self.heading, id="form-heading")
            yield Static(self.message, id="form-note")
            with Horizontal(id="form-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm, variant="error" if self.danger else "primary", id="confirm")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()  # the safe answer is the default

    @on(Button.Pressed, "#confirm")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


VISIBILITY = {
    "private": "Only you",
    "org_only": "Everyone in your organization",
    "org": "Everyone in your organization",
    "public": "Anyone with the link",
}


class ShareScreen(ModalScreen[str | None]):
    """Who can see a session. Changing it happens on claude.ai; this shows where it stands."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label("Share", id="form-heading")
            yield Static("Checking…", id="share-state")
            with Horizontal(id="form-buttons"):
                yield Button("Copy link", id="copy")
                yield Button("Open on claude.ai", id="open", variant="primary")
                yield Button("Close", id="close")

    def show(self, status: dict | None, error: str = "") -> None:
        box = self.query_one("#share-state", Static)
        if status is None:
            box.update(Text(f"Couldn't read the sharing settings: {error}", style=MUTED))
            return
        visibility = str(status.get("effective_visibility") or "")
        text = Text()
        text.append("Visible to: ", style=MUTED)
        text.append(VISIBILITY.get(visibility, visibility or "unknown"), style=f"bold {SAND}")
        if visibility == "private":
            options = VISIBILITY.get(str(status.get("prospective_visibility_type") or ""), "")
            if options:
                text.append(f"\nIf you share it, it goes to: {options}", style=MUTED)
        text.append("\nSharing is changed on claude.ai. The link below only works for people who can already see it.", style=MUTED)
        box.update(text)

    @on(Button.Pressed, "#copy")
    def _copy(self) -> None:
        self.dismiss("copy")

    @on(Button.Pressed, "#open")
    def _open(self) -> None:
        self.dismiss("open")

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


class PullRequestScreen(ModalScreen[None]):
    """A PR's summary, CI checks, changed files and diff, as plain selectable text.
    The checks, files and diff come from the gh CLI."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("o", "open_pr", "Open on GitHub"),
        Binding("r", "reload", "Reload"),
    ]

    def __init__(self, pr: PullRequest, details: Details | None = None):
        super().__init__()
        self.pr = pr
        self.details = details
        self.diff: str | None = None
        self.diff_error = ""
        self.busy = prinfo.gh_available()

    def compose(self) -> ComposeResult:
        with Vertical(id="prscreen"):
            with ScrollableContainer(id="pr-scroll"):
                yield Static(id="pr-summary")
                yield Static(id="pr-checks")
                yield Static(id="pr-files")
                yield Static(id="pr-diff")
            yield Static("drag to select, ctrl+c copies · o opens the PR · r reloads · esc closes", id="pr-hint")

    def on_mount(self) -> None:
        self._paint()
        self.query_one("#pr-scroll", ScrollableContainer).focus()
        if self.busy:
            self._load()

    @work(exclusive=True, group="pr-load")
    async def _load(self) -> None:
        self.busy = True
        self._paint()
        self.details = await asyncio.to_thread(prinfo.gh_details, self.pr.url)
        self._paint()
        self.diff, self.diff_error = await asyncio.to_thread(prinfo.gh_diff, self.pr.url)
        self.busy = False
        self._paint()

    def _paint(self) -> None:
        pr, details = self.pr, self.details
        colour = PR_COLOURS.get(pr.state, MUTED)
        summary = Text(no_wrap=True)
        summary.append("⎇ ", style=colour)
        summary.append(pr.label, style=f"bold {SAND}")
        summary.append(f"  {pr.state.capitalize()}\n", style=colour)
        if pr.title:
            summary.append(f"{pr.title}\n", style=f"bold {SAND}")
        facts = [f"+{pr.additions} −{pr.deletions}", f"{pr.commits} commit{'s' if pr.commits != 1 else ''}"]
        if pr.base:
            facts.append(f"into {pr.base}")
        review = details.review if details and details.review else pr.review
        if review in REVIEW_LABELS:
            facts.append(REVIEW_LABELS[review][0])
        if details and details.mergeable and details.mergeable != "UNKNOWN":
            facts.append(details.mergeable.lower())
        summary.append("  ·  ".join(facts) + "\n", style=MUTED)
        if pr.url:
            summary.append(pr.url, style=link_style(pr.url, MUTED))
        self.query_one("#pr-summary", Static).update(summary)

        checks = Text(no_wrap=True)
        checks.append("\nChecks\n", style=f"bold {CLAY}")
        found = details.checks if details and details.checks else pr.checks
        if found:
            order = {"failure": 0, "pending": 1, "success": 2, "skipped": 3}
            for check in sorted(found, key=lambda c: (order.get(c.state, 4), c.name)):
                glyph, glyph_colour = CHECK_GLYPHS[check.state]
                checks.append(f"  {glyph} ", style=glyph_colour)
                checks.append(check.name, style=link_style(check.url, SAND) if check.url else SAND)
                checks.append(f"  {check.state}\n", style=MUTED)
        elif not prinfo.gh_available():
            checks.append("  CI results need the gh CLI: install it and run `gh auth login`.\n", style=MUTED)
        elif details and details.error:
            checks.append(f"  Couldn't read checks: {details.error}\n", style=MUTED)
        elif self.busy and details is None:
            checks.append("  Loading…\n", style=MUTED)
        else:
            checks.append("  No checks reported.\n", style=MUTED)
        self.query_one("#pr-checks", Static).update(checks)

        files = Text(no_wrap=True)
        if details and details.files:
            files.append(f"\nFiles changed ({len(details.files)})\n", style=f"bold {CLAY}")
            for path, added, removed in details.files:
                files.append(f"  +{added:<5}", style=SAGE)
                files.append(f"−{removed:<5}", style=BRICK)
                files.append(f"{path}\n", style=SAND)
        self.query_one("#pr-files", Static).update(files)

        diff = Text(no_wrap=True)
        diff.append("\nDiff\n", style=f"bold {CLAY}")
        if self.diff:
            for line in self.diff.splitlines():
                diff.append(line + "\n", style=self._diff_style(line))
        elif self.diff_error:
            diff.append(f"  Couldn't read the diff: {self.diff_error}\n", style=MUTED)
        elif not prinfo.gh_available():
            diff.append("  The diff needs the gh CLI: install it and run `gh auth login`.\n", style=MUTED)
        elif self.busy:
            diff.append("  Loading…\n", style=MUTED)
        else:
            diff.append("  No changes.\n", style=MUTED)
        self.query_one("#pr-diff", Static).update(diff)
        self.query_one("#pr-scroll", ScrollableContainer).scroll_home(animate=False)

    @staticmethod
    def _diff_style(line: str) -> str:
        if line.startswith(("diff --git", "index ", "+++", "---", "new file", "deleted file", "similarity", "rename")):
            return f"bold {MUTED}"
        if line.startswith("@@"):
            return "#6fb3c9"
        if line.startswith("+"):
            return SAGE
        if line.startswith("-"):
            return BRICK
        return SAND

    def plain_text(self) -> str:
        """Everything on the screen as text, which is also what selecting all would copy."""
        return "\n".join(
            str(self.query_one(f"#pr-{part}", Static).render()) for part in ("summary", "checks", "files", "diff")
        )

    def action_close(self) -> None:
        self.dismiss(None)

    def action_open_pr(self) -> None:
        if self.pr.url:
            self.app.open_link(self.pr.url)

    def action_reload(self) -> None:
        if prinfo.gh_available():
            self._load()


HELP_KEYS = [
    ("n", "Home: start a new cloud session"),
    ("i", "Add a session by link or ID"),
    ("m", "Session menu (also: right-click, or the ⋮ button)"),
    ("o / y", "Open on claude.ai / copy link"),
    ("d / f / p", "Panels on the right: Changes, Files, Plan (z expands)"),
    ("D", "Pull request summary and CI checks"),
    ("t / c", "Teleport / set local checkout"),
    ("/", "Search sessions"),
    ("v", "Show or hide archived"),
    ("g", "Show or hide groups (made with m > Move to group)"),
    ("r", "Refresh now"),
    ("x", "Forget a locally saved session"),
    ("esc", "Back to the session list"),
    ("q", "Quit (ctrl+q works anywhere)"),
    ("f1", "This help, even while typing"),
    ("drag", "Select text; ctrl+c copies it"),
    ("click", "Open a link (URLs, owner/repo#123)"),
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


class RowList(OptionList):
    """A list of sessions where only a left click selects a row; a right click is for the session menu."""

    def _on_click(self, event: events.Click) -> None:
        if event.button != 1:
            event.prevent_default()  # stops OptionList's own handler, which doesn't look at the button


# ---------------------------------------------------------------------- app


def background(**options):
    """@work for refreshes that update the main UI. If the app closes while one is in flight there's
    nothing left to update, so that case ends quietly instead of crashing the exit."""

    def decorate(method):
        @functools.wraps(method)
        async def guarded(self, *args, **kwargs):
            try:
                return await method(self, *args, **kwargs)
            except NoMatches:
                if self._closing or not self.screen_stack:
                    return None
                raise

        return work(**options)(guarded)

    return decorate


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

    #convo {{ height: 1fr; }}
    #convo-main {{ width: 1fr; }}
    #side {{ display: none; width: 58%; border-left: tall $panel; padding: 0 0 0 1; }}
    #side.max {{ width: 100%; border-left: none; padding: 0; }}
    PlanPane, FilesPane, ChangesPane {{ display: none; }}
    PlanPane {{ height: auto; max-height: 45%; margin-bottom: 1; }}
    FilesPane, ChangesPane {{ height: 1fr; }}
    .pane-head {{ height: 1; margin: 1 0; }}
    .pane-title {{ width: 1fr; text-style: bold; color: {SAND}; }}
    .pane-head Chip {{ margin-left: 1; margin-right: 0; }}
    #plan-scroll {{ height: auto; max-height: 100%; }}
    #plan-body {{ padding: 0 1; }}
    #changes-body, #files-body {{ height: 1fr; }}
    #changes-left {{ width: 38; }}
    #changes-tree, #files-tree {{ background: $background; padding: 0; }}
    #changes-tree {{ height: 1fr; }}
    #files-tree {{ width: 34; }}
    #commits-label {{ margin-top: 1; padding: 0 1; }}
    #changes-commits {{ height: 40%; background: $background; border: none; padding: 0; }}
    #changes-diff-scroll, #files-view-scroll {{ width: 1fr; height: 1fr; background: $background; }}
    #changes-diff, #files-view {{ width: auto; min-width: 100%; height: auto; padding: 0 1; }}
    #files-search {{ border: tall $panel; background: $surface; margin-bottom: 1; }}
    #files-search:focus {{ border: tall {CLAY}; }}
    #header-row Chip.on {{ background: {CLAY} 45%; }}
    #header-row {{ height: auto; border-bottom: hkey $panel; }}
    #session-header {{ width: 1fr; height: auto; padding: 1 0 1 0; }}
    #header-row Chip {{ margin: 1 0 0 1; }}
    #transcript {{ height: 1fr; background: $background; border: none; padding: 1 1 0 0; scrollbar-size-vertical: 1; }}
    #transcript Static {{ width: 100%; height: auto; }}
    #transcript .who {{ margin-top: 1; text-style: bold; }}
    #transcript .tool {{ text-wrap: nowrap; text-overflow: ellipsis; color: {MUTED}; }}
    #transcript .note {{ color: {MUTED}; text-style: italic; }}
    #transcript Markdown {{ margin: 0; padding: 0; background: $background; color: {SAND}; }}
    #transcript Markdown MarkdownBlock {{ color: {SAND}; }}
    #transcript Markdown MarkdownFence {{ background: $surface; }}
    #activity {{ height: 1; color: {SAGE}; }}
    #context-bar {{ height: auto; padding: 0 1; }}
    #composer {{ margin: 0 0 1 0; border: tall $panel; background: $surface; }}
    #composer:focus {{ border: tall {CLAY}; }}

    #form, #choice {{ width: 76; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: tall {CLAY}; }}
    #prscreen {{ width: 96%; height: 92%; padding: 1 2; background: $surface; border: tall {CLAY}; }}
    #pr-scroll {{ height: 1fr; background: $surface; }}
    #pr-scroll Static {{ width: auto; min-width: 100%; height: auto; }}
    #pr-hint {{ height: 1; color: {MUTED}; margin-top: 1; }}
    #menu {{ width: 46; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: tall {CLAY}; }}
    #menu-heading {{ text-style: bold; margin-bottom: 1; width: 100%; text-wrap: nowrap; text-overflow: ellipsis; }}
    #menu-items {{ height: auto; background: $surface; border: none; padding: 0; }}
    #menu-items > .option-list--option-disabled {{ color: {MUTED}; }}
    #share-state {{ margin-bottom: 1; }}
    MenuScreen, ConfirmScreen, ShareScreen {{ align: center middle; }}
    PullRequestScreen {{ align: center middle; }}
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
        Binding("m", "session_menu", "Menu"),
        Binding("d", "toggle_changes", "Changes"),
        Binding("f", "toggle_files", "Files"),
        Binding("p", "toggle_plan", "Plan"),
        Binding("z", "toggle_side_max", "Expand panel", show=False),
        Binding("D", "pull_request", "PR checks", show=False),
        Binding("t", "teleport", "Teleport"),
        Binding("y", "copy_link", "Copy link"),
        Binding("c", "set_checkout", "Checkout path", show=False),
        Binding("slash", "search", "Search"),
        Binding("v", "toggle_archived", "Archived"),
        Binding("g", "toggle_groups", "Groups"),
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
        self.echoes: list[Echo] = []
        self._ticks = 0
        self.events_cursor: str | None = None
        self.events_error_for: str | None = None
        self.api_ok = False
        self.loaded_once = False
        self.show_archived = False
        self.group_view = self.store.grouping.grouped
        self.filter_text = ""
        self.optimistic: dict[str, float] = {}
        self.frame = 0
        self.envs: list[Environment] = []
        self.branches: dict[str, list[BranchStatus]] = {}  # per session: PRs and pushed branches
        self.details: dict[str, Details] = {}  # per PR url: what gh adds (CI, files)
        self.side_open = {"plan": False, "files": False, "changes": False}
        self.side_max = False
        self.target_idx: dict[str, int] = {}  # per session: which comparison the Changes and Files panels show
        self.changes_commit: str | None = None  # a single commit picked in Changes, or None for all changes
        self.cmp_cache: dict[tuple, tuple[float, Comparison]] = {}
        self.commit_cache: dict[tuple, list[ChangedFile]] = {}
        self.tree_cache: dict[tuple, tuple[float, list[str], bool, str]] = {}
        self._pane_sig: dict[str, Any] = {}
        self.board = TaskBoard()  # the open session's plan, rebuilt from its conversation
        self.checks: dict[tuple[str, int], list[Check]] = {}  # CI for live PRs of the newest sessions, from one gh call
        self.draft = self._initial_draft()
        self._seeded = False
        self._closing = False
        self.creating = False
        self._suppress_highlight = False
        self._after_tool = False

    @property
    def main(self):
        """The base screen. App.query_one searches the top screen, which is a modal whenever one is open,
        and background refreshes must keep updating the main UI underneath it."""
        if not self.screen_stack:
            raise NoMatches("the app is closing")
        return self.screen_stack[0]

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("Cloud sessions", id="brand")
                yield Static("Loading…", id="account")
                yield Button("+ New session", id="new", variant="primary", compact=True)
                yield Input(placeholder="Search sessions", id="search")
                yield Static("Ungrouped", id="list-heading")
                yield RowList(id="sessions")
            with Vertical(id="main"):
                yield Static("", id="banner")
                with ContentSwitcher(id="views", initial="home"):
                    with Vertical(id="home"):
                        yield Static("", id="welcome")
                        yield Static("Sessions", id="home-label")
                        yield RowList(id="attention")
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
                    with Horizontal(id="convo"):
                        with Vertical(id="convo-main"):
                            with Horizontal(id="header-row"):
                                yield Static("", id="session-header")
                                yield Chip("±", "toggle:changes", tip="Changes: files, commits and diffs (d)", classes="plain")
                                yield Chip("▤", "toggle:files", tip="Files in the repository (f)", classes="plain")
                                yield Chip("✓", "toggle:plan", tip="Plan: Claude's task list (p)", classes="plain")
                                yield Chip("⋮", "menu", classes="plain", tip="Session menu (m, or right-click a session)")
                            yield VerticalScroll(id="transcript")
                            yield Static("", id="activity")
                            yield Static("", id="context-bar")
                            yield Input(placeholder="Reply to this session", id="composer")
                        with Vertical(id="side"):
                            yield PlanPane(id="pane-plan")
                            yield FilesPane(id="pane-files")
                            yield ChangesPane(id="pane-changes")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "ccweb"
        name = welcome_name()
        self.main.query_one("#welcome", Static).update(
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
        self.set_interval(15, self.refresh_selected_pr)
        self.main.query_one("#prompt", Input).focus()

    async def on_unmount(self) -> None:
        self._closing = True
        await self.api.aclose()

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_attention)

    # ------------------------------------------------------------------ data

    @background(exclusive=True, group="sessions")
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
        waiting = sum(1 for s in live if status_kind(s, self.optimistic, self._pr_state(s.id)) == "needs_input" and s.unread)
        working = sum(1 for s in live if status_kind(s, self.optimistic) == "working")
        account = Text(f"{len(live)} sessions" if self.api_ok else "claude.ai not reachable")
        if waiting:
            account.append(" · ", style=MUTED)
            account.append(f"{waiting} need you", style=AMBER)
        if working:
            account.append(" · ", style=MUTED)
            account.append(f"{working} working", style=SAGE)
        self.main.query_one("#account", Static).update(account)
        self._seed_draft()
        self._render_list()
        self._render_attention()
        self._render_header()
        self._render_activity()
        self.refresh_prs()

    def _pr_state(self, sid: str) -> str:
        return best_pr_state(self.branches.get(sid, []))

    @background(exclusive=True, group="prs")
    async def refresh_prs(self) -> None:
        """PR state for the newest sessions in one batched call, for the sidebar icons and the home page."""
        if not self.api_ok:
            return
        recent = [s for s in by_recency(self.sessions.values()) if s.from_server and not s.archived][:40]
        try:
            found = await self.api.branch_statuses(recent)
        except (ApiError, AuthError):
            return  # PR info is a bonus; the rest of the app doesn't depend on it
        selected = self.selected
        for sid, statuses in found.items():
            if sid != selected or sid not in self.branches:  # the open session gets richer data from its own refresh
                self.branches[sid] = statuses
        try:
            self._render_list()
            self._render_attention()
            self._render_context()
        except NoMatches:  # the app was closed while the request was in flight
            pass
        if not prinfo.gh_available():
            return
        live = {
            ref_key(s.pr.repo, s.pr.number): (s.pr.repo, s.pr.number)
            for sid in found
            for s in relevant(self.branches.get(sid, []))
            if s.pr and s.pr.is_live
        }
        if not live:
            return
        checks = await asyncio.to_thread(prinfo.gh_checks_batch, list(live.values()))
        self.checks.update(checks)
        try:
            self._render_list()
            self._render_context()
        except NoMatches:
            pass

    def _checks_for(self, pr: PullRequest) -> list[Check]:
        """The best CI information held for a PR: the open session's own detail, else the batched lookup."""
        details = self.details.get(pr.url)
        if details and details.checks:
            return details.checks
        return self.checks.get(ref_key(pr.repo, pr.number)) or pr.checks

    @background(exclusive=True, group="pr-selected")
    async def refresh_selected_pr(self) -> None:
        """The open session's PRs with review state, plus CI and files from gh for the live ones."""
        sid = self.selected
        session = self.sessions.get(sid or "")
        if not sid or session is None or not session.from_server or not self.api_ok:
            return
        try:
            found = await self.api.branch_statuses([session], review=True)
        except (ApiError, AuthError):
            return
        self.branches[sid] = found.get(sid, [])
        if prinfo.gh_available():
            live = [s.pr for s in relevant(self.branches[sid]) if s.pr and s.pr.is_live][:3]
            for pr in live:
                self.details[pr.url] = await asyncio.to_thread(prinfo.gh_details, pr.url)
        if self.selected == sid:
            try:
                self._render_context()
                self._render_header()
                self._update_row(sid)
                self._reload_panes()
            except NoMatches:
                pass

    def _render_context(self) -> None:
        """The strip above the reply box: environment, repositories, the branch Claude pushes to, and PR/CI lines."""
        bar = self.main.query_one("#context-bar", Static)
        session = self.sessions.get(self.selected or "")
        if session is None:
            bar.update("")
            return
        text = Text()
        env = next((e.name for e in self.envs if e.id == session.environment_id), "")
        if env:
            text.append(f"☁ {env}", style=SAND)
        sources = session.sources or ([(session.repo, "")] if session.repo else [])
        owners = {repo.split("/")[0] for repo, _ in sources if "/" in repo}
        for repo, revision in sources:
            if not repo:
                continue
            text.append("   " if text.plain else "")
            name = repo.split("/", 1)[-1] if len(owners) == 1 else repo
            text.append(f"</> {name}", style=link_style(f"https://github.com/{repo}", SAND))
            if revision:
                text.append(f" ⎇ {revision}", style=link_style(f"https://github.com/{repo}/tree/{revision}", MUTED))
        branches = list(dict.fromkeys(branch for _, branch in session.outcomes))
        if len(branches) == 1:
            text.append("\n" if text.plain else "")
            text.append(f"⎇ {branches[0]}", style=MUTED)
        elif branches:
            text.append("\n" if text.plain else "")
            text.append("   ".join(f"⎇ {repo.split('/', 1)[-1]}: {branch}" for repo, branch in session.outcomes), style=MUTED)
        shown = relevant(self.branches.get(session.id, []))
        for status in shown[:3]:
            text.append("\n" if text.plain else "")
            text.append_text(pr_line(status, self.details.get(status.pr.url) if status.pr else None, self._checks_for(status.pr) if status.pr else None))
        if len(shown) > 3:
            text.append(f"\n+{len(shown) - 3} more", style=MUTED)
        bar.update(text)

    @background(exclusive=True, group="more")
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

    @background(exclusive=True, group="events")
    async def refresh_events(self) -> None:
        sid = self.selected
        if not sid or not self.api_ok:
            return
        try:
            fresh: list[Message] = []
            task_ops: list[dict] = []
            cursor = self.events_cursor
            for _ in range(20):  # a long session is several pages the first time, usually one afterwards
                page = await self.api.list_events_page(sid, cursor)
                fresh += page.messages
                task_ops += page.task_ops
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
        if task_ops:
            self.board.apply(task_ops)
            self._render_plan()
        shown = [m for m in fresh if m.kind != "event" and m.key not in self.rendered_keys]
        if not shown and not self.rendered_keys and not fresh:
            self._note("No messages yet.")
            self.rendered_keys.add("__empty__")
        for message in shown:
            self.rendered_keys.add(message.key)
            self._reconcile_echo(message)
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

    @background(exclusive=True, group="envs")
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
        banner = self.main.query_one("#banner", Static)
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
        """A sidebar row: the title, then a line of PR and CI status (or what else is worth knowing)."""
        pr_state = self._pr_state(session.id)
        glyph, colour, _ = describe_status(session, self.optimistic, self.frame, pr_state)
        kind = status_kind(session, self.optimistic, pr_state)
        if pr_state and not session.unread and kind not in {"working", "failed", "archived"}:
            glyph, colour = "⎇", PR_ICON_COLOURS.get(pr_state, MUTED)  # a seen session with a PR shows the PR, like the web
        title = Text(no_wrap=True)
        title.append(f"{glyph} ", style=colour)
        title.append(session.title or session.id, style=SAND)
        detail = Text(no_wrap=True)
        detail.append("  ")
        shown = relevant(self.branches.get(session.id, []))
        for index, (part, part_colour) in enumerate(row_summary(session, kind, shown, self._checks_for)):
            if index:
                detail.append(" · ", style=MUTED)
            detail.append(part, style=part_colour)
        width = self.main.query_one("#sessions", OptionList).size.width
        if width > 6:  # the list wraps long rows itself, so cut them with an ellipsis like the web does
            title.truncate(width - 3, overflow="ellipsis")
            detail.truncate(width - 3, overflow="ellipsis")
        return Text.assemble(title, "\n", detail)

    def _sidebar_blocks(self, visible: list[Session], grouping: Grouping | None) -> list[tuple[str, list[Session], Group | None]]:
        """The sidebar as (kind, sessions, group) blocks, top to bottom: pinned, each group, then the rest.

        The rest, kind "sessions", is everything not placed above it, in the order given.
        """
        if grouping is None:
            return [("sessions", visible, None)]
        by_id = {s.id: s for s in visible}
        pinned = [by_id[sid] for sid in grouping.pinned if sid in by_id]
        placed = {s.id for s in pinned}
        blocks: list[tuple[str, list[Session], Group | None]] = [("pinned", pinned, None)]
        for group in grouping.groups:
            members = by_recency(by_id[m] for m in dict.fromkeys(group.members) if m in by_id and m not in placed)
            placed.update(m.id for m in members)
            blocks.append(("group", members, group))
        blocks.append(("sessions", [s for s in visible if s.id not in placed], None))
        return blocks

    def _active_grouping(self) -> Grouping | None:
        grouping = self.store.grouping
        return grouping if (grouping.groups or grouping.pinned) and self.group_view else None

    def _render_list(self) -> None:
        option_list = self.main.query_one("#sessions", OptionList)
        visible = self._visible_sessions()
        filtering = bool(self.filter_text)
        stored = self.store.grouping
        grouping = self._active_grouping()
        heading = self.main.query_one("#list-heading", Static)
        if not (stored.groups or stored.pinned):
            heading.update("Ungrouped")
        elif grouping is None:
            heading.update("Ungrouped · g shows groups")
        heading.display = grouping is None  # with groups on, the section headers say it all

        options: list[Option] = []
        shown_ids: set[str] = set()
        for kind, members, group in self._sidebar_blocks(visible, grouping):
            if kind == "pinned":
                if members:
                    options.append(Option(Text("Pinned", style=f"bold {MUTED}"), disabled=True))
            elif kind == "group":
                if not members:
                    continue  # empty groups, and groups a search doesn't match, are left out
                collapsed = group.collapsed and not filtering
                options.append(self._group_header(group, len(members), collapsed))
                if collapsed:
                    continue
            elif grouping is not None and members:
                options.append(Option(Text("Ungrouped", style=f"bold {MUTED}"), disabled=True))
            if kind == "sessions":
                members_shown = members if filtering else members[: self.visible_limit]
                hidden = len(members) - len(members_shown)
            else:
                members_shown, hidden = members, 0
            options += [Option(self._row(s), id=s.id) for s in members_shown]
            shown_ids.update(s.id for s in members_shown)
            if hidden > 0:
                options.append(Option(Text(f"  Show {min(PAGE, hidden)} more", style=MUTED), id="__more__"))
        if self.has_more and not filtering:
            options.append(Option(Text("Load more sessions", style=MUTED), id="__load__"))
        target = self.selected if self.selected in shown_ids else None
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

    def _group_header(self, group: Group, count: int, collapsed: bool) -> Option:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(f"{'▸' if collapsed else '▾'} {group.name}", style=f"bold {MUTED}")
        text.append(f"  {count}", style=MUTED)
        return Option(text, id=f"__grp__:{group.id}")

    def action_toggle_groups(self) -> None:
        grouping = self.store.grouping
        if not (grouping.groups or grouping.pinned):
            self.notify("No groups yet. Press m on a session and choose Move to group.", severity="warning", timeout=6)
            return
        self.group_view = not self.group_view
        grouping.grouped = self.group_view
        self.store.save_grouping(grouping)
        self._render_list()

    def _save_grouping(self, grouping: Grouping) -> None:
        self.store.save_grouping(grouping)
        self._render_list()

    def _keep_header_highlighted(self, gid: str) -> None:
        """After the rows under a group header change, leave the cursor on that header."""
        try:
            option_list = self.main.query_one("#sessions", OptionList)
            self._suppress_highlight = True
            option_list.highlighted = option_list.get_option_index(f"__grp__:{gid}")
        except Exception:  # the header is gone (an emptied group)
            pass
        finally:
            self._suppress_highlight = False

    def _toggle_group(self, gid: str) -> None:
        grouping = self.store.grouping
        group = grouping.get(gid)
        if group is None:
            return
        group.collapsed = not group.collapsed
        self._save_grouping(grouping)
        self._keep_header_highlighted(gid)

    def _update_row(self, sid: str) -> None:
        session = self.sessions.get(sid)
        if session is None:
            return
        try:
            self.main.query_one("#sessions", OptionList).replace_option_prompt(sid, self._row(session))
        except Exception:  # row not on screen (filtered out, or not shown yet)
            pass

    def _attention_row(self, session: Session, width: int) -> Text:
        glyph, colour, label = describe_status(session, self.optimistic, self.frame, self._pr_state(session.id))
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
        option_list = self.main.query_one("#attention", OptionList)
        pr_states = {sid: self._pr_state(sid) for sid in self.branches}
        rows = home_sessions(list(self.sessions.values()), self.optimistic, pr_states=pr_states)
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
        """Title, status and model, and what Claude is waiting on or last did. Repositories, the branch and
        PRs live in the strip above the reply box."""
        header = self.main.query_one("#session-header", Static)
        session = self.sessions.get(self.selected or "")
        if session is None:
            self.sub_title = ""
            header.update("")
            return
        self.sub_title = session.title or session.id
        pr_state = self._pr_state(session.id)
        glyph, colour, label = describe_status(session, self.optimistic, self.frame, pr_state)
        text = Text()
        text.append(f"{glyph} ", style=colour)
        text.append(session.title or "Untitled session", style=f"bold {SAND}")
        text.append("\n  ")
        text.append(label, style=colour)
        facts = [
            model_label(session.model) if session.model else "",
            label_for(EFFORTS, session.effort, session.effort).lower() if session.effort else "",
            label_for(PERMISSION_MODES, session.permission_mode).lower() if session.permission_mode else "",
        ]
        updated = relative_time(session.last_activity)
        if updated:
            facts.append(f"updated {updated} ago" if updated[:-1].isdigit() else f"updated {updated}")
        for fact in filter(None, facts):
            text.append(" · ", style=MUTED)
            text.append(fact, style=MUTED)
        text.append(" · ", style=MUTED)
        text.append("claude.ai ↗", style=link_style(web_url(session.id), MUTED))
        if status_kind(session, self.optimistic, pr_state) == "needs_input" and session.needs_action:
            text.append("\n  Waiting on you: ", style=AMBER)
            text.append(" ".join(session.needs_action.split())[:240], style=AMBER)
        elif session.summary:
            text.append("\n  " + " ".join(session.summary.split())[:240], style=MUTED)
        header.tooltip = session.id
        header.update(text)

    def _render_activity(self) -> None:
        activity = self.main.query_one("#activity", Static)
        session = self.sessions.get(self.selected or "")
        if session is not None and status_kind(session, self.optimistic) == "working":
            activity.update(Text(f"{SPINNER[self.frame % len(SPINNER)]} Claude is working…", style=SAGE))
        else:
            activity.update("")

    def _tick(self) -> None:
        """Animate the spinners: working sessions' sidebar rows and the open conversation's status line."""
        self.frame += 1
        self._ticks += 1
        if self.echoes and self._ticks % 20 == 0:  # about every 2.5 s while a message of yours isn't recorded yet
            self.refresh_events()
        working = [s for s in self.sessions.values() if status_kind(s, self.optimistic) == "working"]
        if not working:
            return
        try:
            self._render_activity()
            for session in working:
                self._update_row(session.id)
            if self.selected in {s.id for s in working}:
                self._render_header()
        except NoMatches:
            pass  # closing

    def _link_context(self) -> LinkContext:
        """Where a bare #123 in this session's text points: its PR's repository, else its only repository."""
        session = self.sessions.get(self.selected or "")
        statuses = relevant(self.branches.get(self.selected or "", []))
        pr_repos = {s.pr.number: s.pr.repo for s in statuses if s.pr}
        repos = {repo for repo, _ in (session.sources if session else []) if repo}
        return LinkContext(default_repo=next(iter(repos)) if len(repos) == 1 else "", pr_repos=pr_repos)

    def _note(self, text: str) -> None:
        self._append([Static(Text(text), classes="note")])

    def _append(self, widgets: list) -> None:
        """Add to the end of the transcript, and stay at the bottom if that's where the reader was."""
        if not widgets:
            return
        transcript = self.main.query_one("#transcript", VerticalScroll)
        follow = transcript.scroll_y >= transcript.max_scroll_y - 1
        transcript.mount(*widgets)
        if follow:
            transcript.call_after_refresh(transcript.scroll_end, animate=False)

    def _message_widgets(self, message: Message) -> list:
        ctx = self._link_context()
        if message.kind == "text":
            if message.role == "assistant":
                return [
                    Static(Text("Claude", style=f"bold {CLAY}"), classes="who"),
                    Markdown(linkify_markdown(message.text, ctx), open_links=False, classes="assistant"),
                ]
            if message.role == "user":
                body = Text()
                for i, line in enumerate(message.text.splitlines() or [""]):
                    body.append("\n" if i else "")
                    body.append("▎ ", style=CLAY)
                    body.append_text(linkify_text(line, ctx, SAND))
                return [Static(Text("You", style=f"bold {SAND}"), classes="who"), Static(body, classes="user")]
            return [Static(linkify_text(f"{message.role}: {message.text}", ctx, MUTED), classes="note")]
        if message.kind == "tool_use":
            return [Static(linkify_text(f"  ⚙ {message.text}", ctx, MUTED), classes="tool")]
        if message.kind == "tool_result":
            return [Static(linkify_text(f"    ↳ {message.text}", ctx, MUTED), classes="tool")]
        return [Static(Text(f"  {message.text}"), classes="note")]

    def _write_message(self, message: Message) -> None:
        self._append(self._message_widgets(message))

    def transcript_text(self) -> str:
        """The conversation as plain text, as shown (what selecting all of it would copy)."""
        parts = []
        for child in self.main.query_one("#transcript", VerticalScroll).children:
            parts.append(child.source if isinstance(child, Markdown) else str(child.render()))
        return "\n".join(parts)

    # ------------------------------------------------------------------ links and clipboard

    def action_open_link(self, url: str) -> None:
        """Open a link the reader clicked. A click that ends a text selection is not a click on the link."""
        if self.screen.get_selected_text():
            return
        self.open_link(url)

    def open_link(self, url: str) -> None:
        if not is_openable(url):
            self.notify("Only web links are opened from here.", severity="warning")
            return
        self.open_url(url)

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        self.action_open_link(event.href)

    def copy_to_clipboard(self, text: str) -> None:
        """OSC 52 reaches the clipboard in iTerm2, kitty, WezTerm and others, but not Terminal.app or tmux
        by default, so also hand the text to the system clipboard tool when there is one."""
        super().copy_to_clipboard(text)
        if self.is_headless:
            return
        for tool in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(tool[0]):
                try:
                    subprocess.run(tool, input=text.encode(), timeout=3, check=True)
                except (OSError, subprocess.SubprocessError):
                    continue
                return

    # ------------------------------------------------------------------ selection

    def _show(self, view: str) -> None:
        self.main.query_one("#views", ContentSwitcher).current = view

    def _select(self, sid: str | None) -> None:
        if sid is None:
            self.selected = None
            self._show("home")
            self._render_header()
            self._render_context()
            return
        self._show("convo")
        if sid == self.selected:
            return
        self.selected = sid
        self.board = TaskBoard()
        self.echoes = []
        self.changes_commit = None
        self._pane_sig = {}
        self.rendered_keys = set()
        self.events_cursor = None
        self.events_error_for = None
        self._after_tool = False
        self.main.query_one("#transcript", VerticalScroll).remove_children()
        self._render_header()
        self._render_activity()
        self._render_context()
        self._render_plan()
        self._reload_panes()
        self.refresh_selected_pr()
        if sid in self.sessions and not self.api_ok:
            self._note(
                "The conversation can't be loaded because claude.ai isn't reachable. "
                "You can still reply below, teleport (t) or open it on the web (o)."
            )
        self._sync_highlight()
        self.refresh_events()

    def _sync_highlight(self) -> None:
        option_list = self.main.query_one("#sessions", OptionList)
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
        if sid and sid.startswith("__grp__:"):
            self._toggle_group(sid.split(":", 1)[1])
        elif sid == "__more__":
            self.visible_limit += PAGE
            self._render_list()
        elif sid == "__load__":
            self.load_more()
        elif sid:
            self._select(sid)
            self.main.query_one("#composer", Input).focus()

    @on(OptionList.OptionSelected, "#attention")
    def _open_from_home(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self._select(event.option.id)
            self.main.query_one("#composer", Input).focus()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self._render_list()

    @on(Input.Submitted, "#search")
    def _search_done(self) -> None:
        self.main.query_one("#sessions", OptionList).focus()

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
        chips = {c.what: c for c in self.main.query("#settings Chip").results(Chip)}
        chips["mode"].label = label_for(PERMISSION_MODES, self.draft["mode"])
        chips["model"].label = model_label(self.draft["model"])
        chips["effort"].label = label_for(EFFORTS, self.draft["effort"], "Default")

    async def _rebuild_chips(self) -> None:
        row = self.main.query_one("#chips", Horizontal)
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
        if what == "menu":
            self.action_session_menu()
        elif what.startswith("toggle:"):
            self.toggle_pane(what.split(":", 1)[1])
        elif what.startswith("pane-close:"):
            self.close_pane(what.split(":", 1)[1])
        elif what == "side-max":
            self.action_toggle_side_max()
        elif what.startswith("target:"):
            self.pick_target()
        elif what == "pr":
            self.action_pull_request()
        elif what == "env":
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

    @background(group="default-branch")
    async def resolve_default_branch(self, repo: str) -> None:
        branch = await asyncio.to_thread(gitinfo.default_branch, repo)
        if not branch:
            return
        for item in self.draft["repos"]:
            if item["repo"] == repo and not item["branch"]:
                item["branch"] = branch
        self.call_later(self._rebuild_chips)  # not saved: only your own choices are remembered

    @background(group="branches")
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

    @background(group="create")
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
        self.main.query_one("#prompt", Input).value = ""
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
        self._add_echo(prompt, "sent · waiting for the session to start")
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
        echo = self._add_echo(text, "sending…")
        self.send_message(session.id, text, echo)

    def _add_echo(self, text: str, status: str) -> Echo:
        """Show a message you've sent right away, marked as not yet recorded."""
        ctx = self._link_context()
        body = Text()
        for i, line in enumerate(text.splitlines() or [""]):
            body.append("\n" if i else "")
            body.append("▎ ", style=CLAY)
            body.append_text(linkify_text(line, ctx, SAND))
        note = Static(Text(f"  {status}", style=f"italic {MUTED}"), classes="note")
        widgets = [Static(Text("You", style=f"bold {SAND}"), classes="who"), Static(body, classes="user"), note]
        self._append(widgets)
        echo = Echo(text, widgets, note)
        self.echoes.append(echo)
        return echo

    def _set_echo_status(self, echo: Echo, status: str) -> None:
        try:
            echo.status.update(Text(f"  {status}", style=f"italic {MUTED}"))
        except Exception:  # its conversation was closed in the meantime
            pass

    def _drop_echo(self, echo: Echo) -> None:
        if echo in self.echoes:
            self.echoes.remove(echo)
        for widget in echo.widgets:
            try:
                widget.remove()
            except Exception:
                pass

    def _reconcile_echo(self, message: Message) -> None:
        """The server has recorded a message of yours: drop its stand-in, whose place the real one takes."""
        if message.role != "user" or message.kind != "text":
            return
        wanted = same_text(message.text)
        echo = next((e for e in self.echoes if same_text(e.text) == wanted), None)
        if echo is not None:
            self._drop_echo(echo)

    @background(group="send")
    async def send_message(self, sid: str, text: str, echo: Echo | None = None) -> None:
        result = await asyncio.to_thread(claude_cli.send_followup, self.claude_bin, sid, text)
        if result.ok:
            if echo is not None:
                self._set_echo_status(echo, "sent · waiting for the session to take it in")
            self.notify("Sent to the cloud session.")
            self.optimistic[sid] = time.monotonic() + OPTIMISTIC_SECONDS
            self._render_activity()
            await asyncio.sleep(1.5)
            self.refresh_events()
            return
        if echo is not None:
            self._drop_echo(echo)  # it never went anywhere, so it isn't part of the conversation
        self.notify(f"Not sent: {result.error}", severity="error", timeout=12)
        composer = self.main.query_one("#composer", Input)
        if not composer.value:
            composer.value = text

    @on(Button.Pressed, "#new")
    def action_new_session(self) -> None:
        self._select(None)
        self.main.query_one("#prompt", Input).focus()

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
        if session is not None:
            self._forget(session)

    def _forget(self, session: Session) -> None:
        if session.id not in self.store.known:
            self.notify("Only sessions saved on this machine can be forgotten here.", severity="warning")
            return
        self.store.remove_known(session.id)
        self.notify("Removed from this machine. The session itself is unchanged.")
        self.refresh_sessions()

    def action_open_web(self) -> None:
        session = self._current()
        if session is not None:
            self._open_web(session)

    def _open_web(self, session: Session) -> None:
        asyncio.get_running_loop().run_in_executor(None, webbrowser.open, web_url(session.id))
        self.notify("Opened on claude.ai.")

    def action_copy_link(self) -> None:
        session = self._current()
        if session is not None:
            self._copy_link(session)

    def _copy_link(self, session: Session) -> None:
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
        if session is not None:
            self._teleport(session)

    def _teleport(self, session: Session) -> None:
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
        self.main.query_one("#search", Input).focus()

    def action_focus_sessions(self) -> None:
        self.main.query_one("#sessions", OptionList).focus()

    def action_toggle_archived(self) -> None:
        self.show_archived = not self.show_archived
        self._render_list()
        self.notify("Showing archived sessions." if self.show_archived else "Hiding archived sessions.")

    # ------------------------------------------------------------------ session menu

    def _menu_target(self) -> str | None:
        """The session a menu opened from the keyboard is about: the highlighted sidebar row, else the open one."""
        sidebar = self.main.query_one("#sessions", OptionList)
        if sidebar.has_focus and sidebar.highlighted is not None:
            option = sidebar.get_option_at_index(sidebar.highlighted)
            if option.id and (option.id.startswith("__grp__:") or not option.id.startswith("__")):
                return option.id
        return self.selected

    def action_session_menu(self) -> None:
        sid = self._menu_target()
        if sid and sid.startswith("__grp__:"):
            self.open_group_menu(sid.split(":", 1)[1])
            return
        if sid is None or sid not in self.sessions:
            self.notify("Pick a session first.", severity="warning")
            return
        self.open_menu(sid)

    def open_menu(self, sid: str) -> None:
        session = self.sessions[sid]
        server = session.from_server
        items: list[MenuItem | None] = [
            MenuItem("open", "Open in", submenu=True),
            None,
            MenuItem("pin", "Unpin" if self.store.grouping.is_pinned(sid) else "Pin", "p"),
            MenuItem("read", "Mark as read", "u", enabled=server and session.unread),
            MenuItem("rename", "Rename", "r", enabled=server),
            None,
            MenuItem("share", "Share", enabled=server),
            MenuItem("copy", "Copy link", "c"),
            None,
            MenuItem("group", "Move to group", submenu=True),
            None,
            MenuItem("archive", "Archive", "a", enabled=server and not session.archived),
            MenuItem("delete", "Delete" if server else "Forget", "d", danger=True),
        ]
        screen = MenuScreen(session.title or session.id, items)
        self.push_screen(screen, lambda choice: self._menu_choice(sid, choice))

    def _menu_choice(self, sid: str, choice: str | None) -> None:
        session = self.sessions.get(sid)
        if choice is None or session is None:
            return
        title = session.title or session.id
        if choice == "open":
            self._open_in_menu(sid)
        elif choice == "pin":
            grouping = self.store.grouping
            pinned = grouping.toggle_pin(sid)
            self._save_grouping(grouping)
            self.notify("Pinned." if pinned else "Unpinned.")
        elif choice == "group":
            self._group_menu(sid)
        elif choice == "read":
            self.mark_read_task(sid)
        elif choice == "rename":
            form = FormScreen("Rename session", [("title", "Title", session.title)], "Rename")
            self.push_screen(form, lambda values: self._rename(sid, values))
        elif choice == "share":
            self._share(session)
        elif choice == "copy":
            self._copy_link(session)
        elif choice == "archive":
            message = f"“{title}” moves to your archived sessions on claude.ai. ccweb can't bring it back, but the web can."
            self.push_screen(
                ConfirmScreen("Archive this session?", message, "Archive"),
                lambda yes: yes and self.archive_task(sid),
            )
        elif choice == "delete" and not session.from_server:
            self._forget(session)
        elif choice == "delete":
            message = f"“{title}” is removed from claude.ai along with its conversation. This can't be undone."
            self.push_screen(
                ConfirmScreen("Delete this session?", message, "Delete", danger=True),
                lambda yes: yes and self.delete_task(sid),
            )

    def _group_menu(self, sid: str) -> None:
        """Move a session into a group, a new one, or back out."""
        grouping = self.store.grouping
        current = grouping.group_of(sid)
        items: list[MenuItem | None] = [
            MenuItem(f"to:{g.id}", g.name, str(i), note="current" if current is g else "")
            for i, g in enumerate(grouping.groups[:9], 1)
        ]
        if items:
            items.append(None)
        items.append(MenuItem("new", "New group…", str(min(len(grouping.groups) + 1, 9))))
        if current is not None:
            items.append(MenuItem("none", "Remove from group", "0"))
        screen = MenuScreen("Move to group", items)

        def chosen(choice: str | None) -> None:
            if choice == "new":
                form = FormScreen("New group", [("name", "Name", "")], "Create", "The session is moved into it.")
                self.push_screen(form, lambda values: self._new_group(sid, values))
            elif choice == "none":
                self._move(sid, None)
            elif choice and choice.startswith("to:"):
                self._move(sid, choice[3:])

        self.push_screen(screen, chosen)

    def _move(self, sid: str, gid: str | None) -> None:
        grouping = self.store.grouping
        grouping.move(sid, gid)
        self._save_grouping(grouping)
        group = grouping.get(gid) if gid else None
        self.notify(f"Moved to {group.name}." if group else "Moved out of its group.")

    def _new_group(self, sid: str, values: dict | None) -> None:
        name = (values or {}).get("name", "").strip()
        if not name:
            return
        grouping = self.store.grouping
        group = grouping.by_name(name) or grouping.new_group(name)  # a name that exists means that group
        grouping.move(sid, group.id)
        self._save_grouping(grouping)
        self.notify(f"Moved to {group.name}.")

    def open_group_menu(self, gid: str) -> None:
        group = self.store.grouping.get(gid)
        if group is None:
            return
        items: list[MenuItem | None] = [
            MenuItem("toggle", "Expand" if group.collapsed else "Collapse", "c"),
            MenuItem("rename", "Rename group", "r"),
            None,
            MenuItem("delete", "Delete group", "d", danger=True),
        ]

        def chosen(choice: str | None) -> None:
            if choice == "toggle":
                self._toggle_group(gid)
            elif choice == "rename":
                form = FormScreen("Rename group", [("name", "Name", group.name)], "Rename")
                self.push_screen(form, lambda values: self._rename_group(gid, values))
            elif choice == "delete":
                members = sum(1 for sid in group.members if sid in self.sessions)
                message = f"“{group.name}” is removed and its {members} sessions go back to Ungrouped. The sessions themselves aren't touched."
                self.push_screen(
                    ConfirmScreen("Delete this group?", message, "Delete group", danger=True),
                    lambda yes: yes and self._delete_group(gid),
                )

        self.push_screen(MenuScreen(group.name, items), chosen)

    def _rename_group(self, gid: str, values: dict | None) -> None:
        name = (values or {}).get("name", "").strip()
        grouping = self.store.grouping
        existing = grouping.by_name(name)
        if not name or (existing is not None and existing.id != gid):
            if name:
                self.notify(f"There's already a group called {name}.", severity="warning")
            return
        grouping.rename(gid, name)
        self._save_grouping(grouping)

    def _delete_group(self, gid: str) -> None:
        grouping = self.store.grouping
        grouping.delete(gid)
        self._save_grouping(grouping)
        self.notify("Group deleted.")

    def _open_in_menu(self, sid: str) -> None:
        session = self.sessions[sid]
        items: list[MenuItem | None] = [
            MenuItem("web", "claude.ai in the browser", "o"),
            MenuItem("teleport", "Terminal (claude --teleport)", "t"),
        ]
        prs = [s.pr for s in relevant(self.branches.get(sid, [])) if s.pr]
        if prs:
            items.append(None)
            items += [MenuItem(f"pr:{pr.url}", f"GitHub {pr.label}", enabled=bool(pr.url)) for pr in prs]
        screen = MenuScreen("Open in", items)

        def chosen(choice: str | None) -> None:
            if choice == "web":
                self._open_web(session)
            elif choice == "teleport":
                self._teleport(session)
            elif choice and choice.startswith("pr:"):
                self.open_link(choice[3:])

        self.push_screen(screen, chosen)

    def _rename(self, sid: str, values: dict | None) -> None:
        title = (values or {}).get("title", "").strip()
        session = self.sessions.get(sid)
        if not title or session is None or title == session.title:
            return
        self.rename_task(sid, title)

    def _share(self, session: Session) -> None:
        screen = ShareScreen(session)

        def done(choice: str | None) -> None:
            if choice == "copy":
                self._copy_link(session)
            elif choice == "open":
                self._open_web(session)

        self.push_screen(screen, done)
        self.load_share_status(session.id, screen)

    @background(group="share")
    async def load_share_status(self, sid: str, screen: ShareScreen) -> None:
        try:
            status, error = await self.api.share_status(sid), ""
        except (ApiError, AuthError) as exc:
            status, error = None, str(exc)
        if screen.is_attached:
            screen.show(status, error)

    def _after_change(self) -> None:
        self._render_list()
        self._render_attention()
        self._render_header()

    @background(group="action")
    async def mark_read_task(self, sid: str) -> None:
        try:
            await self.api.mark_read(sid)
        except (ApiError, AuthError) as exc:
            self.notify(f"Couldn't mark it as read: {exc}", severity="error", timeout=10)
            return
        if sid in self.sessions:
            self.sessions[sid].unread = False
        self.notify("Marked as read.")
        self._after_change()
        self.refresh_sessions()

    @background(group="action")
    async def rename_task(self, sid: str, title: str) -> None:
        try:
            await self.api.rename_session(sid, title)
        except (ApiError, AuthError) as exc:
            self.notify(f"Couldn't rename it: {exc}", severity="error", timeout=10)
            return
        if sid in self.sessions:
            self.sessions[sid].title = title
        self.notify("Renamed.")
        self._after_change()
        self.refresh_sessions()

    @background(group="action")
    async def archive_task(self, sid: str) -> None:
        try:
            await self.api.archive_session(sid)
        except (ApiError, AuthError) as exc:
            self.notify(f"Couldn't archive it: {exc}", severity="error", timeout=10)
            return
        if sid in self.sessions:
            self.sessions[sid].archived = True
        if self.selected == sid and not self.show_archived:
            self._select(None)
        self.notify("Archived. Press v to show archived sessions.")
        self._after_change()
        self.refresh_sessions()

    @background(group="action")
    async def delete_task(self, sid: str) -> None:
        try:
            await self.api.delete_session(sid)
        except (ApiError, AuthError) as exc:
            self.notify(f"Couldn't delete it: {exc}", severity="error", timeout=10)
            return
        for table in (self.sessions, self.older, self.branches):
            table.pop(sid, None)
        self.store.remove_known(sid)
        grouping = self.store.grouping
        grouping.forget(sid)
        self.store.save_grouping(grouping)
        if self.selected == sid:
            self._select(None)
        self.notify("Deleted.")
        self._after_change()
        self.refresh_sessions()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Right-click a session, in the sidebar, on the home list or anywhere in its conversation, for its menu."""
        if event.button != 3 or len(self.screen_stack) > 1:
            return
        try:
            widget, region = self.screen.get_widget_at(event.screen_x, event.screen_y)
        except Exception:  # nothing under the pointer
            return
        sid: str | None = None
        if isinstance(widget, OptionList) and widget.id in {"sessions", "attention"}:
            line = event.screen_y - region.y + widget.scroll_offset.y
            lines = getattr(widget, "_lines", None)  # rows can be several lines tall: map the line to its option
            index = lines[line][0] if lines and 0 <= line < len(lines) else line
            if 0 <= index < widget.option_count:
                option = widget.get_option_at_index(index)
                if option.id and option.id.startswith("__grp__:") and widget.id == "sessions":
                    event.stop()
                    self.open_group_menu(option.id.split(":", 1)[1])
                    return
                if option.id and not option.id.startswith("__"):
                    sid = option.id
        elif self.selected and any(a.id == "convo" for a in widget.ancestors_with_self):
            sid = self.selected
        if sid in self.sessions:
            event.stop()
            self.open_menu(sid)

    # ------------------------------------------------------------------ side panels

    def action_toggle_changes(self) -> None:
        self.toggle_pane("changes")

    def action_toggle_files(self) -> None:
        self.toggle_pane("files")

    def action_toggle_plan(self) -> None:
        self.toggle_pane("plan")

    def action_toggle_side_max(self) -> None:
        if any(self.side_open.values()):
            self.side_max = not self.side_max
            self._apply_side()

    def toggle_pane(self, name: str) -> None:
        if self.selected is None:
            self.notify("Open a session first.", severity="warning")
            return
        self.side_open[name] = not self.side_open[name]
        self._apply_side()
        if self.side_open[name]:
            self._load_pane(name, force=True)

    def close_pane(self, name: str) -> None:
        self.side_open[name] = False
        self._apply_side()

    def _apply_side(self) -> None:
        opened = any(self.side_open.values())
        if not opened:
            self.side_max = False
        side = self.main.query_one("#side")
        side.display = opened
        side.set_class(self.side_max, "max")
        self.main.query_one("#convo-main").display = not (opened and self.side_max)
        for name, is_open in self.side_open.items():
            self.main.query_one(f"#pane-{name}").display = is_open
        for chip in self.main.query("#header-row Chip").results(Chip):
            if chip.what.startswith("toggle:"):
                chip.set_class(self.side_open[chip.what.split(":", 1)[1]], "on")

    def _targets(self) -> list[Target]:
        session = self.sessions.get(self.selected or "")
        if session is None:
            return []
        return targets_for(session.sources, self.branches.get(session.id, []))

    def _target(self) -> Target | None:
        targets = self._targets()
        if not targets:
            return None
        index = self.target_idx.get(self.selected or "", 0)
        return targets[min(index, len(targets) - 1)]

    def _reload_panes(self) -> None:
        for name, is_open in self.side_open.items():
            if is_open:
                self._load_pane(name)

    def _load_pane(self, name: str, force: bool = False) -> None:
        if name == "plan":
            self._render_plan()
        elif name == "changes":
            self.load_changes(force)
        else:
            self.load_files(force)

    def _render_plan(self) -> None:
        done, total = self.board.counts
        label = f"✓ {done}/{total}" if total else "✓"
        for chip in self.main.query("#header-row Chip").results(Chip):
            if chip.what == "toggle:plan":
                chip.label = label
        self.main.query_one("#pane-plan", PlanPane).show(self.board)

    def pick_target(self) -> None:
        targets = self._targets()
        if len(targets) < 2:
            self.notify("This session has one branch to compare." if targets else "No pushed branch or PR yet.", timeout=3)
            return
        options = [(str(i), t.label, f"PR #{t.pr.number}" if t.pr else "") for i, t in enumerate(targets)]
        current = str(min(self.target_idx.get(self.selected or "", 0), len(targets) - 1))

        def chosen(value: str | None) -> None:
            if value is None or self.selected is None:
                return
            self.target_idx[self.selected] = int(value)
            self.changes_commit = None
            self._pane_sig = {}
            self._reload_panes()

        self.push_screen(ChoiceScreen("Compare", options, current), chosen)

    def _no_target_message(self) -> str:
        session = self.sessions.get(self.selected or "")
        if session is not None and not session.from_server:
            return "This session isn't loaded from claude.ai, so its branches aren't known."
        return "No pushed branch or pull request for this session yet."

    @background(exclusive=True, group="changes")
    async def load_changes(self, force: bool = False) -> None:
        sid = self.selected
        pane = self.main.query_one("#pane-changes", ChangesPane)
        target = self._target()
        if target is None:
            pane.show_message(self._no_target_message())
            return
        if not prinfo.gh_available():
            pane.show_message("Changes need the gh CLI: install it and run `gh auth login`.")
            return
        cached = self.cmp_cache.get(target.key)
        if cached is None or force or time.monotonic() - cached[0] > 45:
            if cached is None:
                pane.show_message("Loading the changes…")
            comparison = await asyncio.to_thread(gh_compare, target.repo, target.base, target.head)
            self.cmp_cache[target.key] = (time.monotonic(), comparison)
        else:
            comparison = cached[1]
        if self.selected != sid or self._target() is None or self._target().key != target.key:
            return
        signature = (target.key, tuple((c.sha for c in comparison.commits)), tuple((f.path, f.additions, f.deletions) for f in comparison.files), comparison.error, self.changes_commit)
        if self._pane_sig.get("changes") == signature and not force:
            return  # nothing new: leave the reader's place in the tree and diff alone
        self._pane_sig["changes"] = signature
        pane.show_comparison(target, comparison, keep_commit=self.changes_commit)
        if self.changes_commit:
            await self._show_commit(target, self.changes_commit)

    async def _show_commit(self, target: Target, sha: str) -> None:
        pane = self.main.query_one("#pane-changes", ChangesPane)
        key = (target.repo.lower(), sha)
        if key not in self.commit_cache:
            files, error = await asyncio.to_thread(gh_commit_files, target.repo, sha)
            if error:
                pane.show_message(f"Couldn't read that commit: {error}")
                return
            self.commit_cache[key] = files
        pane.show_files(self.commit_cache[key])

    def pick_commit(self, sha: str | None) -> None:
        """A commit chosen in the Changes panel: show just its files, or all changes again."""
        self.changes_commit = sha
        target = self._target()
        if target is None:
            return
        if sha is None:
            cached = self.cmp_cache.get(target.key)
            if cached:
                self.main.query_one("#pane-changes", ChangesPane).show_files(cached[1].files)
            return
        self._commit_task(target, sha)

    @background(exclusive=True, group="commit")
    async def _commit_task(self, target: Target, sha: str) -> None:
        await self._show_commit(target, sha)

    @background(exclusive=True, group="files")
    async def load_files(self, force: bool = False) -> None:
        sid = self.selected
        pane = self.main.query_one("#pane-files", FilesPane)
        target = self._target()
        if target is None:
            pane.show_message(self._no_target_message())
            return
        if not prinfo.gh_available():
            pane.show_message("Files need the gh CLI: install it and run `gh auth login`.")
            return
        key = (target.repo.lower(), target.head)
        cached = self.tree_cache.get(key)
        if cached and not force and time.monotonic() - cached[0] < 300:
            if self._pane_sig.get("files") == key:
                return
            paths, truncated, error = cached[1], cached[2], cached[3]
        else:
            pane.show_message("Loading the files…")
            paths, truncated, error = await asyncio.to_thread(gh_tree, target.repo, target.head)
            self.tree_cache[key] = (time.monotonic(), paths, truncated, error)
        if self.selected != sid:
            return
        self._pane_sig["files"] = key
        pane.show_paths(target, paths, truncated, error)

    def open_file(self, path: str) -> None:
        target = self._target()
        if target is not None:
            self._open_file_task(target, path)

    @background(exclusive=True, group="file")
    async def _open_file_task(self, target: Target, path: str) -> None:
        pane = self.main.query_one("#pane-files", FilesPane)
        pane.show_file(path, "", "loading…")
        text, error = await asyncio.to_thread(gh_file, target.repo, path, target.head)
        pane.show_file(path, text, error, url=f"https://github.com/{target.repo}/blob/{target.head}/{path}")

    def action_pull_request(self) -> None:
        session = self._current()
        if session is None:
            return
        prs = [s.pr for s in relevant(self.branches.get(session.id, [])) if s.pr]
        if not prs:
            self.notify("No pull request found for this session.", severity="warning")
            return
        if len(prs) == 1:
            self.push_screen(PullRequestScreen(prs[0], self.details.get(prs[0].url)))
            return
        options = [(pr.url, f"{pr.label} {pr.state}", pr.title) for pr in prs]

        def chosen(url: str | None) -> None:
            pr = next((p for p in prs if p.url == url), None)
            if pr is not None:
                self.push_screen(PullRequestScreen(pr, self.details.get(pr.url)))

        self.push_screen(ChoiceScreen("Which pull request?", options), chosen)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh(self) -> None:
        self.refresh_sessions()
        self.load_environments()
        self.refresh_events()
