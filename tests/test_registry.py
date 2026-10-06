"""`units.toml`, port enumeration and port matching (design Section 7.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from serial.tools import list_ports as pyserial_list_ports
from serial.tools.list_ports_common import ListPortInfo

from benchctl import errors
from benchctl.profile import ConfigError
from benchctl.registry import (
    PORTS_FILE_ENV,
    Registry,
    list_ports,
    load_registry,
    match_port,
    matching_ports,
    read_registry,
    usb_id,
)
from conftest import PROFILE_NAME, UNIT, dump_toml
from fakedev import FAKE_PID, FAKE_VID, FakeDevice

FTDI_VID, FTDI_PID = 0x0403, 0x6010


def port(
    device: str,
    vid: int | None = None,
    pid: int | None = None,
    serial_number: str | None = None,
    location: str | None = None,
    product: str | None = None,
) -> ListPortInfo:
    info = ListPortInfo(device, skip_link_detection=True)
    info.vid, info.pid = vid, pid
    info.serial_number, info.location, info.product = serial_number, location, product
    return info


# The bench FTDI's two channels share vid, pid and location and carry no serial number.
BENCH = [
    port("/dev/cu.Bluetooth-Incoming-Port"),
    port("/dev/cu.usbserial-2100", FTDI_VID, FTDI_PID, location="2-1", product="Dual RS232-HS"),
    port("/dev/cu.usbserial-2101", FTDI_VID, FTDI_PID, location="2-1", product="Dual RS232-HS"),
    port("/dev/cu.usbmodem1101", FAKE_VID, FAKE_PID, serial_number="AA:BB", location="1-1"),
]


# --- units.toml --------------------------------------------------------------------------


def test_units_toml_loads_inline_and_nested_tables(config_dir: Path) -> None:
    (config_dir / "units.toml").write_text(
        """
[units.bench]
profile = "demo"
console.match = { vid = "0403", pid = "6010", location = "2-1", device = "*usbserial-*1" }

