"""PTY proxy shared by both halves of rexagents.

- Wrapping `claude agents`, `on_input` sees every keystroke first and can swallow it.
- Wrapping `claude attach`, statuses from the session's hooks arrive on a fifo and
  are spliced into the output as OSC 7501 reports, only where the EscapeScanner
  says the stream is between escape sequences and characters.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import termios
import time
import tty
from typing import Callable, Optional

from .hook import claude_dir, session_name
from .osc import encode_clear, encode_status
from .store import Listener, SessionStore, state_root
from .vtscan import EscapeScanner

# on_input(data, drain) returns the bytes to forward to the child, or None to swallow them,
# or raises Stop to hang up the child and end the proxy.
# drain() relays the child's pending output until it has been quiet for a moment.
class Stop(Exception):
    pass


InputHook = Callable[[bytes, Callable[[], None]], Optional[bytes]]

_KEY = re.compile(
    rb"\x1b\[200~.*?(?:\x1b\[201~|\Z)"   # a bracketed paste, whole
    rb"|\x1b\[[0-?]*[ -/]*[@-~]"          # CSI: arrows, kitty keys, mouse
    rb"|\x1bO[ -~]"                       # SS3: arrows in application mode
    rb"|\x1b[\xc0-\xff][\x80-\xbf]*"      # Alt + a non-ASCII character
    rb"|\x1b.?"                           # Alt + a key, or Esc
    rb"|[\x00-\x1f\x7f]"                  # a control key such as Enter or Ctrl+C
    rb"|[^\x00-\x1f\x7f]+", re.S)          # a run of text


def split_keys(data: bytes) -> list[bytes]:
    """One read of the terminal as separate keys, so a hook sees Enter even when it
    arrives together with other keys."""
    return [m.group() for m in _KEY.finditer(data)]


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:
            select.select([], [fd], [])


def _copy_winsize(src_fd: int, dst_fd: int) -> None:
    try:
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        pass


def _encode(status: dict, title: str | None) -> bytes | None:
    if status.get("state") == "clear":
        return encode_clear()
    try:
        return encode_status(status.get("state"), kind=status.get("kind"), msg=status.get("msg"), title=status.get("title") or title)
    except ValueError:
        return None


def run_proxy(argv: list[str], *, on_input: InputHook | None = None, listener: Listener | None = None,
              initial: dict | None = None, title: str | None = None,
              poll_title: Callable[[], str | None] | None = None, poll_every: float = 2.0,
              stdin_fd: int = 0, stdout_fd: int = 1) -> int:
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    saved_attrs = None
    if os.isatty(stdin_fd):
        _copy_winsize(stdin_fd, master)
        saved_attrs = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
        signal.signal(signal.SIGWINCH, lambda *_: _copy_winsize(stdin_fd, master))

    scanner = EscapeScanner()
    state = {"pending": None, "last_ts": -1, "reported": False, "child_done": False, "stopped": False,
             "status": None, "title": title, "polled": time.monotonic()}

    def queue(status: dict) -> None:
        ts = status.get("ts", 0)
        if isinstance(ts, int) and ts >= state["last_ts"] and (seq := _encode(status, state["title"])):
            state.update(pending=seq, last_ts=ts, reported=True, status=status)
            state["title"] = status.get("title") or state["title"]

    def refresh_title() -> None:
        """Report the last status again if the session got a new name since."""
        state["polled"] = time.monotonic()
        name = poll_title()
        if name and name != state["title"]:
            state["title"] = name
            if state["status"]:
                queue({**state["status"], "title": name, "ts": state["last_ts"]})
                flush_if_safe()

    def flush_if_safe() -> None:
        if state["pending"] and scanner.safe:
            _write_all(stdout_fd, state["pending"])
            state["pending"] = None

    def relay_output() -> None:
        try:
            data = os.read(master, 65536)
        except OSError:  # EIO once the child side closes
            data = b""
        if not data:
            state["child_done"] = True
            return
        safe_at = scanner.feed(data)
        if state["pending"] and safe_at >= 0:
            data = data[:safe_at] + state["pending"] + data[safe_at:]
            state["pending"] = None
        _write_all(stdout_fd, data)

    def drain(quiet: float = 0.05, limit: float = 0.5) -> None:
        deadline = time.monotonic() + limit
        while not state["child_done"] and time.monotonic() < deadline:
            if not select.select([master], [], [], quiet)[0]:
                return
            relay_output()

    if initial and initial.get("state") != "clear":
        queue(initial)
        flush_if_safe()
    stdin_open = True
    try:
        while not state["child_done"]:
            fds = [master] + ([stdin_fd] if stdin_open else []) + ([listener.fd] if listener else [])
            readable = select.select(fds, [], [], poll_every if poll_title else None)[0]
            if poll_title and time.monotonic() - state["polled"] >= poll_every:
                refresh_title()

            if listener and listener.fd in readable:
                for status in listener.read_statuses():
                    queue(status)
                flush_if_safe()

            if master in readable:
                relay_output()

            if stdin_open and stdin_fd in readable:
                data = os.read(stdin_fd, 65536)
                if not data:
                    stdin_open = False
                    continue
                if on_input is None:
                    _write_all(master, data)
                    continue
                try:
                    # Each key reaches the child before the hook looks at the next one.
                    for key in split_keys(data):
                        if (forward := on_input(key, drain)) is not None:
                            _write_all(master, forward)
                except Stop:
                    state["stopped"] = True
                    break
    finally:
        if state["reported"]:
            _write_all(stdout_fd, encode_clear())
        if saved_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, saved_attrs)
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        os.close(master)

    if state["stopped"]:
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
    code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    return 0 if state["stopped"] else code


def ask(prompt: str, stdin_fd: int = 0, stdout_fd: int = 1) -> bytes:
    """Show `prompt` and return the next key pressed (empty at end of input)."""
    _write_all(stdout_fd, prompt.encode())
    saved = termios.tcgetattr(stdin_fd) if os.isatty(stdin_fd) else None
    try:
        if saved is not None:
            tty.setraw(stdin_fd)
        return os.read(stdin_fd, 16)
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, saved)


def run_attach(session_id: str, title: str, argv: list[str], on_input: InputHook | None = None,
               poll_every: float = 2.0) -> int:
    """Run `argv` (normally `claude attach`) reporting the session's status."""
    store = SessionStore(state_root(), session_id)
    job = str(claude_dir() / "jobs" / session_id[:8])
    with store.listen() as listener:
        code = run_proxy(argv, on_input=on_input, listener=listener, initial=store.load(), title=title,
                         poll_title=lambda: session_name(job, session_id), poll_every=poll_every)
    if code != 0:
        # Rex keeps a pane open only for a command that fails right away, and by the time
        # `claude attach` gives up that window has passed: keep its message on screen.
        ask(f"\r\nrexagents: `{' '.join(argv)}` exited with status {code}; "
            "press any key to close this pane.\r\n")
    return code
