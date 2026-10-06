"""Shared dataclasses and Protocols: the seams between units (design Section 4.1).

Frozen once the scaffold lands. A unit that needs a change here reports it instead of editing.
Every dataclass is JSON-serialisable through `dataclasses.asdict`.
"""

from __future__ import annotations

import queue
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from benchctl.errors import BenchctlError, UsageError

if TYPE_CHECKING:
    import serial

    from benchctl.profile import Profile

# Line index in the daemon log since attach, 0-based, meta lines included.
Offset = int

# Reset strategy: True enters the bootloader. The default pulses RTS/DTR via esptool.reset;
# tests inject one bound to the fake device because modem-line ioctls fail on a pty.
ModemLines = Callable[[bool], None]

# One events.jsonl record (Section 7.5).
Event = dict[str, Any]

SUBSCRIBER_QUEUE_SIZE = 10_000

_OFFSET_TEXT = re.compile(r"[0-9]+")
_NEGATIVE_TEXT = re.compile(r"-[0-9]+")


@dataclass(frozen=True, slots=True)
class Cursor:
    """A `--since`/`--until` value in the Section 7.1 grammar; Session.resolve turns it into
    an Offset. Unknown mark names are a resolve-time error, not a parse error."""

    kind: Literal["attach", "boot", "offset", "mark"]
    offset: Offset | None = None
    mark: str | None = None

    @classmethod
    def parse(cls, text: str) -> Cursor:
        if text == "attach":
            return cls("attach")
        if text == "boot":
            return cls("boot")
        if _OFFSET_TEXT.fullmatch(text):
            return cls("offset", offset=int(text))
        if not text.strip() or _NEGATIVE_TEXT.fullmatch(text):
            raise UsageError(
                f"invalid cursor {text!r}: expected attach, boot, an offset, or a mark"
            )
        return cls("mark", mark=text)


@dataclass(frozen=True, slots=True)
class LogLine:
    offset: Offset
    t: float  # seconds since attach
    ts: str  # HH:MM:SS.mmm local, the first token of a serial.log line
    text: str
    meta: bool  # benchctl's own `## ` line rather than device output


@dataclass(frozen=True, slots=True)
class ResetInfo:
    offset: Offset
    t: float
    code: int | None
    reason: str
    boot_mode: str | None
    elf_sha: str | None
    expected: bool
    cause: str | None
    context: list[str]


@dataclass(frozen=True, slots=True)
class Identity:
    fields: dict[str, str]
    verified: bool
    mismatches: dict[str, tuple[str, str]]  # field -> (expected, got)


@dataclass(frozen=True, slots=True)
class CmdResult:
    tx_offset: Offset
    status: Literal["ok", "error", "timeout"]
    line: str | None
    groups: list[str]
    captured: str | None
    duration: float


@dataclass(frozen=True, slots=True)
class WaitResult:
    result: Literal[
        "match", "timeout", "absent_ok", "absent_violated", "order_ok", "order_violated"
    ]
    line: str | None
    groups: list[str]
    offset: Offset | None
    t: float | None
    duration: float
    order: list[Offset]


@dataclass(frozen=True, slots=True)
class FlashFile:
    addr: int
    path: str
    name: str  # app | bootloader | partition-table | otadata | <addr hex> for bare pairs
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class AppDesc:
    version: str
    project: str
    elf_sha256: str
    idf_ver: str
    time: str
    date: str


@dataclass(frozen=True, slots=True)
class FlashResult:
    files: list[FlashFile]
    app: AppDesc | None
    baud: int
    duration: float
    reset: ResetInfo | None


@dataclass(frozen=True, slots=True)
class GateState:
    state: Literal["booting", "settling", "ready", "unknown", "bootloader"]
    since: float
    method: str | None


@dataclass(frozen=True, slots=True)
class Status:
    unit: str
    pid: int
    attached_at: str
    port: str | None
    connected: bool
    baud: int
    lease: dict[str, Any]
    identity: Identity | None
    last_reset: ResetInfo | None
    offset: Offset
    gate: GateState
    session_dir: str


