"""Simulated ESP-IDF device on a pty (design Section 6.1).

The device stays silent until `reset()`, like a unit whose port was just opened; pyserial's
open flushes the input queue anyway, so anything written earlier would never be seen.
All script times are device seconds, scaled by `timescale` and measured from the reset.
"""

from __future__ import annotations

import heapq
import itertools
import os
import select
import threading
import time
import tty
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from serial.tools.list_ports_common import ListPortInfo

PROMPT = "demo> "
APP_VERSION = "1.2.3-4-gabc"
ELF_SHA_PREFIX = "0123456789abcdef"
SECRET = "hunter2"

# Espressif's native USB-Serial/JTAG ids: a port that carries a serial number, so a test can
# re-resolve it by serial after `reappear()` changes the path.
FAKE_VID = 0x303A
FAKE_PID = 0x1001

ROM_BANNER = b"ESP-ROM:esp32s3-20210327\r\nBuild:Mar 27 2021\r\n"
RST_POWERON = b"rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
RST_SW = b"rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
RST_DOWNLOAD = b"rst:0x1 (POWERON),boot:0x0 (DOWNLOAD(USB/UART0))\r\nwaiting for download\r\n"

# Gap before the second half of a deliberately split write. At the default timescale this is
# 5 ms of wall time, long enough that a reader sees the halves in separate reads.
SPLIT_GAP_S = 0.1

_POLL_S = 0.05
# A UART with nobody listening drops bytes instead of waiting; so does the fake, after this.
_WRITE_STALL_S = 0.5
_JOIN_S = 2.0
_READ_CHUNK = 4096

_ANSI_COLOURS = {"E": "\x1b[0;31m", "W": "\x1b[0;33m", "I": "\x1b[0;32m"}
_ANSI_RESET = "\x1b[0m"


def idf_line(level: str, ms: int, tag: str, msg: str) -> bytes:
    """An ANSI-coloured `L (ms) tag: msg` line as ESP-IDF's logger prints it."""
    return f"{_ANSI_COLOURS[level]}{level} ({ms}) {tag}: {msg}{_ANSI_RESET}\r\n".encode()


def _default_boot() -> tuple[tuple[float, bytes], ...]:
    sha = idf_line("I", 305, "app_init", f"ELF file SHA256:  {ELF_SHA_PREFIX}")
    temp = idf_line("I", 420, "board", "die temp 41.5°C")
    mid_degree = temp.index("°".encode()) + 1
    return (
        (0.05, idf_line("I", 24, "boot", "ESP-IDF v5.4 2nd stage bootloader")),
        (0.02, idf_line("I", 31, "boot", "chip revision: v0.2")),
        (0.2, idf_line("I", 260, "cpu_start", "Multicore app")),
        (0.02, idf_line("I", 290, "app_init", "Project name:     fakeapp")),
        (0.01, idf_line("I", 291, "app_init", f"App version:      {APP_VERSION}")),
        (0.01, sha[:-1]),
        (SPLIT_GAP_S, sha[-1:]),
        (0.01, temp[:mid_degree]),
        (SPLIT_GAP_S, temp[mid_degree:]),
        (0.18, idf_line("W", 700, "wifi", "no saved network")),
        (0.45, idf_line("I", 1150, "console", "shell started")),
    )


DEFAULT_BOOT = _default_boot()
DEFAULT_REPLIES: Mapping[str, str] = {
    "version": f"Application Version: {APP_VERSION}",
    "secret": f"sip_user=bench sip_pass={SECRET}",
    "slow": "slow: done",
}


@dataclass(frozen=True)
class DeviceScript:
    """What the device prints and how its shell behaves. Times are device seconds."""

    boot: tuple[tuple[float, bytes], ...] = DEFAULT_BOOT  # (delay after previous write, bytes)
    prompt: str = PROMPT
    prompt_delay: float = 0.05  # after the last boot write; the default boot prompts at 1.2 s
    line_ending: bytes = b"\r"
    blind_s: float = 1.2  # commands arriving this soon after a reset are lost
    reboot_window_cmds: frozenset[str] = field(default_factory=frozenset)  # by first word
    win_start: float = 1.2
    win_end: float = 2.0
    slow_s: float = 2.0
    replies: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_REPLIES))


