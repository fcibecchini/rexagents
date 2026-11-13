"""Claude Code hook: `rexagents hook` with the payload on stdin.

Kept apart from the rest so each run imports as little as possible; hooks run
synchronously on every tool call of a background session.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from . import debug
from .hookmap import next_status
from .store import SessionStore, state_root

EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "PermissionDenied", "Notification", "Elicitation", "ElicitationResult",
    "Stop", "StopFailure", "SessionEnd",
)
_NOTIFICATIONS = "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"


# Matches both `rexagents hook` and the older `python -m rexagents.hook`.
_OURS = re.compile(r"rexagents'? hook\b|rexagents\.hook\b")


def launcher() -> str | None:
    """The rexagents command for hooks to run: the one on PATH, so that the hooks
    don't depend on where the source lives."""
    found = shutil.which("rexagents")
    if found:
        return found
    arg0 = sys.argv[0]
    return os.path.abspath(arg0) if os.path.basename(arg0) == "rexagents" and os.path.isfile(arg0) else None


def is_ours(command: str) -> bool:
    return bool(_OURS.search(command))


def settings(launcher: str) -> dict:
    """Claude Code settings that run this hook."""
    # CLAUDE_JOB_DIR is only set in background sessions, the only ones that can't
    # report their own status; elsewhere the hook costs just a shell start. If
    # rexagents is uninstalled, the hook quietly does nothing.
    path = shlex.quote(launcher)
    command = f'[ -n "$CLAUDE_JOB_DIR" ] || exit 0; [ -x {path} ] || exit 0; exec {path} hook'
    hooks = {}
    for event in EVENTS:
        group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        if event == "Notification":
            group["matcher"] = _NOTIFICATIONS
        hooks[event] = [group]
    return {"hooks": hooks}


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def user_settings() -> Path:
    return claude_dir() / "settings.json"


def installed(settings_path: Path) -> bool:
    """Whether the settings file already runs this hook."""
    try:
        hooks = json.loads(settings_path.read_text()).get("hooks", {})
        return any(is_ours(handler.get("command", ""))
                   for groups in hooks.values() for group in groups for handler in group.get("hooks", []))
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def session_name(job_dir: str | None, session_id: str) -> str | None:
    """The name Claude gave the session, once it has one (it starts out as the id)."""
    try:
        name = json.loads((Path(job_dir) / "state.json").read_text()).get("name")
    except (TypeError, OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and name and name not in (session_id, session_id[:8]) else None


def main() -> int:
    ts = time.time_ns()
    # Never fail or block Claude: any problem is swallowed, and logged if debugging is on.
    event = None
    try:
        payload = json.load(sys.stdin)
        event = payload.get("hook_event_name")
        store = SessionStore(state_root(), payload["session_id"])
        status = next_status(store.load(), payload)
        if status is not None:
            status["ts"] = ts
            if name := session_name(os.environ.get("CLAUDE_JOB_DIR"), payload["session_id"]):
                status["title"] = name
            store.publish(status)
        debug.log("hook", event=event, state=status and status.get("state"), session=payload["session_id"][:8])
    except Exception as error:
        debug.log("hook error", event=event, error=repr(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
