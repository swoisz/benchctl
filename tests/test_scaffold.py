"""Self-test of the scaffold: the fake device, the fixtures, and the shared modules."""

from __future__ import annotations

import itertools
import json
import os
import re
import socket
import time
import tomllib
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest
import serial

from benchctl import errors, paths
from benchctl.durations import format_duration, parse_duration
from benchctl.types import (
    Cursor,
    GateState,
    Identity,
    LogLine,
    ModemLines,
    ResetInfo,
    Status,
    Subscription,
)
from conftest import (
    PORT_INFO_KEYS,
    PROFILE_NAME,
    UNIT,
    FakeDeviceFactory,
    install_profile,
)
from fakedev import (
    APP_VERSION,
    DEFAULT_BOOT,
    ELF_SHA_PREFIX,
    FAKE_PID,
    FAKE_VID,
    PROMPT,
    SECRET,
    DeviceScript,
    FakeDevice,
)

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Long enough at timescale 0.2 (240 ms blind, 160 ms reboot window) to be immune to
# scheduler jitter, short enough to keep each test well under a second.
ROOMY_TIMESCALE = 0.2
QUIET_S = 0.05


def open_port(path: str) -> serial.Serial:
    """Open the way the daemon does (design Section 4.9): exclusive, modem lines low."""
    port = serial.Serial()
    port.port = path
    port.baudrate = 115200
    port.timeout = 0.02
    port.exclusive = True
    port.dtr = False
    port.rts = False
    port.open()
    return port


def text_of(raw: bytes) -> str:
    return ANSI.sub("", raw.decode("utf-8", errors="replace"))


def tail(text: str) -> str:
    return re.split(r"\r\n|\r|\n", text)[-1]


def read_until(port: serial.Serial, done: Callable[[str], bool], timeout: float = 1.0) -> str:
    raw = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw += port.read(port.in_waiting or 1)
        if done(text_of(raw)):
            return text_of(raw)
    raise AssertionError(f"timed out; read {text_of(raw)!r}")


def prompt_in_tail(text: str) -> bool:
    return tail(text) == PROMPT


def send(port: serial.Serial, line: str) -> None:
    port.write(line.encode() + b"\r")


# --- fake device -------------------------------------------------------------------------


def test_boot_replays_on_reset_through_exclusive_pyserial(fakedev: FakeDevice) -> None:
    with open_port(fakedev.path) as port:
        with pytest.raises(serial.SerialException):
            open_port(fakedev.path)
        assert port.read(64) == b"", "the device is silent until reset"
        fakedev.reset()
        text = read_until(port, prompt_in_tail)

    lines = text.splitlines()
    assert "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)" in lines
    assert f"I (305) app_init: ELF file SHA256:  {ELF_SHA_PREFIX}" in lines
    assert "I (420) board: die temp 41.5°C" in lines
    device_ms = [int(ms) for ms in re.findall(r"^[IWE] \((\d+)\)", text, re.MULTILINE)]
    assert len(device_ms) >= 5
    assert device_ms == sorted(device_ms)
    assert fakedev.resets == ["poweron"]


def test_boot_script_splits_writes_mid_utf8_and_between_cr_lf() -> None:
    chunks = [chunk for _, chunk in DEFAULT_BOOT]
    pairs = list(itertools.pairwise(chunks))
    assert any(a.endswith(b"\r") and b.startswith(b"\n") for a, b in pairs)

    def splits_utf8(chunk: bytes) -> bool:
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False

    assert any(splits_utf8(a) and splits_utf8(b) for a, b in pairs)
    assert any(b"\x1b[" in chunk for chunk in chunks)


def test_command_inside_blind_window_is_dropped(make_fakedev: FakeDeviceFactory) -> None:
    device = make_fakedev(timescale=ROOMY_TIMESCALE)
    with open_port(device.path) as port:
        device.reset()
        send(port, "version")
        boot = read_until(port, prompt_in_tail)
        time.sleep(QUIET_S)
        late = text_of(port.read(port.in_waiting))

        assert "Application Version" not in boot + late
        assert f"{PROMPT}version" not in boot + late
        assert [(r.line, r.outcome) for r in device.received] == [("version", "dropped")]

        send(port, "version")
        reply = read_until(port, lambda t: prompt_in_tail(t) and "Application" in t)

    assert reply.splitlines()[:2] == ["version", f"Application Version: {APP_VERSION}"]
    assert tail(reply) == PROMPT