class Subscription:
    """Bounded feed of LogLine and Event items for one subscriber (Section 4.2).

    Producers never block: `offer` returns False once the feed is closed or full, and the
    producer then forgets it. A full queue ends the feed with error code `slow_client`.
    """

    def __init__(self, maxsize: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue: queue.Queue[LogLine | Event] = queue.Queue(maxsize)
        self._error: BenchctlError | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def offer(self, item: LogLine | Event) -> bool:
        if self._closed:
            return False
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.fail(BenchctlError("subscriber fell behind", code="slow_client"))
            return False
        return True

    def fail(self, error: BenchctlError) -> None:
        """End the feed; the consumer's next `get` raises `error`."""
        self._error = error
        self._closed = True

    def get(self, timeout: float) -> LogLine | Event | None:
        """Next item, or None after `timeout` seconds. Raises the error the feed ended with."""
        if self._error is not None:
            raise self._error
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._error is not None:
                raise self._error from None
            return None

    def close(self) -> None:
        """Unsubscribe; producers drop the feed on their next `offer`."""
        self._closed = True


class Transport(Protocol):
    """The serial port. Implemented by U4; used by U4 and U6."""

    path: str

    def read(self, max_bytes: int, timeout: float) -> bytes:
        """b"" on timeout; raises TransportLost when the port vanishes."""
        ...

    def write(self, data: bytes) -> None: ...

    def reset(self, bootloader: bool = False) -> None: ...

    def get_settings(self) -> dict[str, Any]: ...

    def apply_settings(self, s: dict[str, Any]) -> None:
        """Restore settings from get_settings() and discard pending input; callers do nothing
        more."""
        ...

    def set_baud(self, baud: int) -> None: ...

    def close(self) -> None: ...

    @property
    def raw(self) -> serial.Serial:
        """The pyserial handle, for esptool only."""
        ...


class Session(Protocol):
    """One attached unit inside the daemon. Implemented by U4; used by U5, U6 and U7's tests."""

    profile: Profile
    unit: str
    session_dir: Path

    def status(self) -> Status: ...

    def mark(self, name: str | None) -> Offset: ...

    def note(self, text: str) -> Offset:
        """Append the meta line `## <text>`, redacted; it occupies an offset."""
        ...

    def resolve(self, cursor: str) -> Offset: ...

    def cmd(
        self,
        line: str,
        *,
        expect: str | None,
        error: list[str],
        capture: bool | None,
        timeout: float,
        reboots: bool,
        secret: bool,
        force: bool,
        source: str,
    ) -> CmdResult: ...

    def wait(
        self,
        pattern: str | None,
        since: Offset,
        timeout: float,
        *,
        absent: str | None,
        window: float | None,
        order: list[str] | None,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> WaitResult: ...

    def log(
        self, since: Offset, until: Offset | None, grep: str | None, limit: int
    ) -> list[LogLine]: ...

    def reset(
        self, bootloader: bool, wait_rst: float = 5.0, *, cause: str = "reset"
    ) -> ResetInfo | None:
        """`cause` is the arming record's cause (Section 4.5); flash passes "flash"."""
        ...

    def set_gate(self, state: Literal["booting", "bootloader"], method: str | None = None) -> None:
        """The only gate write from outside gate.py (flash with `--after no-reset`)."""
        ...

    def arm_reset(self, cause: str, window: float) -> None: ...

    def identity(self, reprobe: bool = False) -> Identity: ...

    def gate_check(self, since: Offset, timeout: float) -> dict[str, Any]:
        """Env gate (Section 5): `{ok, missing, forbidden_hits}` as in the `gate` op."""
        ...

    def subscribe(self) -> Subscription: ...

    def device_lock(self) -> AbstractContextManager[None]: ...

    def pause_reader(self) -> AbstractContextManager[Transport]:
        """Parks the reader thread and yields the transport; resumes on exit."""
        ...

    def emit(self, event: Event) -> None:
        """Append to the session events.jsonl and offer to subscribers."""
        ...

    def close(self) -> None: ...


class DeviceClient(Protocol):
    """One method per socket op (Section 7.2), same names and arguments; raises BenchctlError.

    Implemented by U5's Client; used by U7's runner. `since`/`until` accept a cursor string
    or an Offset; the daemon resolves strings at request time.
    """

    def hello(self, owner: str, client: str) -> dict[str, Any]: ...

    def status(self) -> Status: ...

    def mark(self, name: str | None) -> Offset: ...

    def resolve(self, cursor: str) -> Offset: ...

    def cmd(
        self,
        line: str,
        *,
        expect: str | None,
        error: list[str],
        capture: bool | None,
        timeout: float,
        reboots: bool,
        secret: bool,
        force: bool,
        source: str,
    ) -> CmdResult: ...

    def wait(
        self,
        pattern: str | None,
        since: Offset | str,
        timeout: float,
        *,
        absent: str | None,
        window: float | None,
        order: list[str] | None,
    ) -> WaitResult: ...

    def log(
        self, since: Offset | str, until: Offset | str | None, grep: str | None, limit: int
    ) -> tuple[list[LogLine], Offset]:
        """(lines, next offset)."""
        ...

    def reset(self, bootloader: bool, wait_rst: float = 5.0) -> tuple[Offset, ResetInfo | None]:
        """(offset of the reset request, the reset seen within wait_rst or None)."""
        ...

    def arm_reset(self, cause: str, window: float) -> None: ...

    def flash(
        self,
        files: list[tuple[int, str, str]],
        *,
        after: str,
        baud: int,
        flash_args: dict[str, str],
    ) -> FlashResult:
        """files: (addr, path, name) as resolved by artifacts.resolve."""
        ...

    def capture(self, seconds: float) -> tuple[list[LogLine], ResetInfo | None]: ...

    def identity(self, reprobe: bool = False) -> Identity: ...

    def gate(self, since: Offset | str, timeout: float) -> dict[str, Any]: ...

    def tail(self, since: Offset | str, events: bool) -> Subscription:
        """Streams LogLine (and Event when `events`) items until the Subscription is closed."""
        ...

    def lease(self, action: Literal["take", "release"]) -> dict[str, Any]: ...

    def detach(self) -> None: ...
