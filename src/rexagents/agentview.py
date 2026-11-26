"""Wrap the stock `claude agents` view so opening a row splits a Rex pane instead.

When Enter or → reaches the wrapper, it reads the agent view's screen from Rex.
If the footer says the key will open a session and exactly one session matches
the highlighted row, the key is swallowed and the session opens in its own pane.
Otherwise the key goes through and the agent view behaves as usual. Alt+N (open the
Nth row) and Ctrl+Enter (start the typed prompt and open it) open panes too.

In a session's pane, ← with an empty prompt (the session offers "← N agents")
closes the pane and focuses the agent view again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple

from . import debug
from .hook import claude_dir, installed, launcher, settings, user_settings
from .proxy import Stop, run_proxy
from .resume import Resume, launcher_path, take_token, write_launcher
from .rexpane import location
from .store import SessionStore, session_panes, state_root

# Enter (legacy and kitty keyboard protocol) and → (normal and application cursor mode).
_OPEN_KEYS = frozenset({b"\r", b"\x1b[13u", b"\x1b[C", b"\x1bOC"})
_BACK_KEYS = frozenset({b"\x1b[D", b"\x1bOD"})
# "return" is the agent view's word for the session just created or last left.
_OPENING_FOOTERS = ("enter to open", "enter to return", "enter to resume")
_BACK_HINT = re.compile(r"← \d+ agents?(?: ·|$)")
_DETACH_KEYS = {b"\x03": "C", b"\x1b[99;5u": "C", b"\x04": "D", b"\x1b[100;5u": "D"}
# A narrow pane cuts the hint short: "Press Ctrl-C again to detach (sess".
_DETACH_HINTS = {letter: re.compile(rf"Press Ctrl-{letter} again") for letter in "CD"}
_DETACH_WINDOW = 1.0  # seconds Claude waits for the second Ctrl+C or Ctrl+D (detaches at 0.5, not at 1)
_TINY_ROWS = 5  # a pane this short can show none of Claude's input box
_START_AND_OPEN = b"\x1b[13;5u"  # Ctrl+Enter
_CREATE_FOOTER = "enter to create"
# The list is in front, whatever is selected: a session, a group name or "… N more".
_LIST_FOOTERS = (*_OPENING_FOOTERS, _CREATE_FOOTER, "enter to collapse", "enter to expand", "enter to show")
_ALT_DIGIT = re.compile(rb"\x1b\[(49|5[0-7]);3u|\x1b([1-9])")
_CLICK = re.compile(rb"\x1b\[<0;\d+;(\d+)([Mm])")  # SGR mouse: left button, no modifiers
_TOKEN = re.compile(rb"\x1b\[([0-9;:]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.?|([^\x1b]+)")
_ROW = re.compile(r"\s*\S (.+?)(?:\s{2,}|\s*$)")
_NOT_SUMMARY = re.compile(r"\d+[smhd]|#\d+|PR #\d+|\d+ PRs?")  # the age, or a pull request badge
# A job waiting for its first prompt, such as a new fork, needs this; its row, when selected,
# may show the hint instead.
_FIRST_PROMPT_NEEDS = "send a prompt to start"
_FIRST_PROMPT_HINT = "space to send it a prompt"

Rex = Callable[..., bytes]


def is_open_key(data: bytes) -> bool:
    return data in _OPEN_KEYS


def is_back_key(data: bytes) -> bool:
    return data in _BACK_KEYS


class Pen(NamedTuple):
    bg: bool = False      # any background colour
    bright: bool = True   # the default foreground, or white
    bold: bool = False
    dim: bool = False


def _colour(codes: list[int], i: int) -> tuple[bool, int]:
    """Whether the extended colour at codes[i] (38/48/58;5;n or ;2;r;g;b) is white, and its length."""
    if codes[i + 1:i + 2] == [5]:
        return codes[i + 2:i + 3] in ([7], [15], [255]), 3
    if codes[i + 1:i + 2] == [2]:
        return len(codes) >= i + 5 and min(codes[i + 2:i + 5]) >= 230, 5
    return False, 2


def _sgr(params: bytes, pen: Pen) -> Pen:
    bg, bright, bold, dim = pen
    codes = []
    for group in params.split(b";") if params else [b"0"]:
        parts = [int(p) if p.isdigit() else 0 for p in group.split(b":")]
        if len(parts) == 6:  # 38:2:<colour space>:r:g:b
            del parts[2]
        codes += parts
    i = 0
    while i < len(codes):
        code = codes[i]
        if code in (38, 48, 58):
            white, length = _colour(codes, i)
            if code == 38:
                bright = white
            elif code == 48:
                bg = True
            i += length
            continue
        if code == 0:
            bg, bright, bold, dim = Pen()
        elif code == 1:
            bold = True
        elif code == 2:
            dim = True
        elif code == 22:
            bold = dim = False
        elif 30 <= code <= 37 or 90 <= code <= 97:
            bright = code in (37, 97)
        elif code == 39:
            bright = True
        elif 40 <= code <= 47 or 100 <= code <= 107:
            bg = True
        elif code == 49:
            bg = False
        i += 1
    return Pen(bg, bright, bold, dim)


def _screen(vt: bytes) -> list[tuple[str, list[Pen]]]:
    """Each line of a `rex capture --format vt` as (text, the pen of each char)."""
    lines = []
    for raw in vt.split(b"\n"):
        text, pens, pen = [], [], Pen()
        for params, final, chunk in _TOKEN.findall(raw.rstrip(b"\r")):
            if chunk:
                s = chunk.decode(errors="replace")
                text.append(s)
                pens += [pen] * len(s)
            elif final == b"m":
                pen = _sgr(params, pen)
            elif final == b"C":
                n = int(params or 1)
                text.append(" " * n)
                pens += [pen] * n
        lines.append(("".join(text), pens))
    return lines


def _lines(vt: bytes) -> list[tuple[str, list[Pen]]]:
    return [(t, pens) for t, pens in _screen(vt) if t.strip()]


class Row(NamedTuple):
    name: str
    summary: str     # the column after the name: the session's result, question or "stopped"
    glyph: str = ""  # the status glyph: ∙ for a session without a process
    group: str = ""  # the group name above the row: "Needs input", "Working", "Completed"


def _row(text: str, group: str = "") -> Row | None:
    m = _ROW.match(text)
    if not m:
        return None
    summary = re.split(r"\s{2,}", text[m.end():].strip())[0]
    return Row(m.group(1).strip(), "" if _NOT_SUMMARY.fullmatch(summary) else summary, text.lstrip()[0], group)


def _listing(screen: list[tuple[str, list[Pen]]]) -> list[tuple[int, Row, Pen, bool]]:
    """The session rows above the input box: (line index, row, the pen of the name's first char,
    whether the ❯ pointer marks it).

    A row starts with a status glyph; the header above the list, which scrolls off a long
    one, and group names start with a letter or digit, and "… 46 more" with an ellipsis.
    The selected line, row or not, starts with "❯".
    """
    rows, group = [], ""
    for y, (text, pens) in enumerate(screen):
        pointed = text.startswith("❯")
        if pointed:
            text = " " + text[1:]
        glyph = text.lstrip()[:1]
        if glyph in ("─", "╭"):
            break
        if glyph.isalnum():
            group = text.strip()
        elif glyph and glyph != "…" and (row := _row(text, group)):
            name = len(text) - len(text.lstrip()) + 2  # after the status glyph and a space
            rows.append((y, row, pens[name], pointed))
    return rows


def _offers(footer: str, *hints: str) -> bool:
    """Whether one of the footer's hints starts with one of these. Options such as
    --model put their value first: "haiku · enter to open · …"."""
    return any(hint.strip().startswith(hints) for hint in footer.split(" · "))


def _opening(footer: str) -> bool:
    return _offers(footer, *_OPENING_FOOTERS)


def _list_in_front(footer: str) -> bool:
    """Whether clicks and Alt+N reach the list, whatever is selected, rather than a dialog."""
    return _offers(footer, *_LIST_FOOTERS)


def row_to_open(vt: bytes) -> Row | None:
    """The selected row, if the footer says Enter opens it.

    The ❯ pointer marks it. Older versions of Claude Code selected a row with a background
    (keys), or drew its name in the plain foreground colour instead of grey (mouse). A bold
    name isn't a selection.
    """
    if not _opening(_footer(vt)):
        return None
    rows = _listing(_screen(vt))
    selected = [row for _, row, _, pointed in rows if pointed]
    if not selected:
        selected = [row for _, row, pen, _ in rows if pen.bg]
    if not selected:
        selected = [row for _, row, pen, _ in rows if pen.bright and not (pen.bold or pen.dim)]
    return selected[0] if len(selected) == 1 else None


def clicked_row(vt: bytes, y: int) -> Row | None:
    """The session row on screen line y (from 1), if the footer says the list is in front."""
    if not _list_in_front(_footer(vt)):
        return None
    return next((row for line, row, _, _ in _listing(_screen(vt)) if line == y - 1), None)


def alt_digit(data: bytes) -> int | None:
    """N for Alt+N (kitty keyboard protocol or legacy), which opens the Nth row."""
    m = _ALT_DIGIT.fullmatch(data)
    return None if not m else int(m.group(1)) - 48 if m.group(1) else int(m.group(2))


def nth_row(vt: bytes, n: int) -> Row | None:
    """The Nth session row, if the footer says the list is in front."""
    if not _list_in_front(_footer(vt)):
        return None
    rows = _listing(_screen(vt))
    return rows[n - 1][1] if n <= len(rows) else None


def wants_back(vt: bytes) -> bool:
    """Whether an attached session's footer offers ← back to the agent view."""
    return bool(_BACK_HINT.search(_footer(vt)))