def test_echo_completes_the_pending_prompt(fakedev: FakeDevice) -> None:
    with open_port(fakedev.path) as port:
        fakedev.reset()
        read_until(port, prompt_in_tail)
        send(port, "version")
        reply = read_until(port, lambda t: prompt_in_tail(t) and "Application" in t)

    # The prompt was already in the tail, so the shell's echo completes that line.
    assert reply.splitlines()[0] == "version"
    assert reply.endswith(f"Application Version: {APP_VERSION}\r\n{PROMPT}")


def test_vanish_breaks_reads_and_reappear_gives_a_new_path(fakedev: FakeDevice) -> None:
    old_path = fakedev.path
    with open_port(old_path) as port:
        fakedev.vanish()
        with pytest.raises(serial.SerialException):
            for _ in range(20):
                port.read(1)

    new_path = fakedev.reappear()
    assert new_path == fakedev.path
    assert new_path != old_path
    with open_port(new_path) as port:
        send(port, "version")
        reply = read_until(port, lambda t: prompt_in_tail(t) and "Application" in t)
    assert f"Application Version: {APP_VERSION}" in reply.splitlines()


def test_shell_replies(fakedev: FakeDevice) -> None:
    with open_port(fakedev.path) as port:
        send(port, "secret")
        assert f"sip_pass={SECRET}" in read_until(port, prompt_in_tail)
        send(port, "frobnicate")
        assert "command not found" in read_until(port, prompt_in_tail).splitlines()
        send(port, "")
        assert read_until(port, prompt_in_tail) == f"\r\n{PROMPT}"

        start = time.monotonic()
        send(port, "slow")
        assert "slow: done" in read_until(port, prompt_in_tail)
        assert time.monotonic() - start >= 0.9 * fakedev.script.slow_s * fakedev.timescale


def test_reboot_command_is_a_software_reset(fakedev: FakeDevice) -> None:
    with open_port(fakedev.path) as port:
        send(port, "reboot")
        text = read_until(port, lambda t: prompt_in_tail(t) and "rst:" in t)
    assert "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)" in text.splitlines()
    assert fakedev.resets == ["sw"]


def test_reboot_window_command_reboots(make_fakedev: FakeDeviceFactory) -> None:
    script = DeviceScript(reboot_window_cmds=frozenset({"wifi"}))
    device = make_fakedev(script, timescale=ROOMY_TIMESCALE)
    with open_port(device.path) as port:
        device.reset()
        read_until(port, prompt_in_tail)
        send(port, "wifi status")
        text = read_until(port, lambda t: "RTC_SW_CPU_RST" in t and prompt_in_tail(t))
    assert text.splitlines()[0] == "wifi status"
    assert device.resets == ["poweron", "sw"]
    assert device.received[-1].outcome == "rebooted"


def test_bootloader_reset_enters_download_mode(fakedev: FakeDevice) -> None:
    with open_port(fakedev.path) as port:
        fakedev.reset(bootloader=True)
        text = read_until(port, lambda t: "waiting for download" in t)
        send(port, "version")
        time.sleep(QUIET_S)
        late = port.read(port.in_waiting)
    assert "rst:0x1 (POWERON),boot:0x0 (DOWNLOAD(USB/UART0))" in text.splitlines()
    assert late == b""
    assert fakedev.received[-1].outcome == "ignored"


def test_port_info_describes_the_current_path(fakedev: FakeDevice) -> None:
    info = fakedev.port_info()
    assert info.device == fakedev.path
    assert info.serial_number == fakedev.serial_number
    fakedev.reappear()
    assert fakedev.port_info().device == fakedev.path
    assert fakedev.port_info().serial_number == info.serial_number


# --- fixtures ----------------------------------------------------------------------------


