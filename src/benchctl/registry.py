"""`units.toml` and serial-port matching (design Section 7.4)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from serial.tools import list_ports as _list_ports
from serial.tools.list_ports_common import ListPortInfo

from benchctl import paths
from benchctl.errors import DeviceError, UsageError
from benchctl.profile import Checker, Table, load_toml

UNITS_FILE = "units.toml"
PORTS_FILE_ENV = "BENCHCTL_PORTS_FILE"
MATCH_KEYS = ("serial", "location", "vid", "pid", "product", "device")
# Match keys compared for equality against a ListPortInfo attribute of another name.
_EXACT_ATTRS = {"serial": "serial_number", "location": "location", "product": "product"}
_PORT_TEXT_ATTRS = ("serial_number", "location", "manufacturer", "product", "description")
_USB_ID = re.compile(r"(?:0[xX])?([0-9a-fA-F]{1,4})")
_UNIT_KEYS = ("profile", "console", "expect")
_ID_FIELD = "id_field"


@dataclass(frozen=True, slots=True)
class Unit:
    name: str
    match: dict[str, str]  # vid/pid normalised to 4 lowercase hex digits
    profile: str | None = None
    expect: dict[str, str] = field(default_factory=dict)  # identity field -> expected value
    id_field: str | None = None  # the identity field that becomes events' `dev`


@dataclass(frozen=True, slots=True)
class Registry:
    path: Path
    units: dict[str, Unit] = field(default_factory=dict)

    def unit(self, name: str | None) -> Unit:
        """`name`, or the only registered unit when `name` is None. Raises UsageError."""
        if name is None:
            if len(self.units) == 1:
                return next(iter(self.units.values()))
            if not self.units:
                raise UsageError(f"no units registered in {self.path}")
            raise UsageError(f"pass --unit: {self.path} registers {', '.join(self.units)}")
        try:
            return self.units[name]
        except KeyError:
            known = ", ".join(self.units) or "none"
            raise UsageError(f"unknown unit {name!r} (registered: {known})") from None


def usb_id(text: str) -> str:
    """A vid/pid as `f"{id:04x}"`: `"303A"`, `"0x303a"` and `"303a"` are one id."""
    match = _USB_ID.fullmatch(text)
    if match is None:
        raise UsageError(f"invalid USB id {text!r}: expected up to 4 hex digits")
    return f"{int(match.group(1), 16):04x}"


def _match_spec(t: Table) -> dict[str, str]:
    spec: dict[str, str] = {}
    for key, value in t.data.items():
        if key not in MATCH_KEYS:
            continue  # reported as an unknown key
        if key in ("vid", "pid"):
            # Strings only: a TOML integer 6010 and 0x6010 are different ids once parsed.
            if isinstance(value, str) and _USB_ID.fullmatch(value):
                spec[key] = usb_id(value)
                continue
            t.error(key, f'expected up to 4 hex digits as a string such as "0403", got {value!r}')
        elif isinstance(value, str) and value:
            spec[key] = value
        else:
            t.error(key, "expected a non-empty string")
    return spec


def _unit(name: str, t: Table) -> Unit:
    try:
        paths.check_unit_name(name)
    except UsageError as exc:
        t.error(None, exc.message)
    console = t.table("console", ("match",), required=True)
    match = console.table("match", MATCH_KEYS, required=t.has("console"))
    if isinstance(console.data.get("match"), dict) and not match.data:
        match.error(None, f"needs at least one of {', '.join(MATCH_KEYS)}")
    expect = t.text_map("expect")
    id_field = expect.pop(_ID_FIELD, None)
    return Unit(
        name=name,
        match=_match_spec(match),
        profile=t.opt_text("profile", nonempty=True),
        expect=expect,
        id_field=id_field,
    )


def build_registry(data: Mapping[str, Any], path: Path) -> Registry:
    """Validate parsed `units.toml`. Raises ConfigError listing every problem."""
    checker = Checker(path)
    root = Table(checker, data, "", ("units",))
    units_table = root.table("units", None)
    units = {name: _unit(name, units_table.table(name, _UNIT_KEYS)) for name in units_table.data}
    checker.done()
    return Registry(path=path, units=units)


def read_registry(path: Path) -> Registry:
    """The registry in `path`. Raises ConfigError."""
    return build_registry(load_toml(path), path)


def load_registry(config_dir: Path) -> Registry:
    """`<config>/units.toml`; an absent file is an empty registry."""
    path = config_dir / UNITS_FILE
    if not path.exists():
        return Registry(path=path)
    return read_registry(path)


# --- ports -------------------------------------------------------------------------------


def _port_from_entry(index: int, entry: object, source: Path) -> ListPortInfo:
    where = f"{PORTS_FILE_ENV} {source}: entry {index}"
    if not isinstance(entry, dict) or not isinstance(entry.get("device"), str):
        raise DeviceError(f"{where}: expected an object with a string `device`")
    info = ListPortInfo(entry["device"], skip_link_detection=True)
    for key in ("vid", "pid"):
        value = entry.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise DeviceError(f"{where}: {key} must be an integer or null")
        setattr(info, key, value)
    for key in _PORT_TEXT_ATTRS:
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            raise DeviceError(f"{where}: {key} must be a string or null")
        setattr(info, key, value)
    info.hwid = info.usb_info()
    return info


def _read_ports_file(source: Path) -> list[ListPortInfo]:
    try:
        entries = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeviceError(f"{PORTS_FILE_ENV} {source}: {exc}") from exc
    if not isinstance(entries, list):
        raise DeviceError(f"{PORTS_FILE_ENV} {source}: expected a JSON array")
    return [_port_from_entry(index, entry, source) for index, entry in enumerate(entries, 1)]


def list_ports() -> list[ListPortInfo]:
    """Present serial ports. With BENCHCTL_PORTS_FILE set, that file, re-read on every call
    because a reconnect must see a changed path; otherwise pyserial's `comports()`."""
    source = os.environ.get(PORTS_FILE_ENV)
    if source:
        return _read_ports_file(Path(source))
    return list(_list_ports.comports())


