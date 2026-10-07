"""Flashing on the daemon's held handle, and direct mode (design Section 4.10)."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

import serial

from benchctl import artifacts
from benchctl.defaults import RESET_WAIT_S
from benchctl.errors import BenchctlError, DeviceError, UsageError
from benchctl.linebuf import LineEngine
from benchctl.profile import FLASH_AFTER
from benchctl.types import AppDesc, FlashFile, FlashResult, Session

FLASH_CAUSE = "flash"  # the reset-arming cause and the gate method
# pyserial's message for a failed `exclusive=True` open; esptool embeds it in its FatalError.
_LOCK_FAILURE = "Could not exclusively lock port"

# An esptool.targets.CHIP_DEFS entry: (pyserial handle, initial baud) -> ESPLoader.
LoaderClass = Callable[[serial.Serial, int], Any]


class EventLineWriter(io.TextIOBase):
    """A text stream that emits each non-blank line written to it as one `event_type` event."""

    def __init__(self, session: Session, event_type: str) -> None:
        super().__init__()
        self._session = session
        self._type = event_type
        self._engine = LineEngine()

    def writable(self) -> bool:
        return True

    def write(self, text: str, /) -> int:
        for line in self._engine.feed(text.encode()):
            self._emit(line)
        return len(text)

    def close(self) -> None:
        if not self.closed:
            self._emit(self._engine.tail)
        super().close()

    def _emit(self, line: str) -> None:
        if line.strip():
            self._session.emit({"type": self._type, "line": line})


def _check_after(after: str) -> None:
    if after not in FLASH_AFTER:
        raise UsageError(f"invalid after {after!r}: one of {', '.join(FLASH_AFTER)}")


def _loader_class(chip: str) -> LoaderClass:
    # esptool is imported on first use: ~80 ms that verbs which never flash should not pay.
    from esptool.targets import CHIP_DEFS

    if chip not in CHIP_DEFS:
        raise UsageError(f"unknown chip {chip!r}: one of {', '.join(CHIP_DEFS)}")
    loader: LoaderClass = CHIP_DEFS[chip]
    return loader


def _flash_files(files: list[tuple[int, str, str]]) -> tuple[list[FlashFile], AppDesc | None]:
    flash_files = [
        FlashFile(addr, path, name, *artifacts.sha256_file(path)) for addr, path, name in files
    ]
    app = next((f for f in flash_files if f.name == "app"), None)
    return flash_files, None if app is None else artifacts.read_app_desc(app.path)


def _argv(
    chip: str,
    baud: int,
    before: str,
    after: str,
    flash_args: dict[str, str],
    files: list[FlashFile],
    *,
    port: str | None = None,
) -> list[str]:
    if set(flash_args) != set(artifacts.FLASH_ARG_KEYS):
        raise UsageError(f"flash_args must set exactly {', '.join(artifacts.FLASH_ARG_KEYS)}")
    argv = ["--chip", chip]
    if port is not None:
        argv += ["--port", port]
    argv += ["--baud", str(baud), "--before", before, "--after", after, "write-flash"]
    for key in artifacts.FLASH_ARG_KEYS:
        argv += [f"--{key}", flash_args[key]]
    for file in files:
        argv += [hex(file.addr), file.path]
    return argv


@contextlib.contextmanager
def _esptool_errors(port_path: str | None = None) -> Iterator[None]:
    """Turn every esptool failure into DeviceError (exit 4)."""
    import esptool

    try:
        yield
    except esptool.FatalError as exc:
        if port_path is not None and _LOCK_FAILURE in str(exc):
            raise DeviceError(
                f"port busy: {port_path} is locked by another process (a benchctl daemon?)"
            ) from exc
        raise DeviceError(f"esptool: {exc}") from exc
    except OSError as exc:  # pyserial's SerialException included
        raise DeviceError(f"esptool: serial error: {exc}") from exc
    except StopIteration as exc:
        raise DeviceError("esptool: the chip stopped responding") from exc
    except SystemExit as exc:
        # esptool.main re-raises click's usage exits. Uncaught, one would quietly end the
        # daemon's handler thread and leave its client waiting.
        raise DeviceError(f"esptool exited with status {exc.code}") from exc


def _write_on_transport(
    session: Session, loader_class: LoaderClass, before: str, argv: list[str]
) -> None:
    import esptool
    from esptool.loader import ESPLoader

    with session.pause_reader() as transport:
        settings = transport.get_settings()
        try:
            with (
                _esptool_errors(),
                EventLineWriter(session, "flash_log") as out,
                contextlib.redirect_stdout(out),
            ):
                esp = loader_class(transport.raw, ESPLoader.ESP_ROM_BAUD)
                esp.connect(before)
                esptool.main(argv, esp=esp)
        except BaseException:
            # A failed connect closes the port and the reader's reconnect reopens it, so a
            # restore that fails now must not hide why the flash failed.
            with contextlib.suppress(OSError, BenchctlError):
                transport.apply_settings(settings)
            raise
        transport.apply_settings(settings)


def flash_on_session(
    session: Session,
    files: list[tuple[int, str, str]],
    *,
    chip: str,
    before: str,
    after: str,
    baud: int,
    flash_args: dict[str, str],
) -> FlashResult:
    """Write `files` (addr, path, name) through the session's paused transport, then reset
    through the session (`hard-reset`) or move the gate to `bootloader` (`no-reset`)."""
    _check_after(after)
    loader_class = _loader_class(chip)
    flash_files, app = _flash_files(files)
    # esptool never resets here: its banner would arrive while the reader is parked, and the
    # settings restore would discard it. session.reset makes it an ordinary, armed reset.
    # `no-reset` is not enough: from the stub it soft-resets into the ROM loader, banner included.
    argv = _argv(chip, baud, "no-reset", "no-reset-stub", flash_args, flash_files)
    with session.device_lock():
        names = " ".join(f.name for f in flash_files)
        begin = session.note(f"flash begin {names} baud={baud} after={after}")
        started = time.monotonic()
        error: BaseException | None = None
        try:
            _write_on_transport(session, loader_class, before, argv)
        except BaseException as exc:
            error = exc
        duration = time.monotonic() - started
        status = "ok" if error is None else "error"
        session.note(f"flash end status={status} duration={duration:.1f}")
        event = {
            "type": "flash",
            "offset": begin,
            "files": [dataclasses.asdict(f) for f in flash_files],
            "app": None if app is None else dataclasses.asdict(app),
            "baud": baud,
            "after": after,
            "duration": duration,
            "status": status,
            "error": None if error is None else str(error),
        }
        if error is not None:
            # The chip is in the flasher stub, its ROM loader, or somewhere unknown: cmd must
            # refuse until an explicit reset.
            session.set_gate("bootloader", method=FLASH_CAUSE)
            session.emit(event)
            raise error
        reset = None
        if after == "hard-reset":
            reset = session.reset(False, wait_rst=RESET_WAIT_S, cause=FLASH_CAUSE)
        else:
            session.set_gate("bootloader", method=FLASH_CAUSE)
        session.emit(event)
    return FlashResult(files=flash_files, app=app, baud=baud, duration=duration, reset=reset)


def flash_direct(
    port_path: str,
    files: list[tuple[int, str, str]],
    *,
    chip: str,
    before: str,
    after: str,
    baud: int,
    flash_args: dict[str, str],
) -> FlashResult:
    """Write `files` with esptool opening, resetting and closing `port_path` itself (no daemon).

    esptool's output goes to stderr so stdout stays clean for `--json`.
    """
    import esptool

    _check_after(after)
    _loader_class(chip)
    flash_files, app = _flash_files(files)
    argv = _argv(chip, baud, before, after, flash_args, flash_files, port=port_path)
    started = time.monotonic()
    with _esptool_errors(port_path), contextlib.redirect_stdout(sys.stderr):
        esptool.main(argv)
    duration = time.monotonic() - started
    return FlashResult(files=flash_files, app=app, baud=baud, duration=duration, reset=None)
