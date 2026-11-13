"""Encoding of OSC 7501 Program Status Protocol reports.

See https://www.superlogical.com/rex/docs/build/program-status
"""

from __future__ import annotations

import base64
import re

STATES = frozenset({"idle", "working", "done", "blocked", "error"})
KINDS = frozenset({"permission", "question", "auth"})
APP = "claude-code"

MSG_MAX_BYTES = 2048
TITLE_MAX_BYTES = 192

_PREFIX = b"\x1b]7501;"
_ST = b"\x1b\\"
# C0, DEL and C1 controls are forbidden in decoded msg/title.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
_BIDI = re.compile("[‎‏‪-‮⁦-⁩]")
_SPACES = re.compile(r"\s+")


def sanitize_text(text: str, max_bytes: int) -> str:
    text = _BIDI.sub("", text)
    text = _SPACES.sub(" ", _CONTROL.sub(" ", text)).strip()
    if len(text.encode()) <= max_bytes:
        return text
    ellipsis = "…"
    budget = max_bytes - len(ellipsis.encode())
    cut = text.encode()[:budget].decode(errors="ignore").rstrip()
    return cut + ellipsis


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode("ascii")


def encode_status(state: str, *, kind: str | None = None, msg: str | None = None,
                  title: str | None = None, app: str = APP) -> bytes:
    if state not in STATES:
        raise ValueError(f"not a reportable state: {state!r}")
    pairs = [f"state={state}", f"app={app}"]
    if state == "blocked" and kind in KINDS:
        pairs.append(f"kind={kind}")
    if title and (title := sanitize_text(title, TITLE_MAX_BYTES)):
        pairs.append(f"title={_b64(title)}")
    if msg and (msg := sanitize_text(msg, MSG_MAX_BYTES)):
        pairs.append(f"msg={_b64(msg)}")
    return _PREFIX + ":".join(pairs).encode("ascii") + _ST


def encode_clear() -> bytes:
    return _PREFIX + b"state=clear" + _ST