def test_dirs_are_isolated_and_exported(config_dir: Path, state_dir: Path) -> None:
    assert os.environ["BENCHCTL_CONFIG_DIR"] == str(config_dir)
    assert os.environ["BENCHCTL_STATE_DIR"] == str(state_dir)
    assert paths.config_dir() == config_dir
    assert paths.state_dir() == state_dir
    assert paths.socket_path(UNIT) == state_dir / UNIT / "sock"
    assert paths.runs_dir(UNIT) == state_dir / "runs" / UNIT


def test_state_dir_fits_a_unix_socket(state_dir: Path) -> None:
    sock_path = paths.socket_path("a-long-unit-name-01")
    sock_path.parent.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(sock_path))


def test_registry_matches_the_fake_by_serial(registry: Path, fakedev: FakeDevice) -> None:
    unit = tomllib.loads(registry.read_text())["units"][UNIT]
    match = {"serial": fakedev.serial_number}
    assert unit == {"profile": PROFILE_NAME, "console": {"match": match}}


def test_ports_file_follows_vanish_and_reappear(ports_file: Path, fakedev: FakeDevice) -> None:
    def listed() -> list[dict[str, object]]:
        entries: list[dict[str, object]] = json.loads(ports_file.read_text())
        return entries

    assert os.environ["BENCHCTL_PORTS_FILE"] == str(ports_file)
    [entry] = listed()
    assert tuple(entry) == PORT_INFO_KEYS
    assert entry["device"] == fakedev.path
    assert (entry["vid"], entry["pid"]) == (FAKE_VID, FAKE_PID)
    assert entry["serial_number"] == fakedev.serial_number

    old_path = fakedev.path
    fakedev.vanish()
    assert not fakedev.present
    assert listed() == []

    new_path = fakedev.reappear()
    assert fakedev.present
    assert [e["device"] for e in listed()] == [new_path]
    assert new_path != old_path


def test_ports_file_lists_every_present_fake(
    ports_file: Path, make_fakedev: FakeDeviceFactory
) -> None:
    first, second = make_fakedev(), make_fakedev()
    entries = json.loads(ports_file.read_text())
    assert [(e["device"], e["serial_number"]) for e in entries] == [
        (first.path, first.serial_number),
        (second.path, second.serial_number),
    ]
    first.close()
    assert [e["device"] for e in json.loads(ports_file.read_text())] == [second.path]


def test_fake_lines_reset_the_fake(fake_lines: ModemLines, fakedev: FakeDevice) -> None:
    fake_lines(False)
    fake_lines(True)
    assert fakedev.resets == ["poweron", "download"]


SECTION_7_3_PROFILE = r"""
[profile]
name = "esp-idf"
chip = "esp32s3"

[console]
line_ending = "\r"
prompt = "esp32> "

[boot]
ready = "esp32> "
settle = 2.5
reset_patterns = []

[[identity.field]]
name = "fw_version"
source = "cmd"
send = "version"
regex = 'Application Version:\s*(\S+)'

[commands.version]
description = "Firmware version"
send = "version"
capture = true

[[deny]]
pattern = '^sip disable\b'
reason = "crashes from the console task"

[env_gate]
require = ['got ip', 'MQTT connected']
"""


def test_install_profile_fits_the_example_to_the_fake(config_dir: Path, tmp_path: Path) -> None:
    source = tmp_path / "profile.toml"
    source.write_text(SECTION_7_3_PROFILE)
    original = tomllib.loads(SECTION_7_3_PROFILE)

    installed = install_profile(config_dir, source, timescale=0.05)

    assert installed == config_dir / "profiles" / PROFILE_NAME / "profile.toml"
    data = tomllib.loads(installed.read_text())
    assert data["console"] == {"line_ending": "\r", "prompt": PROMPT}
    assert "ready" not in data["boot"]
    assert data["boot"]["settle"] == pytest.approx(0.125)
    assert data["boot"]["ready_timeout"] == pytest.approx(1.0)
    assert data["boot"]["probe_interval"] == pytest.approx(0.05)
    assert data["redact"] == [{"name": "sip_pass", "pattern": r'(sip_pass["\s:=]+)[^",\s]+'}]
    for untouched in ("identity", "commands", "deny", "env_gate"):
        assert data[untouched] == original[untouched]


