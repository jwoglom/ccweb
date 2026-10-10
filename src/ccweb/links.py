"""Turn text that looks like a link into something clickable.

Links carry two things: a terminal hyperlink (OSC 8), which terminals such as iTerm2
open on Cmd-click, and a click action for the app, which works in terminals that hand clicks to
the app instead of acting on the hyperlink. Either way the URL is only ever opened by a click.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from rich.style import Style
from rich.text import Text

LINK_COLOUR = "#6fb3c9"

URL_RE = re.compile(r"https?://[^\s<>\"'`]*[^\s<>\"'`.,;:!?)\]}]")
# owner/repo#123, where owner looks like a GitHub account name
REF_RE = re.compile(r"(?<![\w/.#-])([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+)#(\d+)\b")
# a bare #123, which is only a link when the session tells us which repository it means
BARE_RE = re.compile(r"(?<![\w&/#`])#(\d{1,6})\b")
CODE_RE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)


@dataclass
class LinkContext:
    """What a session says about where bare references point."""

    default_repo: str = ""  # used for #123 when the session touches exactly one repository
    pr_repos: dict[int, str] = field(default_factory=dict)  # PR number -> repository, for this session's PRs

    def repo_for(self, number: int) -> str:
        return self.pr_repos.get(number) or self.default_repo


def issue_url(repo: str, number: int | str) -> str:
    # GitHub redirects /issues/N to /pull/N when N is a pull request.
    return f"https://github.com/{repo}/issues/{number}"


def link_style(url: str, base: str = "") -> Style:
    safe = url.replace("'", "%27")
    style = Style(link=url, underline=True, color=LINK_COLOUR, meta={"@click": f"app.open_link('{safe}')"})
    return Style.parse(base) + style if base else style


def _spans(text: str, ctx: LinkContext | None) -> list[tuple[int, int, str]]:
    """(start, end, url) for every link-like stretch of text, without overlaps."""
    found: list[tuple[int, int, str]] = []
    for match in URL_RE.finditer(text):
        found.append((match.start(), match.end(), match.group(0)))
    for match in REF_RE.finditer(text):
        found.append((match.start(), match.end(), issue_url(match.group(1), match.group(2))))
    if ctx is not None:
        for match in BARE_RE.finditer(text):
            repo = ctx.repo_for(int(match.group(1)))
            if repo:
                found.append((match.start(), match.end(), issue_url(repo, match.group(1))))
    found.sort()
    out: list[tuple[int, int, str]] = []
    for start, end, url in found:
        if not out or start >= out[-1][1]:
            out.append((start, end, url))
    return out


def linkify_text(text: str, ctx: LinkContext | None = None, style: str = "") -> Text:
    """Plain text as a Rich Text with its links made clickable."""
    out = Text(text, style=style)
    for start, end, url in _spans(text, ctx):
        out.stylize(link_style(url), start, end)
    return out


def linkify_markdown(markdown: str, ctx: LinkContext | None = None) -> str:
    """Turn owner/repo#123 and (where known) #123 into Markdown links, leaving code alone.

    Bare URLs need no help: the Markdown widget links those itself.
    """
    parts = CODE_RE.split(markdown)
    for index in range(0, len(parts), 2):  # even parts are outside code; odd ones are the code itself
        chunk = parts[index]
        # Skip what is already a link target or label.
        protected = [(m.start(), m.end()) for m in re.finditer(r"\[[^\]]*\]\([^)]*\)|https?://\S+|<[^>]+>", chunk)]

        def inside(pos: int) -> bool:
            return any(a <= pos < b for a, b in protected)

        def ref(match: re.Match) -> str:
            if inside(match.start()):
                return match.group(0)
            return f"[{match.group(0)}]({issue_url(match.group(1), match.group(2))})"

        chunk = REF_RE.sub(ref, chunk)
        if ctx is not None:
            protected = [(m.start(), m.end()) for m in re.finditer(r"\[[^\]]*\]\([^)]*\)|https?://\S+|<[^>]+>", chunk)]

            def bare(match: re.Match) -> str:
                repo = ctx.repo_for(int(match.group(1)))
                if not repo or inside(match.start()):
                    return match.group(0)
                return f"[{match.group(0)}]({issue_url(repo, match.group(1))})"

            chunk = BARE_RE.sub(bare, chunk)
        parts[index] = chunk
    return "".join(parts)


def is_openable(url: str) -> bool:
    """Only web and mail links are opened; a relative or file: href in a message is never followed."""
    return url.lower().startswith(("https://", "http://", "mailto:"))


Opener = Callable[[str], None]
