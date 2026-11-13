"""Before attaching a stopped session, fix what would stop `claude attach` waking it.

- Its working directory is gone, typically a removed worktree: recreate it, by default
  as an empty worktree (a sparse checkout of nothing, detached so the branch stays free),
  which is enough to read and continue the conversation. Claude trusts it as part of the
  repo, and git commands in it stay off the main checkout.
- It's a stub: handed off to the background but stopped before its first reply, so it
  has no transcript, while the conversation carries on in the session it came from.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .hook import claude_dir

ENTER = (b"\r", b"\n")
_NOTHING = "/.rexagents-placeholder"  # a sparse-checkout pattern that matches no file


def _projects() -> Path:
    return claude_dir() / "projects"


def project_dir(cwd: str) -> Path:
    return _projects() / re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def transcript(session_id: str) -> Optional[Path]:
    return next(_projects().glob(f"*/{session_id}.jsonl"), None)


def handed_off_from(session_id: str, cwd: str) -> Optional[str]:
    needle = f'"continuedInSessionId":"{session_id}"'.encode()
    home = project_dir(cwd)
    for path in [*sorted(home.glob("*.jsonl")), *sorted(_projects().glob("*/*.jsonl"))]:
        try:
            if needle in path.read_bytes():
                return path.stem
        except OSError:
            continue
    return None


def _git(*args: str, cwd: str) -> str:
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


def _ok(*args: str, cwd: str) -> bool:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True).returncode == 0


@dataclass
class Restore:
    root: str           # the worktree to recreate (cwd may be a directory inside it)
    repo: str
    branch: Optional[str]
    commit: str


def plan_restore(cwd: str, session_id: str) -> Optional[Restore]:
    match = re.match(r"(.*/\.claude/worktrees/[^/]+)", cwd)
    root = match.group(1) if match else cwd
    parent = Path(root).parent
    while not parent.exists():
        parent = parent.parent
    try:
        repo = _git("rev-parse", "--show-toplevel", cwd=str(parent))
    except (subprocess.CalledProcessError, OSError):
        return None
    branch = None
    path = transcript(session_id)
    if path:
        names = re.findall(rb'"gitBranch":"([^"]+)"', path.read_bytes())
        for name in dict.fromkeys(n.decode() for n in reversed(names)):
            if name != "HEAD" and _ok("show-ref", "--verify", "--quiet", f"refs/heads/{name}", cwd=repo):
                branch = name
                break
    return Restore(root, repo, branch, _git("rev-parse", branch or "HEAD", cwd=repo))


def restore(plan: Restore, cwd: str, *, full: bool) -> None:
    add = ["worktree", "add", "--quiet"]
    if full:
        # On the branch if it's free, else detached at the same commit.
        if not (plan.branch and _ok(*add, plan.root, plan.branch, cwd=plan.repo)):
            _git(*add, "-f", "--detach", plan.root, plan.commit, cwd=plan.repo)
    else:
        _git(*add, "-f", "--no-checkout", "--detach", plan.root, plan.commit, cwd=plan.repo)
        _git("sparse-checkout", "set", "--no-cone", _NOTHING, cwd=plan.root)
        _git("checkout", "--quiet", cwd=plan.root)
    os.makedirs(cwd, exist_ok=True)


def _intent(session_id: str) -> str:
    try:
        state = json.loads((claude_dir() / "jobs" / session_id[:8] / "state.json").read_text())
    except (OSError, ValueError):
        return ""
    intent = state.get("intent") if isinstance(state, dict) else None
    return " ".join(intent.split())[:300] if isinstance(intent, str) else ""


Ask = Callable[[str], bytes]
Run = Callable[..., subprocess.CompletedProcess]


def preflight(session_id: str, cwd: str, ask: Ask, sessions: Callable[[], list[dict]],
              run: Run = subprocess.run) -> Optional[str]:
    """The session to attach (another one for a stub), or None to close the pane."""
    if not os.path.isdir(cwd) and (plan := plan_restore(cwd, session_id)):
        what = f"{plan.branch} ({plan.commit[:11]})" if plan.branch else plan.commit[:11]
        key = ask(f"rexagents: this session's directory {cwd} no longer exists.\r\n"
                  f"  Enter  recreate it as an empty worktree at {what}, then open the session\r\n"
                  f"  f      recreate the full worktree, then open the session\r\n"
                  f"  other  close\r\n")
        if key not in (*ENTER, b"f"):
            return None
        try:
            restore(plan, cwd, full=key == b"f")
        except subprocess.CalledProcessError as error:
            print(f"rexagents: couldn't recreate {plan.root}: {error.stderr.strip()}\r")

    if transcript(session_id) or not (original := handed_off_from(session_id, cwd)):
        return session_id
    listed = sessions()
    prompt = _intent(session_id)
    key = ask(f"rexagents: this session was handed off from {original[:8]} before its first reply "
              f"finished, so it has no history; the conversation carries on in {original[:8]}.\r\n"
              + (f"  It was given this prompt, which you can send there again:\r\n  {prompt}\r\n"
                 if prompt else "")
              + f"  Enter  open {original[:8]} here instead, and delete this empty session\r\n"
              f"  other  close\r\n")
    if key not in ENTER:
        return None
    if not any(s.get("sessionId") == original and s.get("id") for s in listed):
        stub = next((s for s in listed if s.get("sessionId") == session_id), {})
        name = stub.get("name") if stub.get("name") not in (None, "", stub.get("id")) else None
        try:
            run(["claude", "--bg", "--resume", original, *(["--name", name] if name else [])],
                cwd=cwd if os.path.isdir(cwd) else None, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as error:
            ask(f"rexagents: couldn't resume {original[:8]}: {(error.stderr or '').strip()}\r\n"
                "press any key to close this pane.\r\n")
            return None
    run(["claude", "rm", session_id[:8]], capture_output=True, text=True)
    return original
