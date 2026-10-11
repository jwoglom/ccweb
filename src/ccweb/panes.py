"""The side panels: Plan, Files and Changes, as on the web.

Each is plain selectable text and trees, so drag-select and Ctrl+C work, and long diffs scroll
rather than wrap. They only draw; the app decides what to load and when (see CloudSessionsApp).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static, Tree
from textual.widgets.option_list import Option

from .links import link_style
from .palette import AMBER, BRICK, CLAY, HUNK, MUTED, SAGE, SAND
from .repoinfo import ChangedFile, Comparison, Commit, Target
from .tasks import Task, TaskBoard
from .widgets import Chip

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MAX_VIEW_LINES = 3000
COMMIT_WIDTH = 34  # the commit list's width inside the Changes panel, less its scrollbar
STATUS_MARK = {"added": ("A", SAGE), "removed": ("D", BRICK), "renamed": ("R", AMBER), "modified": ("M", MUTED)}


# --------------------------------------------------------------------------- rendering


def render_patch(file: ChangedFile) -> Text:
    """One file's diff: a header, then each line with its new-file line number beside it."""
    out = Text(no_wrap=True)
    mark, colour = STATUS_MARK.get(file.status, ("M", MUTED))
    out.append(f"{file.path}", style=f"bold {SAND}")
    out.append(f"  {mark}  ", style=colour)
    out.append(f"+{file.additions}", style=SAGE)
    out.append(f" −{file.deletions}\n", style=BRICK)
    if not file.patch:
        out.append("\nNo text diff to show: a binary file, or too large for GitHub to include.\n", style=MUTED)
        return out
    new = 0
    for line in file.patch.splitlines():
        hunk = HUNK_RE.match(line)
        if hunk:
            new = int(hunk.group(2))
            out.append(f"\n{line}\n", style=HUNK)
        elif line.startswith("+"):
            out.append(f"{new:>5} ", style=MUTED)
            out.append(f"{line}\n", style=f"{SAGE} on #1e2b21")
            new += 1
        elif line.startswith("-"):
            out.append("      ", style=MUTED)
            out.append(f"{line}\n", style=f"{BRICK} on #2d1f1d")
        elif line.startswith("\\"):
            out.append(f"      {line}\n", style=MUTED)
        else:
            out.append(f"{new:>5} ", style=MUTED)
            out.append(f"{line}\n", style=SAND)
            new += 1
    return out


def render_file(path: str, text: str, error: str = "", url: str = "") -> Text:
    """A file's contents with line numbers."""
    out = Text(no_wrap=True)
    out.append(path, style=link_style(url, f"bold {SAND}") if url else f"bold {SAND}")
    out.append("\n")
    if error:
        out.append(f"\nCan't show this file: {error}\n", style=MUTED)
        return out
    lines = text.splitlines()
    width = len(str(min(len(lines), MAX_VIEW_LINES))) or 1
    for number, line in enumerate(lines[:MAX_VIEW_LINES], 1):
        out.append(f"\n{number:>{width}}  ", style=MUTED)
        out.append(line.expandtabs(4), style=SAND)
    if len(lines) > MAX_VIEW_LINES:
        out.append(f"\n\n… showing the first {MAX_VIEW_LINES} of {len(lines)} lines", style=MUTED)
    return out


def ago(moment: datetime | None) -> str:
    if moment is None:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600), (86400 * 30, "d", 86400)):
        if seconds < limit:
            return f"{seconds // size}{unit} ago"
    return moment.astimezone().strftime("%b %-d")


# --------------------------------------------------------------------------- trees


def build_tree(tree: Tree, entries: list[tuple[str, Any, Text | None]], expand: bool) -> None:
    """Fill a Tree from (path, data, suffix) entries: folders first, single-child folder chains joined."""
    root: dict = {}
    for path, data, suffix in entries:
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (data, suffix, path)

    def add(parent, children: dict) -> None:
        folders = sorted(k for k, v in children.items() if isinstance(v, dict))
        files = sorted(k for k, v in children.items() if isinstance(v, tuple))
        for name in folders:
            sub, label = children[name], name
            while len(sub) == 1 and isinstance(next(iter(sub.values())), dict):  # a/b/c as one row
                key = next(iter(sub))
                label, sub = f"{label}/{key}", sub[key]
            node = parent.add(Text(label, style=SAND), expand=expand)
            add(node, sub)
        for name in files:
            data, suffix, path = children[name]
            label = Text(name, style=SAND)
            if suffix is not None:
                label.append("  ")
                label.append_text(suffix)
            parent.add_leaf(label, data=data if data is not None else path)

    tree.clear()
    add(tree.root, root)
    tree.root.expand()