def _shown(session: dict) -> list[str]:
    """What the agent view may show in a session's summary column, from its job's state."""
    try:
        state = json.loads((claude_dir() / "jobs" / session["id"] / "state.json").read_text())
    except (OSError, ValueError):
        return []
    output = state.get("output")
    texts = [state.get("detail"), state.get("needs"), output.get("result") if isinstance(output, dict) else None]
    if state.get("needs") == _FIRST_PROMPT_NEEDS:
        texts.append(_FIRST_PROMPT_HINT)  # what the selected row may show instead
    return [" ".join(t.split()) for t in texts if isinstance(t, str)]


def _shows(session: dict, summary: str) -> bool:
    if summary.endswith("…"):
        return any(t.startswith(summary[:-1]) for t in _shown(session))
    return summary in _shown(session)


def _in_group(session: dict, group: str) -> bool:
    """Whether the session may be listed under this group. The agent view groups by the job's
    state when there is one: an idle process whose job is blocked on a question needs input."""
    status = session.get("status") if "pid" in session else None
    if group == "Working":
        return status == "busy" or session.get("state") == "working"
    if group == "Needs input":
        return status == "waiting" or session.get("state") == "blocked"
    if group == "Completed":
        return status not in ("busy", "waiting")
    return True


def match_session(name: str, sessions: list[dict], summary: str = "", glyph: str = "",
                  group: str = "") -> dict | None:
    sessions = [s for s in sessions if s.get("id")]  # background sessions only
    matches = [s for s in sessions if s.get("name") == name]
    if not matches and name.endswith("…") and len(name) > 1:
        matches = [s for s in sessions if (s.get("name") or "").startswith(name[:-1])]
    if not matches:
        # A new session's row shows its prompt until Claude names it; until then its name is its id.
        matches = [s for s in sessions if "pid" in s and s.get("name") in (None, "", s["id"])]
    # Tie-breakers for sessions with the same name. One that would rule out every session reads
    # the screen or the session wrong, so it's skipped rather than trusted.
    for fits in (lambda s: not glyph or ("pid" not in s) == (glyph == "∙"),
                 lambda s: _in_group(s, group),
                 lambda s: _shows(s, summary)):
        if len(matches) > 1:
            matches = [s for s in matches if fits(s)] or matches
    return matches[0] if len(matches) == 1 else None


