"""End-to-end fixtures (design Section 6.4): the real CLI in a subprocess, the real daemon it
auto-attaches (through `bridged_daemon.py`), and fake devices found through the ports file.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from benchctl import paths
from benchctl.client import DAEMON_ARGV_ENV
from benchctl.lease import OWNER_ENV
from conftest import PROFILE_NAME, FakeDeviceFactory, dump_toml, install_profile
from fake_daemon import reap_daemons
from fakedev import DeviceScript, FakeDevice
from integration.bridged_daemon import ACK, RESET_DIR_ENV, bridge_socket, launcher_argv

OWNER = "integration@bench"
# Generous bounds: these hold the suite together on a loaded machine, not set its pace.
CLI_TIMEOUT_S = 60.0
EVENT_TIMEOUT_S = 15.0
POLL_S = 0.05
_ACCEPT_TICK_S = 0.2
_RECV_TIMEOUT_S = 2.0
_REQUEST_BYTES = 16
_BACKLOG = 8
_JOIN_S = 1.0
_BRIDGE_DIR = "_bridge"  # not a valid unit name, so it can never collide with one


class ResetBridge:
    """The test-process end of `bridged_daemon.bridged_lines`: one AF_UNIX listener per unit
    that resets the unit's fake on request and records every pulse."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.pulses: dict[str, list[bool]] = {}
        self._stop = threading.Event()
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []

    def serve(self, unit: str, device: FakeDevice) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(bridge_socket(self.directory, unit)))
        listener.listen(_BACKLOG)
        listener.settimeout(_ACCEPT_TICK_S)
        self.pulses[unit] = []
        thread = threading.Thread(
            target=self._accept, args=(listener, unit, device), name=f"bridge-{unit}", daemon=True
        )
        self._listeners.append(listener)
        self._threads.append(thread)
        thread.start()

    def _accept(self, listener: socket.socket, unit: str, device: FakeDevice) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(_RECV_TIMEOUT_S)
                try:
                    request = conn.recv(_REQUEST_BYTES)
                except OSError:
                    continue
                if not request:
                    continue
                bootloader = request.strip() == b"1"
                self.pulses[unit].append(bootloader)
                device.reset(bootloader)
                with contextlib.suppress(OSError):
                    conn.sendall(ACK)

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(_JOIN_S)
        for listener in self._listeners:
            listener.close()


@dataclass
class Call:
    """One CLI invocation and what it printed."""

    args: tuple[str, ...]
    code: int
    stdout: str
    stderr: str

    @property
    def data(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.stdout)
        return data


@dataclass
class Bench:
    """Registered units on fake devices, and the CLI that drives them."""

    state: Path
    config: Path
    workdir: Path
    make_device: FakeDeviceFactory
    bridge: ResetBridge
    devices: dict[str, FakeDevice] = field(default_factory=dict)
    expect: dict[str, dict[str, str]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    _calls_lock: threading.Lock = field(default_factory=threading.Lock)

    # --- units -----------------------------------------------------------------------

    def add_unit(
        self,
        unit: str,
        script: DeviceScript | None = None,
        *,
        expect: Mapping[str, str] | None = None,
    ) -> FakeDevice:
        """A new fake, registered as `unit` by its serial number, with its reset bridged."""
        device = self.make_device(script)
        self.devices[unit] = device
        if expect:
            self.expect[unit] = dict(expect)
        self.bridge.serve(unit, device)
        self._write_units()
        return device

    def _write_units(self) -> None:
        units: dict[str, Any] = {}
        for name, device in self.devices.items():
            entry: dict[str, Any] = {
                "profile": PROFILE_NAME,
                "console": {"match": {"serial": device.serial_number}},
            }
            if name in self.expect:
                entry["expect"] = self.expect[name]
            units[name] = entry
        text = dump_toml({"units": units})
        (self.config / "units.toml").write_text(text, encoding="utf-8")

    # --- the CLI ---------------------------------------------------------------------

    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "benchctl", *args]

    def env(self) -> dict[str, str]:
        # Unbuffered, so a run's human prompt reaches the stderr pipe while the run waits.
        return {**os.environ, "PYTHONUNBUFFERED": "1"}

    def run(self, *args: str, timeout: float = CLI_TIMEOUT_S) -> Call:
        try:
            done = subprocess.run(
                self.argv(*args),
                cwd=self.workdir,
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            pytest.fail(f"benchctl {' '.join(args)} ran past {timeout:g} s: {exc.stderr!r}")
        return self.record(args, done.returncode, done.stdout, done.stderr)

    def record(self, args: tuple[str, ...], code: int, stdout: str, stderr: str) -> Call:
        call = Call(args, code, stdout, stderr)
        with self._calls_lock:
            self.calls.append(call)
        return call

    def cli(self, *args: str, code: int = 0, timeout: float = CLI_TIMEOUT_S) -> dict[str, Any]:
        """`benchctl <args> --json`: the parsed stdout, after checking the exit code."""
        call = self.run(*args, "--json", timeout=timeout)
        assert call.code == code, (call.args, call.code, call.stdout, call.stderr)
        return call.data

    # --- what the daemon left on disk ------------------------------------------------

    def session_dir(self, unit: str) -> Path:
        """The unit's newest session directory (one per attach)."""
        sessions = sorted(paths.sessions_dir(unit).iterdir())
        assert sessions, f"{unit} has no session"
        return sessions[-1]

    def events(self, unit: str) -> list[dict[str, Any]]:
        text = (self.session_dir(unit) / "events.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def serial_log(self, unit: str) -> str:
        return (self.session_dir(unit) / "serial.log").read_text(encoding="utf-8")

    def daemon_log(self, unit: str) -> str:
        return paths.daemon_log_path(unit).read_text(encoding="utf-8")

    def await_event(
        self,
        unit: str,
        match: Callable[[dict[str, Any]], bool],
        timeout: float = EVENT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """The first session event `match` accepts, polling until `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            found = [event for event in self.events(unit) if match(event)]
            if found:
                return found[0]
            assert time.monotonic() < deadline, f"no matching event for {unit} in {timeout:g} s"
            time.sleep(POLL_S)


def shell_lines(device: FakeDevice) -> list[str]:
    """The lines the fake's shell received, without gate probes (a bare line ending), which
    a slow machine can provoke after any boot."""
    return [record.line for record in device.received if record.line]


@pytest.fixture
def bench(
    config_dir: Path,
    state_dir: Path,
    tmp_path: Path,
    make_fakedev: FakeDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Bench]:
    """The example profile fitted to the fakes, the real daemon behind BENCHCTL_DAEMON_ARGV
    (only to bridge the reset), and a scratch cwd. Units are added by the test."""
    install_profile(config_dir)
    # Under the short state dir: AF_UNIX paths are capped at 104 bytes.
    bridge = ResetBridge(state_dir / _BRIDGE_DIR)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv(DAEMON_ARGV_ENV, launcher_argv())
    monkeypatch.setenv(RESET_DIR_ENV, str(bridge.directory))
    monkeypatch.setenv(OWNER_ENV, OWNER)
    try:
        yield Bench(state_dir, config_dir, workdir, make_fakedev, bridge)
    finally:
        # Before make_fakedev closes the ptys and state_dir is removed.
        reap_daemons(state_dir)
        bridge.close()
