"""capture --reset in daemon and direct mode (design Sections 4.11 and 8, U6)."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest
import serial

from benchctl.capture import capture_direct, capture_on_session
from benchctl.errors import DeviceError, UsageError
from benchctl.types import LogLine, ModemLines
from fakedev import ELF_SHA_PREFIX, FakeDevice
from test_flash import BOOT_RESET, FakeSession

# The default boot script ends ~0.06 s after the reset at the fixture's timescale.
CAPTURE_S = 0.4
# Bounds, not equalities: scheduler jitter on a loaded CI host.
SLACK_S = 0.25
TS = re.compile(r"\d\d:\d\d:\d\d\.\d{3}")


def texts(lines: list[LogLine]) -> list[str]:
    return [line.text for line in lines]


def test_direct_capture_returns_the_boot_with_t_since_reset(
    fakedev: FakeDevice, fake_lines: ModemLines
) -> None:
    started = time.monotonic()
    lines = capture_direct(fakedev.path, 115200, CAPTURE_S, fake_lines)
    elapsed = time.monotonic() - started

    assert fakedev.resets == ["poweron"]
    assert CAPTURE_S <= elapsed < CAPTURE_S + SLACK_S
    assert texts(lines)[:3] == [
        "ESP-ROM:esp32s3-20210327",
        "Build:Mar 27 2021",
        "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    ]
    assert f"I (305) app_init: ELF file SHA256:  {ELF_SHA_PREFIX}" in texts(lines)
    assert "I (420) board: die temp 41.5°C" in texts(lines)
    assert texts(lines)[-1] == "I (1150) console: shell started"
    assert not any("\x1b" in line.text for line in lines)
    # The prompt is unterminated, so it never becomes a line.
    assert not any("demo>" in line.text for line in lines)
    assert [line.offset for line in lines] == list(range(len(lines)))
    assert not any(line.meta for line in lines)
    assert all(TS.fullmatch(line.ts) for line in lines)
    times = [line.t for line in lines]
    assert times == sorted(times)
    assert 0 <= times[0] < SLACK_S
    assert times[-1] <= CAPTURE_S + SLACK_S


def test_direct_capture_measures_t_from_the_reset_not_the_open(
    fakedev: FakeDevice, fake_lines: ModemLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    delay = 0.3
    real_open = serial.Serial.open

    def slow_open(port: serial.Serial) -> None:
        time.sleep(delay)
        real_open(port)

    monkeypatch.setattr(serial.Serial, "open", slow_open)
    lines = capture_direct(fakedev.path, 115200, 0.2, fake_lines)
    assert lines[0].text.startswith("ESP-ROM:")
    assert lines[0].t < delay


def test_direct_capture_on_a_port_a_daemon_holds_is_port_busy(
    fakedev: FakeDevice, fake_lines: ModemLines
) -> None:
    with serial.Serial(fakedev.path, exclusive=True), pytest.raises(DeviceError) as caught:
        capture_direct(fakedev.path, 115200, CAPTURE_S, fake_lines)
    assert caught.value.message.startswith(f"port busy: {fakedev.path}")
    assert caught.value.exit_code == 4
    assert fakedev.resets == []


def test_direct_capture_reports_a_vanished_port(
    fakedev: FakeDevice, fake_lines: ModemLines
) -> None:
    timer = threading.Timer(0.1, fakedev.vanish)
    timer.start()
    try:
        with pytest.raises(DeviceError, match="port lost during capture") as caught:
            capture_direct(fakedev.path, 115200, CAPTURE_S, fake_lines)
    finally:
        timer.join()
    assert caught.value.exit_code == 4


@pytest.mark.parametrize("seconds", [0, -1.0])
def test_capture_needs_a_positive_duration(
    fakedev: FakeDevice, tmp_path: Path, seconds: float
) -> None:
    with pytest.raises(UsageError):
        capture_direct(fakedev.path, 115200, seconds)
    with pytest.raises(UsageError):
        capture_on_session(FakeSession(tmp_path), seconds)


def test_session_capture_resets_then_returns_the_window_from_the_reset_request(
    tmp_path: Path,
) -> None:
    session = FakeSession(tmp_path)
    session.append("I (900) main: before the capture")
    session.append("I (950) main: still before")
    late = threading.Timer(0.05, session.append, args=("I (1150) console: shell started",))

    def device_reboots() -> None:
        session.append("## reset requested bootloader=False", meta=True)
        session.append("ESP-ROM:esp32s3-20210327")
        session.append("rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)")
        late.start()

    session.on_reset = device_reboots
    seconds = 0.2
    started = time.monotonic()
    try:
        lines, reset = capture_on_session(session, seconds)
    finally:
        late.join()
    elapsed = time.monotonic() - started

    assert seconds <= elapsed < seconds + SLACK_S
    assert session.calls == [f"reset:False:{seconds}:reset"]
    assert reset is BOOT_RESET
    assert texts(lines) == [
        "## reset requested bootloader=False",
        "ESP-ROM:esp32s3-20210327",
        "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
        "I (1150) console: shell started",
    ]
    assert lines[0].offset == 2
    assert session.log_calls == [(2, 6, None, 4)]


@pytest.mark.parametrize(("seconds", "wait_rst"), [(0.03, 0.03), (60.0, 5.0)])
def test_session_capture_bounds_the_reset_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: float, wait_rst: float
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    session = FakeSession(tmp_path)
    session.reset_result = None
    lines, reset = capture_on_session(session, seconds)
    assert session.calls == [f"reset:False:{wait_rst}:reset"]
    assert seconds - SLACK_S < slept[0] <= seconds
    assert reset is None
    assert lines == []
