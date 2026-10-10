"""Everything that changes a cloud session goes through the documented claude CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

from .api import extract_session_id


@dataclass
class SendResult:
    ok: bool
    error: str = ""
    url: str = ""


def _last_json_line(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def send_followup(claude_bin: str, session_id: str, message: str) -> SendResult:
    """`claude -p --cloud <id> --output-format json`, message on stdin (documented)."""
    try:
        proc = subprocess.run(
            [claude_bin, "-p", "--cloud", session_id, "--output-format", "json"],
            input=message,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return SendResult(False, f"`{claude_bin}` isn't on your PATH")
    except subprocess.TimeoutExpired:
        return SendResult(False, "the claude CLI didn't finish within 2 minutes")
    data = _last_json_line(proc.stdout)
    if data is not None:
        if data.get("ok"):
            return SendResult(True, url=str(data.get("url") or ""))
        return SendResult(False, str(data.get("error") or "the CLI reported a failure"))
    detail = (proc.stderr or proc.stdout).strip().removeprefix("Error: ")
    return SendResult(False, detail.splitlines()[0] if detail else f"claude exited with {proc.returncode}")


def create_session_selfhosted(claude_bin: str, prompt: str, environment_id: str, cwd: str) -> str:
    """Documented non-interactive create for self-hosted environments; returns the session ID."""
    proc = subprocess.run(
        [claude_bin, "-p", prompt, "--environment", environment_id, "--output-format", "json"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=300,
    )
    data = _last_json_line(proc.stdout)
    if data and data.get("session_id"):
        return str(data["session_id"])
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    raise RuntimeError(detail[0] if detail else f"claude exited with {proc.returncode}")


def run_interactive_capture(argv: list[str], cwd: str | None) -> tuple[int, str]:
    """Run argv on the user's terminal through a pty, mirroring and keeping its output.

    Used for `claude --cloud "<task>"`, which is interactive, so the session ID it
    prints can be picked up afterwards. Falls back to a plain run on Windows.
    """
    if os.name != "posix":
        return subprocess.run(argv, cwd=cwd).returncode, ""

    import fcntl
    import pty
    import select
    import signal
    import termios
    import tty

    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            if cwd:
                os.chdir(cwd)
            os.execvp(argv[0], argv)
        except OSError as exc:
            os.write(2, f"couldn't start {argv[0]}: {exc}\n".encode())
        os._exit(127)

    stdin_fd = sys.__stdin__.fileno()
    stdout_fd = sys.__stdout__.fileno()
    interactive = os.isatty(stdin_fd)
    captured = bytearray()

    def sync_size(*_args):
        try:
            size = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    old_attrs = termios.tcgetattr(stdin_fd) if interactive else None
    old_handler = None
    if interactive:
        sync_size()
        old_handler = signal.signal(signal.SIGWINCH, sync_size)
    try:
        if interactive:
            tty.setraw(stdin_fd)
        watch = [master, stdin_fd] if interactive else [master]
        while True:
            ready, _, _ = select.select(watch, [], [])
            if master in ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # EIO once the child closes its side (Linux)
                    chunk = b""
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(stdout_fd, view):]
                captured += chunk
                if len(captured) > 512_000:
                    del captured[:-256_000]
            if interactive and stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if data:
                    os.write(master, data)
                else:
                    watch.remove(stdin_fd)
    finally:
        if old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_attrs)
        if old_handler is not None:
            signal.signal(signal.SIGWINCH, old_handler)
        os.close(master)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), captured.decode("utf-8", "replace")


def create_session_interactive(claude_bin: str, prompt: str, cwd: str) -> tuple[int, str | None]:
    code, output = run_interactive_capture([claude_bin, "--cloud", prompt], cwd)
    return code, extract_session_id(output)


def teleport(claude_bin: str, session_id: str, cwd: str) -> int:
    return subprocess.run([claude_bin, "--teleport", session_id], cwd=cwd).returncode
