"""Small widgets shared by the app and the side panels."""

from __future__ import annotations

from textual.widgets import Button


class Chip(Button):
    """A compact, focusable setting or action."""

    def __init__(self, label: str, what: str, index: int = -1, tip: str = "", classes: str = ""):
        # A button with an empty label is zero cells wide, and Rich's text wrapping then fails while drawing it.
        super().__init__(label or "\u00a0", compact=True, classes=f"chip {classes}".strip())
        self.what, self.index = what, index
        if tip:
            self.tooltip = tip