def plan_placement(blocks: list[dict], agent_view: str, panes: set[str]) -> tuple[str, str, int]:
    """The first session opens right of the agent view; later ones stack below the last.

    Floating panes are never split, which would open the session inside their floating layer.
    """
    window = next((b.get("window_id") for b in blocks if b["block_id"] == agent_view), None)
    tiled = [b for b in blocks if b.get("window_id") == window and not b.get("floating")]
    opened = [b for b in tiled if b["block_id"] in panes]
    if opened:
        return opened[-1]["block_id"], "below", 50
    if tiled and not any(b["block_id"] == agent_view for b in tiled):
        return tiled[-1]["block_id"], "right", 50
    return agent_view, "right", 60


def _blocks(rex: Rex, rex_session: str) -> list[dict]:
    """Every block in the session, in layout order, with its window and whether it floats."""
    view = json.loads(rex("api", "call", "-s", rex_session, "session.view") or "{}")
    return [{**b, "window_id": w.get("window_id"), "floating": layer.get("kind") == "floating"}
            for w in view.get("windows") or [] for layer in w.get("layers") or []
            for b in layer.get("blocks") or []]


def open_session(session: dict, rex_session: str, agent_view: str, rex: Rex, attach_cmd: list[str]) -> None:
    sid = session["sessionId"]
    store = SessionStore(state_root(), sid)
    blocks = _blocks(rex, rex_session)
    ids = {b["block_id"] for b in blocks}
    if (pane := store.pane()) in ids:
        rex("focus", "-s", rex_session, pane)
        return
    anchor, direction, ratio = plan_placement(blocks, agent_view, ids & set(session_panes(state_root())))
    cwd = session.get("cwd") if os.path.isdir(session.get("cwd") or "") else os.path.expanduser("~")
    # A stopped session wakes in its cwd: `rexagents attach` checks it can first.
    stopped = [] if "pid" in session else [f"--cwd={session.get('cwd') or ''}"]
    label = session.get("name") or session.get("id") or sid[:8]
    created = rex("split", "-s", rex_session, "-b", anchor, "--split", direction, "--ratio", str(ratio),
                  "--label", label, "--json", "-c", cwd, "--shell", "none", "--", *attach_cmd, sid,
                  f"--title={session.get('name') or sid}", f"--agent-view={agent_view}", *stopped)
    # Recorded now as well as by the pane, so opening the session again before it starts focuses it.
    if new := (json.loads(created or "{}").get("block_ids") or [None])[0]:
        store.set_pane(new)


