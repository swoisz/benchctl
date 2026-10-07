"""Bytes to lines: incremental UTF-8, `\\n`/`\\r\\n`/bare `\\r` terminators, ANSI stripping
(design Section 4.3). A port of hail's RxBuf, take_line and strip_ansi.
"""

from __future__ import annotations

import codecs
import re

# CSI: ESC [ then parameter/intermediate bytes up to a final byte in 0x40-0x7E. Anything
# outside the final range counts as a parameter, as in hail, so an unterminated sequence
# runs to the end of the text instead of leaking its parameters.
_CSI = re.compile(r"\x1b\[[^\x40-\x7e]*[\x40-\x7e]?")
_TERMINATOR = re.compile(r"[\r\n]")


def strip_ansi(text: str) -> str:
    return _CSI.sub("", text)


def take_line(buf: str) -> tuple[str, str] | None:
    """Split the first complete line off `buf` as `(line, rest)`, terminator dropped.

    None when `buf` has no terminator yet, or ends in a lone `\\r` that may start `\\r\\n`.
    """
    match = _TERMINATOR.search(buf)
    if match is None:
        return None
    start = match.start()
    end = start + 1
    if buf[start] == "\r":
        if end == len(buf):
            return None
        if buf[end] == "\n":
            end += 1
    return buf[:start], buf[end:]


class LineEngine:
    """Turns serial reads into ANSI-stripped lines; the unterminated remainder is `tail`."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # Decoded but still carrying ANSI: stripping waits for a whole line, so a CSI split
        # across reads is removed intact.
        self._pending = ""

    def feed(self, data: bytes) -> list[str]:
        """Complete lines in `data` plus whatever earlier reads left pending."""
        self._pending += self._decoder.decode(data)
        lines: list[str] = []
        while (taken := take_line(self._pending)) is not None:
            line, self._pending = taken
            lines.append(strip_ansi(line))
        return lines

    @property
    def tail(self) -> str:
        """Text after the last terminator, ANSI-stripped: where an unterminated prompt sits.

        A held-back lone `\\r` already ends a line, so the tail is empty then.
        """
        if self._pending.endswith("\r"):
            return ""
        return strip_ansi(self._pending)
