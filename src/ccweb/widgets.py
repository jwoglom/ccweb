"""Small widgets shared by the app and the side panels."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button



class Chip(Button):
    """A compact, focusable setting or action."""

    def __init__(self, label: str, what: str, index: int = -1, tip: str = "", classes: str = ""):
        # A button with an empty label is zero cells wide, and Rich's text wrapping then fails while drawing it.
        super().__init__(label or "\u00a0", compact=True, classes=f"chip {classes}".strip())
        self.what, self.index = what, index
        if tip:
            self.tooltip = tip


class Splitter(Widget):
    """A one-cell handle between two panels. Drag it sideways; it reports where the pointer is and leaves
    the sizing to whoever owns the layout."""

    class Dragged(Message):
        def __init__(self, screen_x: int):
            super().__init__()
            self.screen_x = screen_x

    class Released(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dragging = False
        self.tooltip = "Drag to resize"

    def render(self) -> Text:
        return Text("\n".join("▎" for _ in range(self.size.height)))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self.dragging = True
        self.add_class("dragging")
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.dragging:
            event.stop()
            self.post_message(self.Dragged(event.screen_x))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.dragging:
            event.stop()
            self.dragging = False
            self.remove_class("dragging")
            self.release_mouse()
            self.post_message(self.Released())
