"""The plan: a session's task list, rebuilt from its conversation.

Claude keeps its to-do list with tool calls: TaskCreate (subject, description, activeForm) and
TaskUpdate (taskId, status), or the older TodoWrite that replaces the whole list. The server doesn't
expose the list itself, so it's reconstructed by replaying those calls in order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CREATED_RE = re.compile(r"Task\s+#?(\w+)\s+created", re.IGNORECASE)
STATUSES = {"pending", "in_progress", "completed"}


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    active_form: str = ""
    status: str = "pending"


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def extract_task_ops(payload: Any) -> list[dict]:
    """The task-related calls in a page of events, in order, as plain dicts for TaskBoard.apply."""
    events = payload.get("data") if isinstance(payload, dict) else payload
    ops: list[dict] = []
    for event in events if isinstance(events, list) else []:
        message = event.get("message") if isinstance(event, dict) and isinstance(event.get("message"), dict) else {}
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind, args = block.get("type"), block.get("input") if isinstance(block.get("input"), dict) else {}
            if kind == "tool_use" and block.get("name") == "TaskCreate":
                ops.append(
                    {"op": "create", "use": block.get("id"), "subject": str(args.get("subject") or ""),
                     "description": str(args.get("description") or ""), "active": str(args.get("activeForm") or "")}
                )
            elif kind == "tool_use" and block.get("name") == "TaskUpdate":
                ops.append({"op": "update", "id": str(args.get("taskId")), **{k: args[k] for k in ("status", "subject", "description", "activeForm") if k in args}})
            elif kind == "tool_use" and block.get("name") == "TodoWrite":
                ops.append({"op": "todos", "todos": [t for t in args.get("todos") or [] if isinstance(t, dict)]})
            elif kind == "tool_result":
                match = CREATED_RE.search(_text(block.get("content")))
                if match and block.get("tool_use_id"):
                    ops.append({"op": "created", "use": block["tool_use_id"], "id": match.group(1)})
    return ops


@dataclass
class TaskBoard:
    tasks: dict[str, Task] = field(default_factory=dict)
    _awaiting: dict[str, str] = field(default_factory=dict)  # tool_use id -> the task id it was given for now
    _next: int = 1

    def apply(self, ops: list[dict]) -> None:
        for op in ops:
            kind = op.get("op")
            if kind == "create" and op["subject"]:
                tid = str(self._next)  # tasks are numbered from 1 in order; the call's result confirms it
                self._next += 1
                self.tasks[tid] = Task(tid, op["subject"], op["description"], op["active"])
                self._awaiting[op["use"]] = tid
            elif kind == "created":
                tid = self._awaiting.pop(op["use"], None)
                if tid is not None and tid != op["id"] and tid in self.tasks and op["id"] not in self.tasks:
                    task = self.tasks.pop(tid)
                    task.id = op["id"]
                    self.tasks[task.id] = task
            elif kind == "update":
                task = self.tasks.get(op["id"])
                if task is None:
                    continue
                if op.get("status") == "deleted":
                    del self.tasks[op["id"]]
                    continue
                if op.get("status") in STATUSES:
                    task.status = op["status"]
                for key, attr in (("subject", "subject"), ("description", "description"), ("activeForm", "active_form")):
                    if op.get(key):
                        setattr(task, attr, str(op[key]))
            elif kind == "todos":
                self.tasks = {}
                for i, todo in enumerate(op["todos"], 1):
                    status = todo.get("status") if todo.get("status") in STATUSES else "pending"
                    self.tasks[str(i)] = Task(str(i), str(todo.get("content") or ""), "", str(todo.get("activeForm") or ""), status)
                self._next = len(self.tasks) + 1

    @property
    def ordered(self) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda t: (not t.id.isdigit(), int(t.id) if t.id.isdigit() else 0, t.id))

    @property
    def counts(self) -> tuple[int, int]:
        """(completed, total)"""
        return sum(t.status == "completed" for t in self.tasks.values()), len(self.tasks)
