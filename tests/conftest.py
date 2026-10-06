"""Shared fixtures (design Section 6.1). Frozen with the scaffold; units report changes."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import pytest

from benchctl.types import ModemLines
from fakedev import PROMPT, DeviceScript, FakeDevice

if TYPE_CHECKING:
    from benchctl.profile import Profile

TIMESCALE = 0.05
UNIT = "u1"
PROFILE_NAME = "esp-idf"
EXAMPLE_PROFILE = Path(__file__).resolve().parent.parent / "examples" / "profile.toml"
SIP_PASS_REDACT = r'(sip_pass["\s:=]+)[^",\s]+'
# The ListPortInfo attributes a Section 7.4 ports file carries.
PORT_INFO_KEYS = (
    "device",
    "vid",
    "pid",
    "serial_number",
    "location",
    "manufacturer",
    "product",
    "description",
)

# [boot] timings the profile fixture scales by the fake's timescale, with their Section 7.3
# defaults for when the example leaves them out.
_SCALED_BOOT_TIMINGS = {"settle": 2.5, "ready_timeout": 20.0, "probe_interval": 1.0}
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class FakeDeviceFactory(Protocol):
    def __call__(
        self, script: DeviceScript | None = None, timescale: float = TIMESCALE
    ) -> FakeDevice: ...


def dump_toml(data: Mapping[str, Any]) -> str:
    """Serialise the TOML subset profiles and units use (tables, arrays of tables, scalars).

    tomllib only reads; this keeps the fixtures free of a TOML-writer dependency.
    """
    return _dump_table(data, ())


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes.
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{" + items + "}"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def _dump_table(data: Mapping[str, Any], prefix: tuple[str, ...]) -> str:
    out = ""
    nested: list[tuple[str, Any]] = []
    for key, value in data.items():
        is_table_array = (
            isinstance(value, list) and value and all(isinstance(v, dict) for v in value)
        )
        if isinstance(value, dict) or is_table_array:
            nested.append((key, value))
        else:
            out += f"{_toml_key(key)} = {_toml_value(value)}\n"
    for key, value in nested:
        header = ".".join(_toml_key(part) for part in (*prefix, key))
        if isinstance(value, dict):
            out += f"\n[{header}]\n" + _dump_table(value, (*prefix, key))
        else:
            for item in value:
                out += f"\n[[{header}]]\n" + _dump_table(item, (*prefix, key))
    return out


def install_profile(
    config_dir: Path, source: Path = EXAMPLE_PROFILE, timescale: float = TIMESCALE
) -> Path:
    """Install `source` as profile PROFILE_NAME, fitted to the fake device.

    Boot timings scale with the fake; the prompt is the fake's and `boot.ready` falls back to
    it; a `sip_pass` redaction is guaranteed so redaction tests have something to hit.
    """
    data: dict[str, Any] = tomllib.loads(source.read_text(encoding="utf-8"))
    data.setdefault("profile", {})["name"] = PROFILE_NAME
    data.setdefault("console", {})["prompt"] = PROMPT
    boot = data.setdefault("boot", {})
    boot.pop("ready", None)
    for key, default in _SCALED_BOOT_TIMINGS.items():
        boot[key] = float(boot.get(key, default)) * timescale
    redact = data.setdefault("redact", [])
    if not any(rule.get("name") == "sip_pass" for rule in redact):
        redact.append({"name": "sip_pass", "pattern": SIP_PASS_REDACT})
    path = config_dir / "profiles" / PROFILE_NAME / "profile.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(data), encoding="utf-8")
    return path


def write_ports_file(path: Path, devices: Iterable[FakeDevice]) -> None:
    """List every present fake in the Section 7.4 ports file.

    Replaced atomically: a daemon re-reads it on every reconnect attempt and must never see
    half a list.
    """
    entries = [
        {key: getattr(device.port_info(), key) for key in PORT_INFO_KEYS}
        for device in devices
        if device.present
    ]
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    os.replace(staging, path)


def _short_tempdir() -> Path:
    # AF_UNIX socket paths are capped at 104 bytes on macOS. pytest's tmp_path, or a long
    # TMPDIR, can pass that before <unit>/sock is appended, so prefer /tmp.
    try:
        return Path(tempfile.mkdtemp(prefix="bc-", dir="/tmp"))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="bc-"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's BENCHCTL_* settings never leak into a test."""
    for name in list(os.environ):
        if name.startswith("BENCHCTL_"):
            monkeypatch.delenv(name)


@pytest.fixture
def config_dir(_clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    monkeypatch.setenv("BENCHCTL_CONFIG_DIR", str(path))
    return path


@pytest.fixture
def state_dir(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = _short_tempdir()
    monkeypatch.setenv("BENCHCTL_STATE_DIR", str(path))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_dirs(config_dir: Path, state_dir: Path) -> None:
    """No test touches the real ~/.config/benchctl or ~/.local/state/benchctl."""


@pytest.fixture
def ports_file(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The BENCHCTL_PORTS_FILE every fake from `make_fakedev` keeps current."""
    path = config_dir / "ports.json"
    write_ports_file(path, [])
    monkeypatch.setenv("BENCHCTL_PORTS_FILE", str(path))
    return path


@pytest.fixture
def make_fakedev(ports_file: Path) -> Iterator[FakeDeviceFactory]:
    devices: list[FakeDevice] = []

    def changed(device: FakeDevice) -> None:
        # The first call comes from inside the constructor, before `make` could record it.
        if device not in devices:
            devices.append(device)
        write_ports_file(ports_file, devices)

    def make(script: DeviceScript | None = None, timescale: float = TIMESCALE) -> FakeDevice:
        return FakeDevice(script, timescale, on_change=changed)

    yield make
    for device in devices:
        device.close()


@pytest.fixture
def fakedev(make_fakedev: FakeDeviceFactory) -> FakeDevice:
    return make_fakedev()


@pytest.fixture
def registry(config_dir: Path, fakedev: FakeDevice) -> Path:
    """`units.toml` registering unit UNIT by the fake's serial number, which (unlike its
    path) survives `reappear()`."""
    path = config_dir / "units.toml"
    unit = {"profile": PROFILE_NAME, "console": {"match": {"serial": fakedev.serial_number}}}
    path.write_text(dump_toml({"units": {UNIT: unit}}), encoding="utf-8")
    return path


@pytest.fixture
def profile(config_dir: Path, tmp_path: Path) -> Profile:
    """examples/profile.toml installed by `install_profile` and loaded through U3's loader."""
    # Imported here so the scaffold's own tests run before U3 lands.
    from benchctl.profile import load_profile

    return load_profile(str(install_profile(config_dir)), tmp_path, config_dir)


@pytest.fixture
def fake_lines(fakedev: FakeDevice) -> ModemLines:
    """Reset strategy bound to the fake: True enters its ROM download mode."""
    return fakedev.reset