def _port_matches(port: ListPortInfo, key: str, want: str) -> bool:
    if key in ("vid", "pid"):
        value = getattr(port, key)
        return value is not None and f"{value:04x}" == usb_id(want)
    if key == "device":
        return fnmatchcase(port.device, want)
    actual: str | None = getattr(port, _EXACT_ATTRS[key])
    return actual == want


def matching_ports(spec: Mapping[str, str], ports: list[ListPortInfo]) -> list[ListPortInfo]:
    """Every port that satisfies all keys of `spec`. Raises UsageError on an empty spec or an
    unknown key."""
    if not spec:
        raise UsageError(f"empty port match: give at least one of {', '.join(MATCH_KEYS)}")
    unknown = sorted(set(spec) - set(MATCH_KEYS))
    if unknown:
        raise UsageError(f"unknown port match key(s): {', '.join(unknown)}")
    return [
        port for port in ports if all(_port_matches(port, key, want) for key, want in spec.items())
    ]


def _describe(spec: Mapping[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in spec.items())


def match_port(spec: dict[str, str], ports: list[ListPortInfo]) -> str:
    """The device path of the one port matching `spec`. Raises DeviceError naming the
    candidates when none or several match."""
    found = matching_ports(spec, ports)
    if len(found) == 1:
        return found[0].device
    if not found:
        present = ", ".join(port.device for port in ports) or "none"
        raise DeviceError(
            f"no port matches {_describe(spec)} (present: {present})",
            data={"match": dict(spec), "candidates": [port.device for port in ports]},
        )
    candidates = [port.device for port in found]
    raise DeviceError(
        f"{len(found)} ports match {_describe(spec)}: {', '.join(candidates)}; "
        "add a match key that tells them apart",
        data={"match": dict(spec), "candidates": candidates},
    )