def test_profile_fixture_loads_the_example(request: pytest.FixtureRequest) -> None:
    pytest.importorskip("benchctl.profile", reason="profile loader lands with U3")
    assert request.getfixturevalue("profile") is not None


# --- shared modules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("timeout", 1),
        ("assert", 1),
        ("usage", 2),
        ("busy", 3),
        ("device", 4),
        ("internal", 4),
        ("slow_client", 4),
        ("protocol", 4),
    ],
)
def test_exit_codes(code: str, status: int) -> None:
    assert errors.exit_code(code) == status


def test_errors_round_trip_the_wire() -> None:
    busy = errors.BusyError("busy: held by a@b since t", data={"owner": "a@b"})
    rebuilt = errors.from_dict(json.loads(json.dumps(busy.to_dict())))
    assert isinstance(rebuilt, errors.BusyError)
    assert (rebuilt.code, rebuilt.message, rebuilt.data) == ("busy", busy.message, busy.data)
    slow = errors.from_dict({"code": "slow_client", "message": "behind"})
    assert (type(slow), slow.code, slow.exit_code) == (errors.BenchctlError, "slow_client", 4)
    assert errors.TransportLost("gone").exit_code == 4


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("250ms", 0.25), ("50s", 50.0), ("15m", 900.0), ("1.5h", 5400.0), ("60", 60.0), (8, 8.0)],
)
def test_parse_duration(text: str | int, seconds: float) -> None:
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "5x", "-5s", " 5s", "5 s", "1e3", True, -1])
def test_parse_duration_rejects(text: str | int) -> None:
    with pytest.raises(errors.UsageError):
        parse_duration(text)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.25, "0.25s"), (50, "50s"), (1500, "25m"), (1420, "23m40s"), (3900, "1h5m")],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


def test_cursor_grammar() -> None:
    assert Cursor.parse("attach") == Cursor("attach")
    assert Cursor.parse("boot") == Cursor("boot")
    assert Cursor.parse("812") == Cursor("offset", offset=812)
    assert Cursor.parse("after_flash") == Cursor("mark", mark="after_flash")
    for bad in ("", " ", "-3"):
        with pytest.raises(errors.UsageError):
            Cursor.parse(bad)


def test_subscription_is_bounded_and_never_blocks_the_producer() -> None:
    feed = Subscription(maxsize=2)
    line = LogLine(offset=0, t=0.0, ts="00:00:00.000", text="hello", meta=False)
    assert feed.offer(line)
    assert feed.offer({"type": "mark", "name": "m"})
    assert not feed.offer(line)
    assert feed.closed
    with pytest.raises(errors.BenchctlError) as raised:
        feed.get(timeout=0.01)
    assert raised.value.code == "slow_client"


def test_subscription_close_and_timeout() -> None:
    feed = Subscription()
    assert feed.get(timeout=0.01) is None
    feed.close()
    assert not feed.offer({"type": "mark"})


def test_status_is_json_serialisable() -> None:
    reset = ResetInfo(
        offset=3,
        t=0.1,
        code=1,
        reason="POWERON",
        boot_mode="SPI_FAST_FLASH_BOOT",
        elf_sha=ELF_SHA_PREFIX,
        expected=True,
        cause="reset",
        context=[],
    )
    status = Status(
        unit=UNIT,
        pid=1,
        attached_at="2026-10-06T12:00:00+00:00",
        port="/dev/ttys001",
        connected=True,
        baud=115200,
        lease={"owner": "a@b"},
        identity=Identity(fields={"mac": "aa"}, verified=False, mismatches={"mac": ("bb", "aa")}),
        last_reset=reset,
        offset=40,
        gate=GateState(state="ready", since=1.0, method="pattern"),
        session_dir="/s",
    )
    decoded = json.loads(json.dumps(asdict(status)))
    assert decoded["last_reset"]["reason"] == "POWERON"
    assert decoded["identity"]["mismatches"] == {"mac": ["bb", "aa"]}


def test_unit_names_are_path_safe() -> None:
    assert paths.check_unit_name("bench-1.a_b") == "bench-1.a_b"
    for bad in ("runs", "../x", "a/b", "", ".hidden"):
        with pytest.raises(errors.UsageError):
            paths.check_unit_name(bad)
