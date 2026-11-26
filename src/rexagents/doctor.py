"""`rexagents doctor`: is everything rexagents relies on in place?"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .hook import is_ours, user_settings
from .resume import launcher_path
from .rexpane import location
from .store import state_root

MIN_CLAUDE = (2, 1, 295)
Result = tuple  # (status, message): status is "ok", "fail" or "info"


def check_rex(env: dict, rex_version: str | None) -> Result:
    if not rex_version:
        return "fail", "rex isn't on PATH. Install Rex: https://www.superlogical.com/rex"
    if location(env):
        return "ok", f"in a Rex pane ({rex_version})"
    return "info", f"{rex_version} found, but this isn't a Rex pane: run rexagents inside Rex"


def resume_rules(launcher: Path) -> list[str]:
    """The names of Rex's resume rules (in init.lua) that trust a command starting with `launcher`."""
    try:
        done = subprocess.run(["rex", "config", "check", "--json"], capture_output=True, text=True, timeout=5)
        rules = json.loads(done.stdout).get("resume_rules") or []
        return [r["name"] for r in rules if (r.get("rule", {}).get("command") or [None])[0] == str(launcher)]
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError, KeyError, TypeError):
        return []


def check_resume(rules: list[str], launcher: Path) -> Result:
    if rules:
        return "ok", f"Rex resumes rexagents after a restart (rules {', '.join(rules)})"
    return "info", f"Rex won't resume rexagents after a restart: add the trust rules from the README, " \
                   f"whose command starts with {launcher}"


def check_claude(version: str | None) -> Result:
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if not found:
        return "fail", "claude isn't on PATH. Install Claude Code: https://code.claude.com"
    have = tuple(int(n) for n in found.groups())
    if have < MIN_CLAUDE:
        return "fail", f"Claude Code {found.group(0)} is too old: rexagents needs {'.'.join(map(str, MIN_CLAUDE))}. " \
                       "Run `claude update`."
    return "ok", f"Claude Code {found.group(0)}"


def check_hooks(path: Path) -> Result:
    fix = "Run `rexagents hooks install`."
    try:
        hooks = json.loads(path.read_text()).get("hooks", {})
        commands = [h.get("command", "") for groups in hooks.values() for g in groups for h in g.get("hooks", [])]
    except (OSError, ValueError, AttributeError, TypeError):
        commands = []
    ours = [c for c in commands if is_ours(c)]
    if not ours:
        return "fail", f"the hooks aren't in {path}, so only sessions started from rexagents report. {fix}"
    if any("rexagents.hook" in c for c in ours):
        return "fail", f"{path} has the old hook command, which breaks if the source moves. {fix}"
    launchers = {shlex.split(m.group(1))[0] for c in ours if (m := re.search(r"exec (.+) hook$", c))}
    missing = sorted(p for p in launchers if not os.access(p, os.X_OK))
    if missing:
        return "fail", f"the hooks run {', '.join(missing)}, which isn't there any more, so they do nothing. {fix}"
    return "ok", f"hooks installed in {path}"


def check_sessions(sessions: list[dict], state_dir: Path) -> Result:
    running = [s for s in sessions if s.get("id") and "pid" in s]
    silent = [s for s in running if not (state_dir / s.get("sessionId", "") / "status.json").exists()]
    if not silent:
        counted = {0: "no sessions are running", 1: "the running session has reported a status"}
        return "ok", counted.get(len(running), f"all {len(running)} running sessions have reported a status")
    names = ", ".join(f"{s.get('name') or s['id']} ({s['id']})" for s in silent)
    return "fail", f"running sessions that have never reported, probably started without the hooks: {names}. " \
                   "To restart one with them: `claude stop <id>`, then open it again."


def check_debug(state_dir: Path) -> Result:
    log = state_dir / "debug.log"
    if log.exists():
        return "info", f"debug log on: {log} (delete it to turn it off)"
    return "info", f"debug log off (create {log} to turn it on)"


def report(results: list[Result]) -> tuple[str, int]:
    marks = {"ok": "✓", "fail": "✗", "info": "·"}
    text = "".join(f"{marks[status]} {message}\n" for status, message in results)
    return text, 1 if any(status == "fail" for status, _ in results) else 0


def _output(*argv: str) -> str | None:
    if not shutil.which(argv[0]):
        return None
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run() -> int:
    rex_version = (_output("rex", "version") or "").splitlines()[:1]
    try:
        sessions = json.loads(_output("claude", "agents", "--json") or "[]")
    except ValueError:
        sessions = []
    text, code = report([
        check_rex(dict(os.environ), rex_version[0] if rex_version else None),
        check_resume(resume_rules(launcher_path()) if rex_version else [], launcher_path()),
        check_claude(_output("claude", "--version")),
        check_hooks(user_settings()),
        check_sessions(sessions, state_root()),
        check_debug(state_root()),
    ])
    print(text, end="")
    return code
