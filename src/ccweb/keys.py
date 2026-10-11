"""The app's shortcuts, in one table that drives the bindings, the footer, the help screen and the ctrl+p list.

Every shortcut needs ctrl, so a text box can keep focus and nothing typed in it is taken for a command.
The keys left out of the ctrl+letter scheme are the ones a terminal or a text box already owns:
ctrl+a/e/d/k/u/w are editing keys in the boxes, ctrl+x/c/v are cut, copy and paste, and i/m/h/j arrive as
Tab, Enter, Backspace and a newline. ctrl+b is tmux's prefix. There are more actions than free keys, so the
rest are reached through ctrl+p, which lists every action.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from textual.command import DiscoveryHit, Hit, Hits, Provider


@dataclass(frozen=True)
class Shortcut:
    action: str  # the app's action_<name>
    label: str  # as listed in the help screen and ctrl+p
    key: str = ""  # a Textual key name; empty when ctrl+p is the only way in
    footer: str = ""  # a short label to show in the footer; empty to keep it off the footer
    priority: bool = False


SHORTCUTS = (
    Shortcut("new_session", "Start a new session", "ctrl+n", "New"),
    Shortcut("session_menu", "Session menu: rename, pin, open in…", "ctrl+o", "Menu"),
    Shortcut("toggle_changes", "Changes panel", "ctrl+g", "Changes"),
    Shortcut("toggle_files", "Files panel", "ctrl+f", "Files"),
    Shortcut("toggle_plan", "Plan panel", "ctrl+l", "Plan"),
    Shortcut("teleport", "Teleport into the local checkout", "ctrl+t", "Teleport"),
    Shortcut("copy_link", "Copy link", "ctrl+y", "Copy link"),
    Shortcut("search", "Search sessions", "ctrl+s", "Search"),
    Shortcut("refresh", "Refresh now", "ctrl+r"),
    Shortcut("help", "Help", "f1", "Help"),
    Shortcut("quit", "Quit", "ctrl+q", "Quit", priority=True),
    # ctrl+p only
    Shortcut("open_web", "Open on claude.ai"),
    Shortcut("add_session", "Add a session by link or ID"),
    Shortcut("pull_request", "Pull request summary and CI checks"),
    Shortcut("toggle_side_max", "Expand or restore the side panel"),
    Shortcut("toggle_archived", "Show or hide archived sessions"),
    Shortcut("toggle_groups", "Show groups or one flat list"),
    Shortcut("set_checkout", "Set the local checkout for teleport"),
    Shortcut("forget", "Forget a session added here"),
)


LISTED = tuple(s for s in SHORTCUTS if s.action != "quit")  # Textual's own commands already include Quit


def key_label(action: str) -> str:
    """How to name an action's key in a message, e.g. 'ctrl+t'; empty when it only has a ctrl+p entry."""
    return next((s.key for s in SHORTCUTS if s.action == action), "")


class ActionCommands(Provider):
    """Every action in the ctrl+p list, with its key beside it, so the ones without a key of their own are
    still one search away."""

    def _run(self, shortcut: Shortcut):
        return functools.partial(self.app.run_action, shortcut.action)

    async def discover(self) -> Hits:
        for shortcut in LISTED:
            yield DiscoveryHit(shortcut.label, self._run(shortcut), help=shortcut.key or None)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for shortcut in LISTED:
            score = matcher.match(shortcut.label)
            if score > 0:
                yield Hit(score, matcher.highlight(shortcut.label), self._run(shortcut), help=shortcut.key or None)