@dataclass(frozen=True)
class RxRecord:
    line: str
    outcome: Literal["dropped", "answered", "rebooted", "ignored"]
    since_reset: float | None  # device seconds since the last reset when the line arrived


_Action = bytes | Callable[[], None]
_serials = itertools.count(1)


class FakeDevice:
    """An ESP-IDF unit behind a pty slave at `path`.

    `reset()` replays the boot script; `reboot` and reboot-window commands reset it from the
    inside. `received` and `resets` record what happened for assertions. `on_change` runs
    after the pty appears or disappears (open, vanish, reappear, close).
    """

    def __init__(
        self,
        script: DeviceScript | None = None,
        timescale: float = 0.05,
        on_change: Callable[[FakeDevice], None] | None = None,
    ) -> None:
        self.script = script or DeviceScript()
        self.timescale = timescale
        self._on_change = on_change
        self.serial_number = f"FAKE{next(_serials):04d}"
        self.received: list[RxRecord] = []
        self.resets: list[Literal["poweron", "sw", "download"]] = []
        self.path = ""
        self._lock = threading.Lock()
        self._schedule: list[tuple[float, int, _Action]] = []
        self._seq = itertools.count()
        self._t_reset: float | None = None
        self._mode: Literal["app", "bootloader"] = "app"
        self._shell_free_at = 0.0
        self._rx = b""
        self._wake_r, self._wake_w = os.pipe()
        self._master = -1
        self._slave = -1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._open()

    def __enter__(self) -> FakeDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def present(self) -> bool:
        """True while the pty exists, as a USB port is listed only while plugged in."""
        return self._master >= 0

    def reset(self, bootloader: bool = False) -> None:
        """Hardware reset: replay the boot script, or enter the ROM download mode."""
        with self._lock:
            self._boot("download" if bootloader else "poweron")
        os.write(self._wake_w, b"\0")

    def vanish(self) -> None:
        """Close the pty like an unplugged USB port; the slave's next read fails."""
        if self._master < 0:
            return
        self._stop_thread()
        self._close_pty()

    def reappear(self) -> str:
        """A new pty pair at a new path. The unit's shell is up; nothing is replayed."""
        self.vanish()
        with self._lock:
            self._schedule.clear()
            self._rx = b""
            self._t_reset = None
            self._mode = "app"
            self._shell_free_at = 0.0
        self._open()
        return self.path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.vanish()
        os.close(self._wake_r)
        os.close(self._wake_w)

    def port_info(self) -> ListPortInfo:
        """How `serial.tools.list_ports` would describe this port, for registry matching."""
        info = ListPortInfo(self.path, skip_link_detection=True)
        info.vid = FAKE_VID
        info.pid = FAKE_PID
        info.serial_number = self.serial_number
        info.location = "fake-1"
        info.manufacturer = "benchctl"
        info.product = "FakeDevice"
        info.description = "FakeDevice"
        info.hwid = info.usb_info()
        return info

    def _open(self) -> None:
        self._master, self._slave = self._openpty_avoiding(self.path)
        # Raw before anyone opens the slave: a cooked pty echoes the device's own output back
        # into its RX and rewrites \r as \n. The fake holds the slave open for its lifetime so
        # pty behaviour does not depend on whether a client currently has it open.
        tty.setraw(self._slave)
        os.set_blocking(self._master, False)
        self.path = os.ttyname(self._slave)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(self._stop, self._master),
            name=f"fakedev-{self.serial_number}",
            daemon=True,
        )
        self._thread.start()
        self._notify()

    @staticmethod
    def _openpty_avoiding(old_path: str) -> tuple[int, int]:
        # The OS may hand back the pty number just freed; a reconnect test needs a new path.
        rejected: list[tuple[int, int]] = []
        try:
            while True:
                master, slave = os.openpty()
                if os.ttyname(slave) != old_path:
                    return master, slave
                rejected.append((master, slave))
        finally:
            for fd in itertools.chain.from_iterable(rejected):
                os.close(fd)

    def _stop_thread(self) -> None:
        self._stop.set()
        os.write(self._wake_w, b"\0")
        if self._thread is not None:
            self._thread.join(_JOIN_S)
            self._thread = None

    def _close_pty(self) -> None:
        for fd in (self._master, self._slave):
            if fd >= 0:
                os.close(fd)
        self._master = self._slave = -1
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self)

    def _run(self, stop: threading.Event, master: int) -> None:
        while not stop.is_set():
            with self._lock:
                due = self._schedule[0][0] if self._schedule else None
            wait = _POLL_S if due is None else min(_POLL_S, max(0.0, due - time.monotonic()))
            readable, _, _ = select.select([master, self._wake_r], [], [], wait)
            if self._wake_r in readable:
                os.read(self._wake_r, _READ_CHUNK)
            if master in readable:
                try:
                    data = os.read(master, _READ_CHUNK)
                except BlockingIOError:
                    data = b""
                except OSError:
                    stop.wait(_POLL_S)
                    continue
                if data:
                    with self._lock:
                        self._on_rx(data)
            self._fire_due(stop, master)

    def _fire_due(self, stop: threading.Event, master: int) -> None:
        # Writes happen under the lock so a concurrent reset() lands between whole writes,
        # never after a stale chunk it meant to cancel.
        with self._lock:
            while self._schedule and self._schedule[0][0] <= time.monotonic():
                _, _, action = heapq.heappop(self._schedule)
                if isinstance(action, bytes):
                    self._write(stop, master, action)
                else:
                    action()

    def _write(self, stop: threading.Event, master: int, data: bytes) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + _WRITE_STALL_S
        while view and not stop.is_set():
            _, writable, _ = select.select([], [master], [], _POLL_S)
            if not writable:
                if time.monotonic() > deadline:
                    return
                continue
            try:
                view = view[os.write(master, view) :]
            except BlockingIOError:
                continue
            except OSError:
                return

    def _at(self, when: float, action: _Action) -> None:
        heapq.heappush(self._schedule, (when, next(self._seq), action))

    def _boot(self, kind: Literal["poweron", "sw", "download"]) -> None:
        now = time.monotonic()
        self._schedule.clear()
        self._rx = b""
        self._t_reset = now
        self._shell_free_at = now
        self.resets.append(kind)
        if kind == "download":
            self._mode = "bootloader"
            self._at(now, ROM_BANNER + RST_DOWNLOAD)
            return
        self._mode = "app"
        self._at(now, ROM_BANNER + (RST_POWERON if kind == "poweron" else RST_SW))
        t = now
        for delay, chunk in self.script.boot:
            t += delay * self.timescale
            self._at(t, chunk)
        self._at(t + self.script.prompt_delay * self.timescale, self.script.prompt.encode())

    def _on_rx(self, data: bytes) -> None:
        self._rx += data
        ending = self.script.line_ending
        while ending in self._rx:
            raw, self._rx = self._rx.split(ending, 1)
            # Tolerate a client that ends lines with \r\n when the shell splits on \r.
            self._on_line(raw.decode("utf-8", errors="replace").strip("\r\n"))

    def _on_line(self, line: str) -> None:
        now = time.monotonic()
        since = None if self._t_reset is None else (now - self._t_reset) / self.timescale
        if self._mode == "bootloader":
            self.received.append(RxRecord(line, "ignored", since))
            return
        if since is not None and since < self.script.blind_s:
            self.received.append(RxRecord(line, "dropped", since))
            return
        script = self.script
        words = line.split()
        name = words[0] if words else ""
        # A busy shell (`slow`) leaves later input in the UART until it gets back to reading.
        start = max(now, self._shell_free_at)
        self._at(start, line.encode() + b"\r\n")
        in_window = since is not None and script.win_start <= since <= script.win_end
        if name == "reboot" or (in_window and name in script.reboot_window_cmds):
            self.received.append(RxRecord(line, "rebooted", since))
            self._at(start, lambda: self._boot("sw"))
            return
        reply = script.replies.get(name, "command not found") if name else ""
        delay = script.slow_s if name == "slow" else 0.0
        done = start + delay * self.timescale
        body = (reply + "\r\n").encode() if reply else b""
        self._at(done, body + script.prompt.encode())
        self._shell_free_at = done
        self.received.append(RxRecord(line, "answered", since))