def _footer(vt: bytes) -> str:
    """The lines below the last box border, joined: a narrow pane wraps the footer.

    When the border is the last line (a short pane cut off below it), that's the footer.
    """
    texts = [text.strip() for text, _ in _lines(vt)]
    border = max((i for i, t in enumerate(texts) if t.startswith(("─", "╰"))), default=len(texts) - 1)
    return " ".join(texts[border + 1:]) or (texts[-1] if texts else "")


def _prompt(vt: bytes) -> str | None:
    """What an attached session's input box shows: "empty", "typed", "cut" or None.

    A pane too short for the whole box shows only its top, down to the top border:
    nothing typed can be seen there ("cut"). None means no input box is on screen,
    either because a dialog has taken its place or because the pane is shorter still.
    """
    screen = _screen(vt)
    while screen and not screen[-1][0].strip():
        screen.pop()
    for i in range(len(screen) - 1, 0, -1):
        text, pens = screen[i]
        if text.lstrip().startswith("❯") and screen[i - 1][0].lstrip().startswith("─"):
            start = text.index("❯") + 1
            typed = any(not c.isspace() and pens[j].bright and not pens[j].dim  # not a grey suggestion
                        for j, c in enumerate(text[start:], start))
            return "typed" if typed else "empty"
    return "cut" if screen and screen[-1][0].lstrip().startswith("─") else None


def _rows(rex: Rex, rex_session: str, block: str) -> int | None:
    try:
        size = rex("api", "call", "-s", rex_session, "com.superlogical.terminal.size",
                   json.dumps({"block_id": block}))
        return int(json.loads(size)["rows"])
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError):
        return None


