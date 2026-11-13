"""Map Claude Code hook events to Program Status Protocol states."""

from __future__ import annotations

import json
import os

_RESUMED = frozenset({"PostToolUse", "PostToolUseFailure", "PermissionDenied", "ElicitationResult"})

_NOTIFICATION_KINDS = {
    "permission_prompt": "permission",
    "elicitation_dialog": "question",
    "elicitation_url_dialog": "auth",
    "agent_needs_input": None,
}


def _first_line(text) -> str:
    if not isinstance(text, str):
        return ""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _status(state, msg=None, kind=None) -> dict:
    status = {"state": state}
    if kind:
        status["kind"] = kind
    if msg:
        status["msg"] = msg
    return status


def _describe_tool(name: str, tool_input) -> str:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if desc := _first_line(tool_input.get("description")):
        return desc
    if path := tool_input.get("file_path") or tool_input.get("notebook_path"):
        return f"{name} {os.path.basename(str(path))}"
    if cmd := _first_line(tool_input.get("command")):
        return f"{name}: {cmd}"
    return name


def status_from_hook(payload: dict) -> dict | None:
    """Return {state, kind?, msg?} for a hook payload, or None to ignore it."""
    event = payload.get("hook_event_name")
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")

    if event == "SessionStart":
        return _status("idle")
    if event == "UserPromptSubmit":
        return _status("working", _first_line(payload.get("prompt")))
    if event == "PreToolUse":
        if tool == "AskUserQuestion":
            questions = (tool_input or {}).get("questions") or [{}]
            return _status("blocked", _first_line(questions[0].get("question")), "question")
        return _status("working", _describe_tool(tool, tool_input))
    if event == "PermissionRequest":
        return _status("blocked", f"Allow: {_describe_tool(tool, tool_input)}", "permission")
    if event in _RESUMED:
        return _status("working")
    if event == "Elicitation":
        kind = "auth" if payload.get("mode") == "url" else "question"
        return _status("blocked", _first_line(payload.get("message")), kind)
    if event == "Notification":
        ntype = payload.get("notification_type")
        if ntype not in _NOTIFICATION_KINDS:
            return None
        return _status("blocked", _first_line(payload.get("message")), _NOTIFICATION_KINDS[ntype])
    if event == "Stop":
        if tasks := payload.get("background_tasks"):
            n = len(tasks)
            return _status("working", f"Waiting on {n} background task{'s' if n != 1 else ''}")
        return _status("done", _first_line(payload.get("last_assistant_message")))
    if event == "StopFailure":
        msg = _first_line(payload.get("last_assistant_message")) or _first_line(payload.get("error"))
        return _status("error", msg)
    if event == "SessionEnd":
        return {"state": "clear"}
    return None


_TOOL_EVENTS = frozenset({"PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure",
                          "PermissionDenied"})


def tool_key(payload: dict) -> str | None:
    """Identify the tool call a tool event is about. PermissionRequest carries
    no tool_use_id, so the call is identified by its name and input."""
    if payload.get("hook_event_name") not in _TOOL_EVENTS:
        return None
    return json.dumps([payload.get("tool_name"), payload.get("tool_input")], sort_keys=True)


def next_status(current: dict | None, payload: dict) -> dict | None:
    """The status to publish for this hook, given the current one, or None.

    While blocked on a tool call, events about other tool calls (parallel
    tools) leave the block in place."""
    status = status_from_hook(payload)
    if status is None:
        return None
    key = tool_key(payload)
    if status["state"] == "blocked" and key:
        status["blocker"] = key
    blocker = (current or {}).get("blocker")
    if (current or {}).get("state") == "blocked" and blocker and key and key != blocker \
            and status["state"] != "blocked":
        return None
    return status
