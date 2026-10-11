"""Local state: sessions added by hand or created here, and checkout paths for teleport.

Stored as JSON with owner-only permissions. Holds no credentials and no
conversation content.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .config import data_dir


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "state.json"
        self.data: dict = {"version": 1, "known": {}, "checkouts": {}, "prefs": {}, "ui": {}, "groups": None}
        self.load()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            self.data["known"] = dict(loaded.get("known") or {})
            self.data["checkouts"] = dict(loaded.get("checkouts") or {})
            self.data["prefs"] = dict(loaded.get("prefs") or {})
            self.data["ui"] = dict(loaded.get("ui") or {})
            self.data["groups"] = loaded.get("groups") if isinstance(loaded.get("groups"), dict) else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @property
    def known(self) -> dict[str, dict]:
        return self.data["known"]

    def add_known(self, session_id: str, title: str = "", source: str = "added") -> None:
        entry = self.known.setdefault(session_id, {"added_at": time.time(), "source": source})
        if title and not entry.get("title"):
            entry["title"] = title
        self.save()

    def remove_known(self, session_id: str) -> None:
        if self.known.pop(session_id, None) is not None:
            self.save()

    def checkout_for(self, key: str) -> str:
        return self.data["checkouts"].get(key, "")

    def set_checkout(self, key: str, path: str) -> None:
        self.data["checkouts"][key] = path
        self.save()

    @property
    def prefs(self) -> dict:
        return self.data["prefs"]

    def set_prefs(self, **values) -> None:
        self.data["prefs"].update(values)
        self.save()

    @property
    def ui(self) -> dict:
        """Layout choices, such as how wide the side panel is."""
        return self.data["ui"]

    def set_ui(self, **values) -> None:
        self.data["ui"].update(values)
        self.save()

    @property
    def grouping(self):
        """Sidebar groups and pins kept on this machine (see groups.py). A fresh copy each time:
        change it, then pass it to save_grouping."""
        from .groups import Grouping

        return Grouping.from_json(self.data["groups"])

    def save_grouping(self, grouping) -> None:
        self.data["groups"] = grouping.to_json()
        self.save()
