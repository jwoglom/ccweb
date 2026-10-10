"""Sidebar groups and pins, kept on this machine.

claude.ai stores your groups and pins in a settings document whose endpoint refuses the Claude Code
login ccweb uses, so ccweb keeps its own. They live in ccweb's state file next to your checkout paths
and are not shared with the web: a group made here doesn't appear there, and the reverse.

Only session IDs and group names are stored.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Group:
    id: str
    name: str
    collapsed: bool = False
    members: list[str] = field(default_factory=list)  # session IDs


@dataclass
class Grouping:
    groups: list[Group] = field(default_factory=list)  # in the order they're shown
    pinned: list[str] = field(default_factory=list)  # session IDs, most recently pinned first
    grouped: bool = True  # False shows one flat list, whatever groups exist

    # ---- lookups

    def get(self, gid: str) -> Group | None:
        return next((g for g in self.groups if g.id == gid), None)

    def group_of(self, sid: str) -> Group | None:
        return next((g for g in self.groups if sid in g.members), None)

    def by_name(self, name: str) -> Group | None:
        return next((g for g in self.groups if g.name.casefold() == name.casefold()), None)

    def is_pinned(self, sid: str) -> bool:
        return sid in self.pinned

    # ---- changes

    def new_group(self, name: str) -> Group:
        group = Group(id="g-" + secrets.token_hex(4), name=name.strip())
        self.groups.append(group)
        return group

    def move(self, sid: str, gid: str | None) -> None:
        """Put a session in a group, or back in Ungrouped when gid is None. A session is in one group at most."""
        for group in self.groups:
            if sid in group.members:
                group.members.remove(sid)
        target = self.get(gid) if gid else None
        if target is not None:
            target.members.append(sid)

    def rename(self, gid: str, name: str) -> None:
        group = self.get(gid)
        if group is not None:
            group.name = name.strip()

    def delete(self, gid: str) -> None:
        """Remove a group. Its sessions go back to Ungrouped; the sessions themselves aren't touched."""
        self.groups = [g for g in self.groups if g.id != gid]

    def toggle_pin(self, sid: str) -> bool:
        """Pin or unpin; returns whether it's pinned now."""
        if sid in self.pinned:
            self.pinned.remove(sid)
            return False
        self.pinned.insert(0, sid)
        return True

    def forget(self, sid: str) -> None:
        """A session was deleted: drop it from every group and from the pins."""
        self.move(sid, None)
        if sid in self.pinned:
            self.pinned.remove(sid)

    # ---- storage

    def to_json(self) -> dict:
        return {
            "groups": [{"id": g.id, "name": g.name, "collapsed": g.collapsed, "members": g.members} for g in self.groups],
            "pinned": self.pinned,
            "grouped": self.grouped,
        }

    @classmethod
    def from_json(cls, data: Any) -> Grouping:
        if not isinstance(data, dict):
            return cls()
        groups = [
            Group(
                id=str(g["id"]),
                name=str(g.get("name") or "Untitled"),
                collapsed=bool(g.get("collapsed")),
                members=[m for m in g.get("members", []) if isinstance(m, str)],
            )
            for g in data.get("groups", [])
            if isinstance(g, dict) and isinstance(g.get("id"), str)
        ]
        return cls(groups, [m for m in data.get("pinned", []) if isinstance(m, str)], data.get("grouped") is not False)
