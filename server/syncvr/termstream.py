"""Turns a terminal byte stream into plain text lines (no Qt, no pty).

Shell output arrives in arbitrary chunks, so everything here is incremental: an escape sequence or a UTF-8
character split between two ``feed`` calls decodes the same as one delivered whole. Colours and other
escapes are dropped; carriage return, backspace and tab move the cursor like a dumb terminal would.
"""

import codecs
from typing import List

MAX_LINES = 5000
TAB = 8

_TEXT, _ESC, _CSI, _STRING, _STRING_ESC, _SKIP = range(6)


class TermStream:
    def __init__(self, max_lines: int = MAX_LINES):
        self.max_lines = max_lines
        self.lines: List[str] = [""]  # the last one is the line the cursor is on
        self.col = 0
        self.bells = 0
        self.dropped = 0  # lines pushed out by the cap, so a view can tell its text is stale
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._state = _TEXT
        self._params = ""

    def text(self) -> str:
        return "\n".join(self.lines)

    def feed(self, data: bytes) -> None:
        for ch in self._decoder.decode(data):
            self._char(ch)

    # ------------------------------------------------------------ state machine

    def _char(self, ch: str) -> None:
        state = self._state
        if state == _TEXT:
            self._text(ch)
        elif state == _ESC:
            if ch == "[":
                self._state, self._params = _CSI, ""
            elif ch in "]PX^_":  # OSC, DCS, SOS, PM, APC: a string that ends at BEL or ESC \
                self._state = _STRING
            elif "\x20" <= ch <= "\x2f":  # ESC ( B and friends: intermediates, then one final character
                self._state = _SKIP
            else:
                self._state = _TEXT
        elif state == _CSI:
            if "\x40" <= ch <= "\x7e":
                self._csi(ch, self._params)
                self._state = _TEXT
            elif ch == "\x1b":  # an interrupted sequence: start over
                self._state = _ESC
            else:
                self._params += ch
        elif state == _STRING:
            if ch == "\x07":
                self._state = _TEXT
            elif ch == "\x1b":
                self._state = _STRING_ESC
        elif state == _STRING_ESC:
            self._state = _TEXT if ch == "\\" else _STRING if ch != "\x1b" else _STRING_ESC
        else:  # _SKIP
            if not "\x20" <= ch <= "\x2f":
                self._state = _TEXT

    def _text(self, ch: str) -> None:
        if ch == "\x1b":
            self._state = _ESC
        elif ch == "\n":
            self.lines.append("")
            self.col = 0
            if len(self.lines) > self.max_lines:
                del self.lines[:len(self.lines) - self.max_lines]
                self.dropped += 1
        elif ch == "\r":
            self.col = 0
        elif ch == "\b":
            self.col = max(0, self.col - 1)
        elif ch == "\t":
            self._put(" " * (TAB - self.col % TAB))
        elif ch == "\x07":
            self.bells += 1
        elif ch >= " " and ch != "\x7f" and not "\x80" <= ch < "\xa0":
            self._put(ch)

    def _put(self, s: str) -> None:
        line = self.lines[-1]
        if self.col > len(line):
            line += " " * (self.col - len(line))
        self.lines[-1] = line[:self.col] + s + line[self.col + len(s):]
        self.col += len(s)

    def _csi(self, final: str, params: str) -> None:
        """Only what line editing needs: cursor left/right/column and erase in line; everything else is dropped."""
        first = params.split(";")[0].lstrip("?>")
        n = int(first) if first.isdigit() else None
        line = self.lines[-1]
        if final == "C":
            self.col += n or 1
        elif final == "D":
            self.col = max(0, self.col - (n or 1))
        elif final == "G":
            self.col = max(0, (n or 1) - 1)
        elif final == "K":
            if n in (None, 0):
                self.lines[-1] = line[:self.col]
            elif n == 1:
                self.lines[-1] = " " * min(self.col, len(line)) + line[self.col:]
            else:
                self.lines[-1] = ""
        elif final == "P":  # delete characters
            self.lines[-1] = line[:self.col] + line[self.col + (n or 1):]
