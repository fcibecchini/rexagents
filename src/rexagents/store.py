"""Per-session status shared between hooks (writers) and attach proxies (readers).

Layout under the state root:

    <session_id>/status.json   last status, replayed when a proxy attaches
    <session_id>/<pid>.fifo    one per attached proxy; hooks write JSON lines
    <session_id>/pane          the Rex block showing the session, if any
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

_SAFE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def state_root() -> Path:
    if root := os.environ.get("REXAGENTS_STATE_DIR"):
        return Path(root)
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "rexagents"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Listener:
    def __init__(self, path: Path):
        self.path = path
        os.mkfifo(path, 0o600)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        # Hold a writer open so the fifo never reports EOF between hook runs.
        self._keepalive = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        self._buf = b""

    def read_statuses(self) -> list[dict]:
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        statuses = []
        for line in lines:
            try:
                status = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(status, dict):
                statuses.append(status)
        return statuses

    def close(self) -> None:
        for fd in (self.fd, self._keepalive):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def session_panes(root: Path) -> dict[str, str]:
    """The Rex blocks showing sessions, each with its session id."""
    panes = {}
    for path in Path(root).glob("*/pane"):
        try:
            block = path.read_text().strip()
        except OSError:
            continue
        if block:
            panes[block] = path.parent.name
    return panes


class SessionStore:
    def __init__(self, root: Path, session_id: str):
        if not session_id or len(session_id) > 64 or not set(session_id) <= _SAFE_ID_CHARS:
            raise ValueError(f"unsafe session id: {session_id!r}")
        self.dir = Path(root) / session_id
        self._status = self.dir / "status.json"
        self._pane = self.dir / "pane"

    def load(self) -> dict | None:
        try:
            return json.loads(self._status.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def pane(self) -> str | None:
        try:
            return self._pane.read_text().strip() or None
        except OSError:
            return None

    def set_pane(self, block_id: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._pane.write_text(block_id)

    def forget_pane(self, block_id: str) -> None:
        if self.pane() == block_id:
            self._pane.unlink(missing_ok=True)

    def publish(self, status: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        current = self.load()
        if current and current.get("ts", 0) > status.get("ts", 0):
            return
        line = json.dumps(status)
        tmp = self.dir / f".status-{os.getpid()}.tmp"
        tmp.write_text(line)
        os.replace(tmp, self._status)
        self._fan_out(line.encode() + b"\n")

    def _fan_out(self, data: bytes) -> None:
        for fifo in self.dir.glob("*.fifo"):
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno == errno.ENXIO and not _pid_alive(int(fifo.stem) if fifo.stem.isdigit() else -1):
                    fifo.unlink(missing_ok=True)
                continue
            try:
                os.write(fd, data)
            except OSError:
                pass
            finally:
                os.close(fd)

    def listen(self) -> Listener:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{os.getpid()}.fifo"
        path.unlink(missing_ok=True)
        return Listener(path)
