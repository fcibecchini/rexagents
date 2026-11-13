"""Track where a terminal output stream can safely take an injected sequence.

The proxy inserts OSC 7501 reports into the output of `claude attach`. A report
spliced into the middle of an escape sequence or a UTF-8 character would corrupt
the screen, so this scanner follows just enough VT/UTF-8 structure to know where
the stream is "between things".
"""

from __future__ import annotations

GROUND, ESC, ESC_INTER, CSI, STRING, STRING_ESC = range(6)

_ESC, _BEL, _CAN, _SUB = 0x1B, 0x07, 0x18, 0x1A
# ESC P (DCS), ESC ] (OSC), ESC X (SOS), ESC ^ (PM), ESC _ (APC) open strings
# that end with ST (ESC \) or, in practice, BEL.
_STRING_INTRODUCERS = frozenset(b"P]X^_")


def _utf8_len(lead: int) -> int:
    if lead >= 0xF0:
        return 4
    if lead >= 0xE0:
        return 3
    if lead >= 0xC0:
        return 2
    return 1


class EscapeScanner:
    def __init__(self) -> None:
        self._state = GROUND
        self._utf8_pending = 0

    @property
    def safe(self) -> bool:
        return self._state == GROUND and self._utf8_pending == 0

    def feed(self, data: bytes) -> int:
        """Consume `data` and return the largest offset k (0..len(data)) such
        that the stream is at a safe point after `data[:k]`, or -1 if none."""
        last_safe = 0 if self.safe else -1
        i, n = 0, len(data)
        while i < n:
            if self._state == GROUND:
                esc = data.find(b"\x1b", i)
                end = n if esc < 0 else esc
                if end > i:
                    safe_in_run = self._scan_text(data, i, end)
                    if safe_in_run >= 0:
                        last_safe = safe_in_run
                if esc < 0:
                    break
                self._utf8_pending = 0
                self._state = ESC
                i = esc + 1
                continue

            b = data[i]
            i += 1
            if b in (_CAN, _SUB):
                self._state = GROUND
            elif self._state == ESC:
                if b == ord("["):
                    self._state = CSI
                elif b in _STRING_INTRODUCERS:
                    self._state = STRING
                elif 0x20 <= b <= 0x2F:
                    self._state = ESC_INTER
                elif b != _ESC:
                    self._state = GROUND
            elif self._state == ESC_INTER:
                if b == _ESC:
                    self._state = ESC
                elif not 0x20 <= b <= 0x2F:
                    self._state = GROUND
            elif self._state == CSI:
                if b == _ESC:
                    self._state = ESC
                elif 0x40 <= b <= 0x7E:
                    self._state = GROUND
            elif self._state == STRING:
                if b == _BEL:
                    self._state = GROUND
                elif b == _ESC:
                    self._state = STRING_ESC
                else:
                    # Skip the string body in one step; strings (e.g. OSC 8
                    # hyperlinks) can be long.
                    stop = min((p for p in (data.find(b"\x07", i), data.find(b"\x1b", i)) if p >= 0),
                               default=n)
                    i = stop
            elif self._state == STRING_ESC:
                if b == ord("\\"):
                    self._state = GROUND
                elif b != _ESC:
                    self._state = STRING
            if self._state == GROUND:
                last_safe = i
        return last_safe

    def _scan_text(self, data: bytes, start: int, end: int) -> int:
        """Process a run of plain text; return the last safe offset in it or -1."""
        i = start
        while self._utf8_pending and i < end:
            if 0x80 <= data[i] <= 0xBF:
                self._utf8_pending -= 1
                i += 1
            else:
                self._utf8_pending = 0  # malformed; resynchronise
        if self._utf8_pending:
            return -1
        # Only the tail of the run can hold an incomplete character.
        p = end - 1
        while p >= max(i, end - 3) and 0x80 <= data[p] <= 0xBF:
            p -= 1
        if p >= i and data[p] >= 0xC0:
            missing = _utf8_len(data[p]) - (end - p)
            if missing > 0:
                self._utf8_pending = missing
                return p
        return end
