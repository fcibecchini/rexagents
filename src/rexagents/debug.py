"""Opt-in debug log: lines are appended only while `<state dir>/debug.log` exists.

A file rather than an environment variable, because the agent view, the session
panes and the hooks each start in a different environment.
"""

from __future__ import annotations

import os
import time

from .store import state_root


def log(what: str, /, **fields: object) -> None:
    """Never raises: it runs inside hooks and input handlers that must not fail."""
    try:
        path = state_root() / "debug.log"
        if not path.exists():
            return
        detail = " ".join(f"{key}={value!r}" for key, value in fields.items())
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {os.getpid()} {what} {detail}\n")
    except Exception:
        pass