def _in_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def open_input(rex_session: str, agent_view: str, rex: Rex, sessions: Callable[[], list[dict]],
               attach_cmd: list[str], background: Callable[[Callable[[], None]], None] = _in_background,
               ) -> Callable[..., bytes | None]:
    """Input hook for the agent view: keys that would attach a session open it in its own pane.

    Enter, → or Ctrl+Enter opens the selected row, Alt+N the Nth row, and a click the
    row clicked. With a prompt typed, Ctrl+Enter starts it as a new session (with
    Enter, which leaves the agent view in front), then opens it once it's listed.
    """
    def start_and_open(known: set[str]) -> None:
        try:
            for _ in range(40):
                time.sleep(0.25)
                new = [s for s in sessions() if s.get("id") and s.get("sessionId") not in known]
                if new:
                    open_session(new[0], rex_session, agent_view, rex, attach_cmd)
                    debug.log("open", key=_START_AND_OPEN, outcome=f"pane {new[0].get('id')}")
                    return
            debug.log("open", key=_START_AND_OPEN, outcome="no new session listed")
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
            debug.log("open", key=_START_AND_OPEN, outcome=f"no pane: {error!r}")

    clicked = {"opened": False}  # swallow the release of a click that opened a pane

    def on_input(data: bytes, drain: Callable[[], None]) -> bytes | None:
        n = alt_digit(data)
        click = _CLICK.fullmatch(data)
        if click and click.group(2) == b"m":
            opened, clicked["opened"] = clicked["opened"], False
            return None if opened else data
        if not (is_open_key(data) or n or data == _START_AND_OPEN or click):
            return data
        footer = row = None
        try:
            drain()
            time.sleep(0.02)  # let Rex take in what was just drawn
            vt = rex("capture", "-s", rex_session, "-b", agent_view, "--format", "vt")
            footer = _footer(vt)
            if data == _START_AND_OPEN and _offers(footer, _CREATE_FOOTER):
                known = {s.get("sessionId") for s in sessions()}
                background(lambda: start_and_open(known))
                return b"\r"
            if click:
                row = clicked_row(vt, int(click.group(1)))
            else:
                row = nth_row(vt, n) if n else row_to_open(vt)
            if not row:
                if not (_list_in_front(footer) if n or click else _opening(footer)):
                    why = "to claude: the footer doesn't say it opens a session"
                elif click:
                    why = "to claude: not a session row"
                else:
                    why = f"to claude: no row {n} on screen" if n else "inline: no selected row"
                debug.log("open", key=data, footer=footer, outcome=why)
                return data
            candidates = sessions()
            session = match_session(row.name, candidates, row.summary, row.glyph, row.group)
            if not session:
                debug.log("open", footer=footer, row=row.name, summary=row.summary, candidates=len(candidates),
                          outcome="inline: no single session matches")
                return data
            open_session(session, rex_session, agent_view, rex, attach_cmd)
            debug.log("open", footer=footer, row=row.name, summary=row.summary, candidates=len(candidates),
                      outcome=f"pane {session.get('id')}")
            clicked["opened"] = bool(click)
            return None
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
            debug.log("open", footer=footer, row=row and row.name, outcome=f"inline: {error!r}",
                      stderr=getattr(error, "stderr", None))
            return data
    return on_input


