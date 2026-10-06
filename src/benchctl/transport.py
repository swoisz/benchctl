"""The held serial port (design Section 4.9): pyserial behind the `types.Transport` seam."""

from __future__ import annotations

import contextlib
from typing import Any

import serial

from benchctl.errors import DeviceError, TransportLost
from benchctl.types import ModemLines

READ_TIMEOUT = 0.1
_BOOTLOADER_RESET_DELAY = 0.05


def esptool_lines(port: serial.Serial) -> ModemLines:
    """The default reset strategy: esptool's own RTS/DTR sequences on `port`.

    esptool already warns once and carries on when the port has no modem lines (a pty).
    """

    def pulse(bootloader: bool) -> None:
        # Imported on first use: esptool costs ~0.1 s to import and tests inject their own.
        from esptool.reset import ClassicReset, HardReset

        if bootloader:
            ClassicReset(port, reset_delay=_BOOTLOADER_RESET_DELAY)()
        else:
            HardReset(port)()

    return pulse


class PySerialTransport:
    """One open pyserial handle. A failed read, write or reset surfaces as TransportLost: the
    reader's cue to reconnect."""

    def __init__(self, port: serial.Serial, lines: ModemLines | None = None) -> None:
        self._port = port
        self.path: str = port.port or ""
        self._lines = lines if lines is not None else esptool_lines(port)

    @classmethod
    def open(cls, path: str, baud: int, lines: ModemLines | None = None) -> PySerialTransport:
        """Open `path` exclusively with DTR and RTS low. Raises DeviceError when the port is
        missing or held by another process."""
        port = serial.Serial()
        port.port = path
        port.baudrate = baud
        port.timeout = READ_TIMEOUT
        port.exclusive = True
        # Set before open() so the lines never rise: on the bench adapter a raised line is a
        # reset. pyserial itself ignores the ENOTTY a pty answers with.
        port.dtr = False
        port.rts = False
        try:
            port.open()
        except (serial.SerialException, OSError) as exc:
            raise DeviceError(f"cannot open {path}: {exc}", data={"port": path}) from exc
        return cls(port, lines)

    @property
    def raw(self) -> serial.Serial:
        return self._port

    def read(self, max_bytes: int, timeout: float) -> bytes:
        """Whatever is waiting, up to `max_bytes`, blocking at most `timeout` for the first
        byte; b"" on timeout."""
        port = self._port
        if not port.is_open:
            # esptool closes the port when a connect fails; pyserial's in_waiting would then
            # die with a TypeError on the missing fd instead of reporting the loss.
            raise TransportLost(f"{self.path}: port closed")
        try:
            if port.timeout != timeout:
                port.timeout = timeout
            waiting = port.in_waiting
            data = port.read(min(max_bytes, waiting) if waiting else 1)
            if data and not waiting and max_bytes > 1:
                more = port.in_waiting
                if more:
                    data += port.read(min(max_bytes - 1, more))
            return bytes(data)
        except (serial.SerialException, OSError) as exc:
            raise TransportLost(f"{self.path}: {exc}") from exc

    def write(self, data: bytes) -> None:
        try:
            self._port.write(data)
        except (serial.SerialException, OSError) as exc:
            raise TransportLost(f"{self.path}: {exc}") from exc

    def reset(self, bootloader: bool = False) -> None:
        try:
            self._lines(bootloader)
        except (serial.SerialException, OSError) as exc:
            raise TransportLost(f"{self.path}: reset failed: {exc}") from exc

    def get_settings(self) -> dict[str, Any]:
        return dict(self._port.get_settings())

    def apply_settings(self, s: dict[str, Any]) -> None:
        try:
            self._port.apply_settings(s)
            # esptool closes the port when connect fails; a closed port has no input to drop,
            # and the reader's next read reports the loss and reconnects.
            if self._port.is_open:
                self._port.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            raise TransportLost(f"{self.path}: {exc}") from exc

    def set_baud(self, baud: int) -> None:
        # A refused rate (a pty, a driver without custom bauds) is not a lost port.
        try:
            self._port.baudrate = baud
        except (serial.SerialException, OSError, ValueError) as exc:
            raise DeviceError(f"{self.path}: cannot set baud {baud}: {exc}") from exc

    def close(self) -> None:
        # Closing a port whose device already vanished can fail; it is closed either way.
        with contextlib.suppress(serial.SerialException, OSError):
            self._port.close()
