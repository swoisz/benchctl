"""PySerialTransport against the fake device's pty (design Section 4.9)."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from benchctl.errors import DeviceError, TransportLost
from benchctl.transport import PySerialTransport, esptool_lines
from benchctl.types import ModemLines, Transport
from fakedev import PROMPT, FakeDevice


@pytest.fixture
def transport(fakedev: FakeDevice, fake_lines: ModemLines) -> Iterator[PySerialTransport]:
    opened = PySerialTransport.open(fakedev.path, 115200, fake_lines)
    yield opened
    opened.close()


def read_until(transport: Transport, marker: bytes, timeout: float = 1.0) -> bytes:
    data = b""
    deadline = time.monotonic() + timeout
    while marker not in data and time.monotonic() < deadline:
        data += transport.read(4096, 0.05)
    assert marker in data, f"no {marker!r} in {data!r}"
    return data


def test_open_is_exclusive_and_reads_time_out_empty(
    fakedev: FakeDevice, transport: PySerialTransport
) -> None:
    with pytest.raises(DeviceError, match="cannot open"):
        PySerialTransport.open(fakedev.path, 115200)
    started = time.monotonic()
    assert transport.read(4096, 0.05) == b""
    assert time.monotonic() - started < 0.5
    assert transport.path == fakedev.path
    assert transport.raw.is_open


def test_open_missing_port_is_a_device_error() -> None:
    with pytest.raises(DeviceError, match="cannot open /dev/benchctl-none"):
        PySerialTransport.open("/dev/benchctl-none", 115200)


def test_reset_goes_through_the_injected_lines(
    fakedev: FakeDevice, transport: PySerialTransport
) -> None:
    transport.reset()
    data = read_until(transport, PROMPT.encode())
    assert b"rst:0x1 (POWERON)" in data
    transport.reset(bootloader=True)
    assert b"waiting for download" in read_until(transport, b"waiting for download")
    assert fakedev.resets == ["poweron", "download"]


def test_write_reaches_the_device(fakedev: FakeDevice, transport: PySerialTransport) -> None:
    transport.reset()
    read_until(transport, PROMPT.encode())
    transport.write(b"version\r")
    assert b"Application Version" in read_until(transport, b"Application Version")
    assert [record.line for record in fakedev.received] == ["version"]


def test_settings_round_trip_restores_baud_and_discards_input(
    transport: PySerialTransport,
) -> None:
    saved = transport.get_settings()
    transport.reset()
    time.sleep(0.15)  # the whole boot is buffered, unread
    # A standard rate: a pty refuses the custom-rate ioctl a 921600 needs on macOS.
    transport.set_baud(230400)
    assert transport.raw.baudrate == 230400
    transport.apply_settings(saved)
    assert transport.raw.baudrate == 115200
    assert transport.read(4096, 0.05) == b""


def test_vanished_port_raises_transport_lost(
    fakedev: FakeDevice, transport: PySerialTransport
) -> None:
    fakedev.vanish()
    with pytest.raises(TransportLost):
        for _ in range(20):
            transport.read(4096, 0.05)
    with pytest.raises(TransportLost):
        transport.write(b"x")
    transport.close()
    transport.close()


def test_a_port_closed_underneath_reads_as_lost_and_settings_still_apply(
    transport: PySerialTransport,
) -> None:
    # esptool's connect() closes the handle when it fails, while the reader is parked.
    saved = transport.get_settings()
    transport.raw.close()
    transport.apply_settings(saved)
    with pytest.raises(TransportLost, match="port closed"):
        transport.read(4096, 0.05)


def test_default_lines_survive_a_port_without_modem_lines(fakedev: FakeDevice) -> None:
    # A pty answers modem-line ioctls with ENOTTY; esptool warns once and carries on.
    opened = PySerialTransport.open(fakedev.path, 115200)
    try:
        esptool_lines(opened.raw)(False)
        opened.reset(bootloader=True)
    finally:
        opened.close()
    assert fakedev.resets == []