def counts_suffix(file: ChangedFile) -> Text:
    out = Text()
    out.append(f"+{file.additions}", style=SAGE)
    out.append(f" −{file.deletions}", style=BRICK)
    return out


# --------------------------------------------------------------------------- panes


def set_chip_label(chip: Chip, text: str) -> None:
    """Change a chip's label and size it to match. A button's automatic width isn't re-measured when its
    label changes after it first appeared, which left these showing only their first letter."""
    chip.label = text
    chip.styles.width = cell_len(text) + 2  # the label plus a cell of padding either side


class PaneHead(Horizontal):
    def __init__(self, title: str, name: str, *extra: Chip):
        super().__init__(classes="pane-head")
        self._title, self._name, self._extra = title, name, extra

    def compose(self) -> ComposeResult:
        yield Static(self._title, classes="pane-title")
        yield from self._extra
        yield Chip("⤢", "side-max", tip="Expand or restore the panel", classes="plain")
        yield Chip("×", f"pane-close:{self._name}", tip="Close", classes="plain")


class PlanPane(Vertical):
    def compose(self) -> ComposeResult:
        yield PaneHead("Plan", "plan")
        with VerticalScroll(id="plan-scroll"):
            yield Static("", id="plan-body")

    def show(self, board: TaskBoard | None) -> None:
        body = self.query_one("#plan-body", Static)
        tasks = board.ordered if board else []
        if not tasks:
            body.update(Text("No tasks yet. Claude lists its plan here when it makes one.", style=MUTED))
            return
        done, total = board.counts
        text = Text()
        text.append(f"TASKS  {done}/{total}\n\n", style=f"bold {MUTED}")
        for task in tasks:
            text.append_text(self._line(task))
        body.update(text)

    @staticmethod
    def _line(task: Task) -> Text:
        line = Text()
        if task.status == "completed":
            line.append("✓ ", style=SAGE)
            line.append(task.subject, style="strike #7d776e")
        elif task.status == "in_progress":
            line.append("◐ ", style=CLAY)
            line.append(task.active_form or task.subject, style=f"bold {SAND}")
        else:
            line.append("○ ", style=MUTED)
            line.append(task.subject, style=SAND)
        line.append("\n")
        if task.status == "in_progress" and task.description:
            line.append(f"    {task.description}\n", style=MUTED)
        return line


