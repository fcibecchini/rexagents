"""Reports for the Program Resume Protocol (OSC 7577), which tell the terminal how to
start a program again after the terminal restarts.

See https://www.superlogical.com/rex/docs/build/program-resume
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import time
from pathlib import Path

from .store import state_root

TOKEN_VAR = "OSC7577_TOKEN"
MAX_BYTES = 2048  # the whole sequence; terminals accept at least this much
MAX_ARGS = 64

_TOKEN = re.compile(r"[A-Za-z0-9_-]{22,64}")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


def take_token() -> str | None:
    """The terminal's token, removed from the environment so that the programs this one
    starts don't report in its place."""
    token = os.environ.pop(TOKEN_VAR, None)
    return token if token and _TOKEN.fullmatch(token) else None


def launcher_path() -> Path:
    """What both kinds of pane run as: Rex shows Claude's icon for a program named `claude`, and
    resumes a command naming this path once a trust rule names it too."""
    return state_root() / "claude"


def write_launcher(python: str, src: str) -> Path:
    """The launcher, running rexagents from `src` with `python`, written again when either moved."""
    # Both a shell script, for Rex to start, and the script Python runs, for the pane to run as.
    text = (f"#!/bin/sh\n''''exec {shlex.quote(python)} \"$0\" \"$@\" #'''\n"
            f"import sys\nsys.path.insert(0, {src!r})\nfrom rexagents.__main__ import cli\ncli()\n")
    path = launcher_path()
    try:
        if path.read_text() == text:
            return path
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".claude.{os.getpid()}")
    temp.write_text(text)
    temp.chmod(0o755)
    os.replace(temp, path)
    return path


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode("ascii")


def _reportable(argv: list[str], cwd: str | None) -> bool:
    if not argv or len(argv) > MAX_ARGS or any(_CONTROL.search(a) for a in argv):
        return False
    program = argv[0]
    if not program or program.startswith("-") or ("/" in program and not program.startswith("/")):
        return False
    return cwd is None or (cwd.startswith("/") and not _CONTROL.search(cwd))


class Resume:
    """How to start this program again, for the terminal whose token this is."""

    def __init__(self, token: str, argv: list[str], cwd: str | None = None, pid: int | None = None):
        self.token, self.argv, self.cwd = token, argv, cwd
        self.pid = os.getpid() if pid is None else pid

    def _sequence(self, pairs: str) -> bytes:
        mac = hashlib.sha256(f"{pairs}:{self.token}".encode()).hexdigest()
        return f"\x1b]7577;{pairs}:mac={mac}\x1b\\".encode()

    def report(self, now: float | None = None) -> bytes | None:
        """The report, or None for a command the protocol can't carry."""
        if not _reportable(self.argv, self.cwd):
            return None
        pairs = f"pid={self.pid}:time={int(time.time() if now is None else now)}"
        pairs += ":argv=" + ",".join(_b64(a) for a in self.argv)
        if self.cwd is not None:
            pairs += ":cwd=" + _b64(self.cwd)
        sequence = self._sequence(pairs)
        return sequence if len(sequence) <= MAX_BYTES else None

    def clear(self, now: float | None = None) -> bytes:
        return self._sequence(f"pid={self.pid}:time={int(time.time() if now is None else now)}:clear=1")