[units.native]
[units.native.console.match]
serial = "AA:BB"
vid = "303A"
pid = "1001"
[units.native.expect]
mac = "AA:BB:CC:DD:EE:FF"
id_field = "mac"
""",
        encoding="utf-8",
    )
    registry = load_registry(config_dir)

    bench = registry.unit("bench")
    assert bench.profile == "demo"
    assert bench.match == {
        "vid": "0403",
        "pid": "6010",
        "location": "2-1",
        "device": "*usbserial-*1",
    }
    native = registry.unit("native")
    assert native.match == {"serial": "AA:BB", "vid": "303a", "pid": "1001"}
    assert (native.profile, native.expect, native.id_field) == (
        None,
        {"mac": "AA:BB:CC:DD:EE:FF"},
        "mac",
    )


def test_registry_fixture_loads(registry: Path, config_dir: Path, fakedev: FakeDevice) -> None:
    unit = load_registry(config_dir).unit(None)
    assert unit.name == UNIT
    assert unit.profile == PROFILE_NAME
    assert unit.match == {"serial": fakedev.serial_number}


def test_unit_selection(config_dir: Path) -> None:
    assert load_registry(config_dir) == Registry(path=config_dir / "units.toml")
    with pytest.raises(errors.UsageError, match="no units registered"):
        load_registry(config_dir).unit(None)

    units = {name: {"console": {"match": {"serial": name}}} for name in ("a", "b")}
    (config_dir / "units.toml").write_text(dump_toml({"units": units}), encoding="utf-8")
    registry = load_registry(config_dir)
    with pytest.raises(errors.UsageError, match="pass --unit"):
        registry.unit(None)
    with pytest.raises(errors.UsageError, match="unknown unit 'c'"):
        registry.unit("c")
    assert registry.unit("b").match == {"serial": "b"}


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[other]\nx = 1\n", "other: unknown key"),
        ("[units.u1]\nport = '/dev/x'\n", "units.u1.port: unknown key"),
        ("[units.u1]\nprofile = 'p'\n", "units.u1.console: required"),
        ("[units.u1.console]\nbaud = 1\n", "units.u1.console.match: required"),
        ("[units.u1.console.match]\n", "units.u1.console.match: needs at least one of"),
        ("[units.u1.console.match]\nhwid = 'x'\n", "units.u1.console.match.hwid: unknown key"),
        ("[units.u1.console.match]\nvid = 'ftdi'\n", "units.u1.console.match.vid: expected up to"),
        ("[units.u1.console.match]\npid = 70000\n", "units.u1.console.match.pid: expected up to"),
        ("[units.u1.console.match]\nvid = 1027\n", "units.u1.console.match.vid: expected up to"),
        ("[units.u1.console.match]\nserial = ''\n", "units.u1.console.match.serial: expected a"),
        ("[units.runs.console.match]\nserial = 'x'\n", "units.runs: invalid unit name"),
        (
            "[units.u1.console.match]\nserial = 'x'\n[units.u1.expect]\nmac = 1\n",
            "units.u1.expect.mac: expected a string",
        ),
    ],
)
def test_rule_violation_is_path_qualified(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "units.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        read_registry(path)
    found = raised.value.errors
    assert any(line.startswith(f"{path}: {error}") for line in found), found


# --- matching ----------------------------------------------------------------------------


def test_match_picks_exactly_one_port() -> None:
    assert match_port({"serial": "AA:BB"}, BENCH) == "/dev/cu.usbmodem1101"
    ftdi = {"vid": "0403", "pid": "6010", "location": "2-1", "device": "*usbserial-*1"}
    assert match_port(ftdi, BENCH) == "/dev/cu.usbserial-2101"
    assert match_port({"product": "Dual RS232-HS", "device": "*2100"}, BENCH) == (
        "/dev/cu.usbserial-2100"
    )


def test_usb_ids_compare_as_four_hex_digits_case_insensitively() -> None:
    assert match_port({"vid": "303A"}, BENCH) == "/dev/cu.usbmodem1101"
    assert match_port({"vid": "0x303a", "pid": "1001"}, BENCH) == "/dev/cu.usbmodem1101"
    assert match_port({"vid": "403", "device": "*2100"}, BENCH) == "/dev/cu.usbserial-2100"
    assert usb_id("0X0403") == "0403"
    with pytest.raises(errors.UsageError):
        usb_id("12345")


def test_ambiguity_names_every_candidate() -> None:
    spec = {"vid": "0403", "pid": "6010", "location": "2-1"}
    assert len(matching_ports(spec, BENCH)) == 2
    with pytest.raises(errors.DeviceError) as raised:
        match_port(spec, BENCH)
    assert "/dev/cu.usbserial-2100" in raised.value.message
    assert "/dev/cu.usbserial-2101" in raised.value.message
    assert raised.value.data["candidates"] == ["/dev/cu.usbserial-2100", "/dev/cu.usbserial-2101"]
    assert raised.value.exit_code == 4


def test_no_match_names_the_present_ports() -> None:
    with pytest.raises(errors.DeviceError, match="no port matches serial=ZZ") as raised:
        match_port({"serial": "ZZ"}, BENCH)
    assert "/dev/cu.Bluetooth-Incoming-Port" in raised.value.message
    assert matching_ports({"serial": "ZZ"}, []) == []


def test_ports_without_usb_ids_never_match_a_vid() -> None:
    no_ids = [port("/dev/cu.Bluetooth-Incoming-Port")]
    assert matching_ports({"vid": "0403"}, no_ids) == []
    assert matching_ports({"pid": "6010"}, no_ids) == []
    assert matching_ports({"serial": "x"}, no_ids) == []


def test_match_spec_must_be_known_and_non_empty() -> None:
    with pytest.raises(errors.UsageError, match="empty port match"):
        matching_ports({}, BENCH)
    with pytest.raises(errors.UsageError, match="unknown port match key"):
        matching_ports({"hwid": "x"}, BENCH)


# --- enumeration -------------------------------------------------------------------------


def test_list_ports_reads_the_ports_file(ports_file: Path, fakedev: FakeDevice) -> None:
    [info] = list_ports()
    assert info.device == fakedev.path
    assert (info.vid, info.pid) == (FAKE_VID, FAKE_PID)
    assert info.serial_number == fakedev.serial_number
    assert info.product == "FakeDevice"
    assert f"VID:PID={FAKE_VID:04X}:{FAKE_PID:04X}" in info.hwid


def test_list_ports_rereads_the_file_on_every_call(ports_file: Path) -> None:
    entry = {"device": "/dev/ttyA", "vid": None, "pid": None, "serial_number": None}
    ports_file.write_text(json.dumps([entry]), encoding="utf-8")
    assert [p.device for p in list_ports()] == ["/dev/ttyA"]
    assert list_ports()[0].vid is None

    ports_file.write_text(json.dumps([{**entry, "device": "/dev/ttyB"}]), encoding="utf-8")
    assert [p.device for p in list_ports()] == ["/dev/ttyB"]


@pytest.mark.parametrize(
    "content",
    ["not json", '{"device": "/dev/x"}', '[{"vid": 1}]', '[{"device": "/dev/x", "vid": "1"}]'],
)
def test_malformed_ports_file_is_a_device_error(ports_file: Path, content: str) -> None:
    ports_file.write_text(content, encoding="utf-8")
    with pytest.raises(errors.DeviceError, match=PORTS_FILE_ENV):
        list_ports()


def test_list_ports_falls_back_to_comports(monkeypatch: pytest.MonkeyPatch) -> None:
    present = [port("/dev/cu.usbmodem1", FAKE_VID, FAKE_PID)]
    monkeypatch.setattr(pyserial_list_ports, "comports", lambda: iter(present))
    assert list_ports() == present


def test_registry_unit_resolves_to_the_fake_through_the_ports_file(
    registry: Path, config_dir: Path, fakedev: FakeDevice
) -> None:
    unit = load_registry(config_dir).unit(UNIT)
    assert match_port(unit.match, list_ports()) == fakedev.path

    fakedev.vanish()
    with pytest.raises(errors.DeviceError, match="no port matches"):
        match_port(unit.match, list_ports())

    new_path = fakedev.reappear()
    assert match_port(unit.match, list_ports()) == new_path
