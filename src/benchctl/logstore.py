"""Every line of one daemon session, in memory and in `serial.log` (design Section 4.7).

The store's Condition is the session's single rendezvous: the reader appends and publishes
the tail under it, and every waiter blocks on it with a deadline and rescans from its cursor.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from benchctl.errors import UsageError
from benchctl.types import Cursor, LogLine, Offset

META_PREFIX = "## "
_MS_DIGITS = 3


def format_line(line: LogLine) -> str:
    """The serial.log form: `HH:MM:SS.mmm +SSSSS.mmm <text>`."""
    return f"{line.ts} +{line.t:09.3f} {line.text}"


class LogStore:
    """Offsets start at 0 at attach and count meta lines too. Thread-safe: every method
    takes `cond`, an RLock-backed Condition notified on each append and tail change."""

    def __init__(self, path: Path) -> None:
        self.cond = threading.Condition(threading.RLock())
        self.path = path
        self.boot: Offset = 0  # offset of the most recent reset line
        self._t0 = time.monotonic()
        self._lines: list[LogLine] = []
        self._tail = ""
        self._marks: dict[str, Offset] = {}
        self._file = path.open("a", encoding="utf-8")
        self._closed = False

    def now(self) -> float:
        """Seconds since attach."""
        return time.monotonic() - self._t0

    @property
    def next_offset(self) -> Offset:
        """The offset the next stored line will get (the number of lines stored)."""
        with self.cond:
            return len(self._lines)

    @property
    def tail(self) -> str:
        with self.cond:
            return self._tail

    def append(self, text: str, *, meta: bool = False) -> LogLine:
        """Store one line (already redacted). A meta line's text gets the `## ` prefix."""
        if meta:
            # A meta line is one stored line, whatever the caller's text holds.
            text = META_PREFIX + " ".join(text.splitlines())
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-_MS_DIGITS]
        with self.cond:
            line = LogLine(len(self._lines), round(self.now(), _MS_DIGITS), stamp, text, meta)
            self._lines.append(line)
            if not self._closed:
                # Flushed per line so the file is readable while the session runs; fsync is
                # the housekeeping thread's (sync), once a second.
                self._file.write(format_line(line) + "\n")
                self._file.flush()
            self.cond.notify_all()
            return line

    def set_tail(self, text: str) -> bool:
        """Publish the reader's unterminated tail; True (and waiters woken) when it changed."""
        with self.cond:
            if text == self._tail:
                return False
            self._tail = text
            self.cond.notify_all()
            return True

    def lines(self, since: Offset, until: Offset | None = None) -> list[LogLine]:
        """Stored lines with `since <= offset < until` (`until` None: to the end)."""
        with self.cond:
            return self._lines[max(since, 0) : until]

    def before(self, offset: Offset, count: int) -> list[LogLine]:
        """Up to `count` lines immediately before `offset`."""
        with self.cond:
            return self._lines[max(offset - count, 0) : max(offset, 0)]

    def set_mark(self, name: str, offset: Offset) -> None:
        with self.cond:
            self._marks[name] = offset

    def resolve(self, cursor: str) -> Offset:
        """A cursor in the Section 7.1 grammar as an offset. Raises UsageError for an unknown
        mark or malformed cursor."""
        parsed = Cursor.parse(cursor)
        with self.cond:
            if parsed.kind == "attach":
                return 0
            if parsed.kind == "boot":
                return self.boot
            if parsed.kind == "offset" and parsed.offset is not None:
                return parsed.offset
            name = parsed.mark or ""
            if name not in self._marks:
                known = ", ".join(sorted(self._marks)) or "none"
                raise UsageError(f"unknown mark {name!r} (marks: {known})")
            return self._marks[name]

    def sync(self) -> None:
        """Push serial.log to disk."""
        with self.cond:
            if not self._closed:
                self._file.flush()
                os.fsync(self._file.fileno())

    def close(self) -> None:
        with self.cond:
            if self._closed:
                return
            self.sync()
            self._closed = True
            self._file.close()