def attach_input(rex_session: str, block: str, agent_view: str, rex: Rex) -> Callable[..., bytes | None]:
    """Input hook for a session's pane: going back to the agent view closes the pane.

    That's ← when the footer offers it, or the second Ctrl+C or Ctrl+D while the
    footer asks for it, which would otherwise turn the pane into a second agent view.
    A small pane cuts the footer short or leaves it out, so there ← with an empty
    prompt goes back too, and so does a second Ctrl+C or Ctrl+D on an empty prompt
    soon enough that Claude would detach. A prompt cut off by a short pane counts as empty. The pane closes even if the agent view has gone.
    """
    last = {"letter": None, "at": 0.0}  # the previous Ctrl+C or Ctrl+D on an empty prompt

    def on_input(data: bytes, drain: Callable[[], None]) -> bytes | None:
        letter = _DETACH_KEYS.get(data)
        if not (letter or is_back_key(data)):
            return data
        now = time.monotonic() if letter else 0.0
        try:
            drain()
            vt = rex("capture", "-s", rex_session, "-b", block, "--format", "vt")
        except (subprocess.CalledProcessError, OSError) as error:
            debug.log("back", key=data, outcome=f"to claude: {error!r}")
            return data
        footer, prompt = _footer(vt), _prompt(vt)
        if prompt is None and (_rows(rex, rex_session, block) or _TINY_ROWS + 1) <= _TINY_ROWS:
            prompt = "cut"
        empty = prompt in ("empty", "cut")
        if letter:
            hidden = prompt == "cut" or footer.startswith(("─", "❯"))  # no room for the footer
            back = bool(_DETACH_HINTS[letter].search(footer)) or (
                hidden and empty and last["letter"] == letter and now - last["at"] < _DETACH_WINDOW)
            last.update(letter=letter if empty and not back else None, at=now)
        else:
            back = bool(_BACK_HINT.search(footer)) or empty
        if not back:
            debug.log("back", key=data, footer=footer, outcome="to claude: no hint")
            return data
        try:
            rex("focus", "-s", rex_session, agent_view)
            outcome = "close pane"
        except (subprocess.CalledProcessError, OSError):
            # Closed anyway: forwarding the key would turn the pane into an agent view
            # that opens sessions inline.
            outcome = "close pane (agent view gone)"
        debug.log("back", key=data, footer=footer, outcome=outcome)
        raise Stop
    return on_input


def agents_argv(agents_args: list[str], hook_settings: dict | None) -> list[str]:
    """`claude agents` with the status hooks, which it passes on to the sessions it dispatches."""
    argv = ["claude", "agents", *agents_args]
    if hook_settings is None or any(a == "--settings" or a.startswith("--settings=") for a in agents_args):
        return argv
    return argv + ["--settings", json.dumps(hook_settings)]


def _rex(*args: str) -> bytes:
    return subprocess.run(["rex", *args], capture_output=True, check=True).stdout


def _sessions(agents_args: list[str]) -> list[dict]:
    argv = ["claude", "agents", "--json", "--all"]  # --all includes stopped sessions
    for i, arg in enumerate(agents_args):
        if arg == "--cwd" and i + 1 < len(agents_args):
            argv += ["--cwd", agents_args[i + 1]]
        elif arg.startswith("--cwd="):
            argv.append(arg)
    out = subprocess.run(argv, capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


_AS_CLAUDE = "REXAGENTS_AS_CLAUDE"  # set while running again as the launcher


def show_as_claude(args: list[str]) -> None:
    """Rex picks a pane's icon from its program's argv[0], so run again as the launcher named
    `claude`, which is also the program a resume command names. Python runs the launcher as its
    script, so the command line Rex checks the resume command against has no flags of Python's."""
    if os.environ.pop(_AS_CLAUDE, None) is not None:
        return
    launcher = str(write_launcher(sys.executable, str(Path(__file__).resolve().parent.parent)))
    os.execve(sys.executable, [launcher, launcher, *args], {**os.environ, _AS_CLAUDE: "1"})


def run(agents_args: list[str]) -> int:
    hook_launcher = None if installed(user_settings()) else launcher()
    argv = agents_argv(agents_args, settings(hook_launcher) if hook_launcher else None)
    where = location()
    if not where:
        print("rexagents: not in a Rex pane, so sessions open in the agent view as usual.", file=sys.stderr)
        os.execvp(argv[0], argv)
    show_as_claude(agents_args)
    rex_session, block = where
    token = take_token()
    resume = Resume(token, [str(launcher_path()), *agents_args], cwd=os.getcwd()) if token else None

    attach_cmd = [str(launcher_path()), "attach"]
    on_input = open_input(rex_session, block, _rex, lambda: _sessions(agents_args), attach_cmd)
    return run_proxy(argv, on_input=on_input, resume=resume)
