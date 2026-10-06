"""`capture --reset` in daemon and direct mode (design Section 4.11)."""

from __future__ import annotations

import errno
import time
from datetime import datetime

import serial

from benchctl.errors import DeviceError, UsageError
from benchctl.linebuf import LineEngine
from benchctl.types import LogLine, ModemLines, ResetInfo, Session

RESET_WAIT_S = 5.0
READ_TIMEOUT_S = 0.1  # the Section 4.9 port settings
_US_PER_MS = 1000
# flock(LOCK_EX | LOCK_NB) on a port another process holds.
_LOCKED = (errno.EAGAIN, errno.EWOULDBLOCK)


def _check_seconds(seconds: float) -> None:
    if seconds <= 0:
        raise UsageError(f"capture needs a positive duration, got {seconds}")


def _timestamp() -> str:
    """HH:MM:SS.mmm local, as a serial.log line starts."""
    now = datetime.now()
    return f"{now:%H:%M:%S}.{now.microsecond // _US_PER_MS:03d}"


def capture_on_session(session: Session, seconds: float) -> tuple[list[LogLine], ResetInfo | None]:
    """Reset, then return the stored lines from the reset request to `seconds` after it."""
    _check_seconds(seconds)
    # Taken before the reset, so the ROM banner that precedes the `rst:` line is included, as
    # in direct mode.
    start = session.mark(None)
    reset_at = time.monotonic()
    reset = session.reset(False, wait_rst=min(seconds, RESET_WAIT_S))
    time.sleep(max(0.0, reset_at + seconds - time.monotonic()))
    end = session.mark(None)
    if end <= start:
        return [], reset
    return session.log(start, end, None, end - start), reset


def capture_direct(
    port_path: str, baud: int, seconds: float, lines: ModemLines | None = None
) -> list[LogLine]:
    """Open the port, reset through `lines` (default: esptool's HardReset), read for `seconds`.

    Offsets count from 0 and `t` from the reset call. Lines are not redacted: the caller
    holds the profile.
    """
    _check_seconds(seconds)
    from esptool.reset import HardReset

    port = serial.Serial()
    port.port = port_path
    port.baudrate = baud
    port.timeout = READ_TIMEOUT_S
    port.exclusive = True
    port.dtr = False
    port.rts = False
    try:
        port.open()
    except serial.SerialException as exc:
        if exc.errno in _LOCKED:
            raise DeviceError(
                f"port busy: {port_path} is locked by another process (a benchctl daemon?)"
            ) from exc
        raise DeviceError(f"cannot open {port_path}: {exc}") from exc

    engine = LineEngine()
    captured: list[LogLine] = []
    try:
        started = time.monotonic()
        if lines is None:
            HardReset(port)()
        else:
            lines(False)
        while time.monotonic() - started < seconds:
            # in_waiting first: a fixed-size read would hold each line until its timeout, and
            # skew `t` by up to READ_TIMEOUT_S.
            data = port.read(port.in_waiting or 1)
            t = time.monotonic() - started
            texts = engine.feed(data)
            ts = _timestamp() if texts else ""
            for text in texts:
                captured.append(LogLine(offset=len(captured), t=t, ts=ts, text=text, meta=False))
    except OSError as exc:  # pyserial's SerialException included
        raise DeviceError(f"{port_path}: port lost during capture: {exc}") from exc
    finally:
        port.close()
    return captured