class ChangesPane(Vertical):
    """The branch's changed files and commits on the left, the selected file's diff on the right."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.files: dict[str, ChangedFile] = {}
        self.commits: list[Commit] = []

    def compose(self) -> ComposeResult:
        yield PaneHead("Changes", "changes", Chip("", "target:changes", tip="Which branch to compare", classes="plain"), Chip("PR", "pr", tip="PR summary and CI checks (D)", classes="plain"))
        with Horizontal(id="changes-body"):
            with Vertical(id="changes-left"):
                yield Tree("changes", id="changes-tree")
                yield Static("", id="commits-label")
                yield OptionList(id="changes-commits")
            with ScrollableContainer(id="changes-diff-scroll"):
                yield Static("", id="changes-diff")

    def target_chip(self) -> Chip:
        return next(c for c in self.query(Chip) if c.what == "target:changes")

    def show_message(self, text: str) -> None:
        self.files, self.commits = {}, []
        self.query_one("#changes-tree", Tree).clear()
        self.query_one("#changes-commits", OptionList).clear_options()
        self.query_one("#commits-label", Static).update("")
        self.query_one("#changes-diff", Static).update(Text(text, style=MUTED))

    def show_comparison(self, target: Target, comparison: Comparison, keep_commit: str | None = None) -> None:
        set_chip_label(self.target_chip(), f"{target.base} → {target.head} ▾")
        if comparison.error:
            self.show_message(f"Couldn't read the changes: {comparison.error}")
            return
        self.commits = comparison.commits
        self.query_one("#commits-label", Static).update(Text(f"Commits  {len(comparison.commits)}", style=f"bold {MUTED}"))
        options = self.query_one("#changes-commits", OptionList)
        options.clear_options()
        rows = [Option(Text("All changes", style=f"bold {SAND}"), id="__all__")]
        for commit in reversed(comparison.commits):  # newest first, as the web lists them
            title = Text(commit.message or commit.sha[:7], style=SAND, no_wrap=True)
            detail = Text(f"{commit.sha[:7]} · {commit.author} · {ago(commit.date)}", style=MUTED, no_wrap=True)
            for line in (title, detail):  # the list wraps long rows itself, so cut them with an ellipsis
                line.truncate(COMMIT_WIDTH, overflow="ellipsis")
            rows.append(Option(Text.assemble(title, "\n", detail), id=commit.sha))
        options.add_options(rows)
        options.highlighted = next((i for i, o in enumerate(rows) if o.id == (keep_commit or "__all__")), 0)
        self.show_files(comparison.files)

    def show_files(self, files: list[ChangedFile]) -> None:
        self.files = {f.path: f for f in files}
        tree = self.query_one("#changes-tree", Tree)
        tree.show_root = False
        build_tree(tree, [(f.path, f.path, counts_suffix(f)) for f in files], expand=True)
        diff = self.query_one("#changes-diff", Static)
        if not files:
            diff.update(Text("No changes yet.", style=MUTED))
        else:
            diff.update(Text(f"{len(files)} files changed. Pick one on the left.", style=MUTED))
            first = next(iter(self.files))
            self.show_diff(first)

    def show_diff(self, path: str) -> None:
        file = self.files.get(path)
        if file is not None:
            self.query_one("#changes-diff", Static).update(render_patch(file))
            self.query_one("#changes-diff-scroll", ScrollableContainer).scroll_home(animate=False)

    @on(Tree.NodeHighlighted, "#changes-tree")
    def _highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.is_root or event.node.allow_expand:
            return
        self.show_diff(str(event.node.data))

    @on(OptionList.OptionSelected, "#changes-commits")
    def _commit(self, event: OptionList.OptionSelected) -> None:
        sha = event.option.id
        self.app.pick_commit(None if sha == "__all__" else sha)  # type: ignore[attr-defined]


class FilesPane(Vertical):
    """The repository's files at the session's branch, with search, and the open file beside them."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.paths: list[str] = []
        self.truncated = False

    def compose(self) -> ComposeResult:
        yield PaneHead("Files", "files", Chip("", "target:files", tip="Which repository and branch", classes="plain"))
        yield Input(placeholder="Search files…", id="files-search")
        with Horizontal(id="files-body"):
            yield Tree("files", id="files-tree")
            with ScrollableContainer(id="files-view-scroll"):
                yield Static(Text("Pick a file in the tree.", style=MUTED), id="files-view")

    def target_chip(self) -> Chip:
        return next(c for c in self.query(Chip) if c.what == "target:files")

    def show_message(self, text: str) -> None:
        self.paths = []
        self.query_one("#files-tree", Tree).clear()
        self.query_one("#files-view", Static).update(Text(text, style=MUTED))

    def show_paths(self, target: Target, paths: list[str], truncated: bool, error: str = "") -> None:
        set_chip_label(self.target_chip(), f"{target.repo.split('/')[-1]} · {target.head} ▾")
        if error:
            self.show_message(f"Couldn't list the files: {error}")
            return
        self.paths, self.truncated = paths, truncated
        self._fill()
        self.query_one("#files-view", Static).update(Text("Pick a file in the tree.", style=MUTED))

    def _fill(self) -> None:
        query = self.query_one("#files-search", Input).value.strip().lower()
        paths = [p for p in self.paths if query in p.lower()][:400] if query else self.paths
        tree = self.query_one("#files-tree", Tree)
        tree.show_root = False
        build_tree(tree, [(p, p, None) for p in paths], expand=bool(query))
        if self.truncated and not query:
            tree.root.add_leaf(Text("Some entries may not be listed. Keep typing to narrow.", style=MUTED), data=None)

    @on(Input.Changed, "#files-search")
    def _search(self) -> None:
        self._fill()

    @on(Tree.NodeSelected, "#files-tree")
    def _open(self, event: Tree.NodeSelected) -> None:
        if event.node.allow_expand or not event.node.data:
            return
        self.app.open_file(str(event.node.data))  # type: ignore[attr-defined]

    def show_file(self, path: str, text: str, error: str = "", url: str = "") -> None:
        self.query_one("#files-view", Static).update(render_file(path, text, error, url))
        self.query_one("#files-view-scroll", ScrollableContainer).scroll_home(animate=False)
