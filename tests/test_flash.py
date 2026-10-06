"""Flashing on the held handle and in direct mode (design Sections 4.10 and 8, U6)."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Literal

import esptool
import esptool.targets
import pytest
import serial

from benchctl import artifacts
from benchctl.errors import DeviceError, UsageError
from benchctl.flash import flash_direct, flash_on_session
from benchctl.profile import Profile
from benchctl.types import (
    AppDesc,
    CmdResult,
    Event,
    FlashResult,
    Identity,
    LogLine,
    Offset,
    ResetInfo,
    Status,
    Subscription,
    Transport,
    WaitResult,
)
from fakedev import FakeDevice

CHIP = "esp32s3"
BEGIN_NOTE = "flash begin bootloader partition-table otadata app baud=921600 after=hard-reset"
FLASH_BAUD = 921600
CONSOLE_SETTINGS: dict[str, Any] = {"baudrate": 115200, "timeout": 0.1}
ELF_SHA = bytes(range(32))
BOOT_RESET = ResetInfo(
    offset=7,
    t=1.5,
    code=1,
    reason="POWERON",
    boot_mode="SPI_FAST_FLASH_BOOT",
    elf_sha=None,
    expected=True,
    cause="flash",
    context=[],
)
FLASHER_ARGS_JSON: dict[str, Any] = {
    "write_flash_args": ["--flash_mode", "dio", "--flash_size", "keep", "--flash_freq", "80m"],
    "flash_settings": {"flash_mode": "dio", "flash_size": "keep", "flash_freq": "80m"},
    "flash_files": {
        "0x0": "bootloader/bootloader.bin",
        "0x20000": "app.bin",
        "0x10000": "partition_table/partition-table.bin",
        "0x1c000": "ota_data_initial.bin",
    },
    "bootloader": {"offset": "0x0", "file": "bootloader/bootloader.bin"},
    "app": {"offset": "0x20000", "file": "app.bin"},
    "partition-table": {"offset": "0x10000", "file": "partition_table/partition-table.bin"},
    "otadata": {"offset": "0x1c000", "file": "ota_data_initial.bin"},
}


def app_image() -> bytes:
    """An app image whose esp_app_desc_t sits after the image and first segment headers."""
    desc = struct.pack(
        "<II8s32s32s16s16s32s32s",
        artifacts.APP_DESC_MAGIC,
        0,
        bytes(8),
        b"1.2.3-4-gabc",
        b"fakeapp",
        b"12:42:21",
        b"Oct  6 2026",
        b"v5.4",
        ELF_SHA,
    )
    return bytes(artifacts.APP_DESC_OFFSET) + desc + bytes(64)


EXPECTED_APP = AppDesc(
    version="1.2.3-4-gabc",
    project="fakeapp",
    elf_sha256=ELF_SHA.hex(),
    idf_ver="v5.4",
    time="12:42:21",
    date="Oct  6 2026",
)


class FakeTransport:
    """The paused transport: records settings traffic; nothing may read or write it."""

    path = "/dev/fake-console"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.restored: list[dict[str, Any]] = []
        self.restore_error: Exception | None = None
        self._raw = serial.Serial()

    def read(self, max_bytes: int, timeout: float) -> bytes:
        raise AssertionError("nothing reads the console during a flash")

    def write(self, data: bytes) -> None:
        raise AssertionError("nothing writes the console during a flash")

    def reset(self, bootloader: bool = False) -> None:
        raise AssertionError("flash resets through esptool or the session, never the transport")

    def get_settings(self) -> dict[str, Any]:
        self.calls.append("get_settings")
        return dict(CONSOLE_SETTINGS)

    def apply_settings(self, s: dict[str, Any]) -> None:
        self.calls.append("apply_settings")
        self.restored.append(s)
        if self.restore_error is not None:
            raise self.restore_error

    def set_baud(self, baud: int) -> None:
        raise AssertionError("esptool owns the baud while paused; apply_settings restores it")

    def close(self) -> None:
        raise AssertionError("the session owns the port")

    @property
    def raw(self) -> serial.Serial:
        return self._raw


class FakeSession:
    """A types.Session that logs every flash-relevant call into one ordered `calls` list.

    `lines` is the stored log; `on_reset` stands in for the reader storing what the device
    prints after a reset.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.profile = Profile(name="esp-idf", path=tmp_path / "profile.toml")
        self.unit = "u1"
        self.session_dir = tmp_path
        self.calls: list[str] = []
        self.transport = FakeTransport(self.calls)
        self.lines: list[LogLine] = []
        self.events: list[Event] = []
        self.paused = False
        self.locked = 0
        self.reset_result: ResetInfo | None = BOOT_RESET
        self.on_reset: Callable[[], None] | None = None
        self.log_calls: list[tuple[Offset, Offset | None, str | None, int]] = []

    def append(self, text: str, *, meta: bool = False) -> Offset:
        offset = len(self.lines)
        self.lines.append(LogLine(offset, 0.1 * offset, "12:00:00.000", text, meta))
        return offset

    def status(self) -> Status:
        raise NotImplementedError

    def mark(self, name: str | None) -> Offset:
        assert name is None
        return len(self.lines)

    def note(self, text: str) -> Offset:
        self.calls.append(f"note:{text.split(' duration=')[0]}")
        return self.append(f"## {text}", meta=True)

    def resolve(self, cursor: str) -> Offset:
        raise NotImplementedError

    def cmd(
        self,
        line: str,
        *,
        expect: str | None,
        error: list[str],
        capture: bool | None,
        timeout: float,
        reboots: bool,
        secret: bool,
        force: bool,
        source: str,
    ) -> CmdResult:
        raise NotImplementedError

    def wait(
        self,
        pattern: str | None,
        since: Offset,
        timeout: float,
        *,
        absent: str | None,
        window: float | None,
        order: list[str] | None,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> WaitResult:
        raise NotImplementedError

    def log(
        self, since: Offset, until: Offset | None, grep: str | None, limit: int
    ) -> list[LogLine]:
        self.log_calls.append((since, until, grep, limit))
        stop = len(self.lines) if until is None else until
        return self.lines[since:stop][:limit]

    def reset(
        self, bootloader: bool, wait_rst: float = 5.0, *, cause: str = "reset"
    ) -> ResetInfo | None:
        self.calls.append(f"reset:{bootloader}:{wait_rst}:{cause}")
        if self.on_reset is not None:
            self.on_reset()
        return self.reset_result

    def set_gate(self, state: Literal["booting", "bootloader"], method: str | None = None) -> None:
        self.calls.append(f"set_gate:{state}:{method}")

    def arm_reset(self, cause: str, window: float) -> None:
        raise NotImplementedError

    def identity(self, reprobe: bool = False) -> Identity:
        raise NotImplementedError

    def gate_check(self, since: Offset, timeout: float) -> dict[str, Any]:
        raise NotImplementedError

    def subscribe(self) -> Subscription:
        raise NotImplementedError

    @contextmanager
    def _device_lock(self) -> Iterator[None]:
        self.locked += 1
        try:
            yield
        finally:
            self.locked -= 1

    def device_lock(self) -> AbstractContextManager[None]:
        return self._device_lock()

    @contextmanager
    def _paused(self) -> Iterator[Transport]:
        self.calls.append("pause")
        self.paused = True
        try:
            yield self.transport
        finally:
            self.paused = False
            self.calls.append("resume")

    def pause_reader(self) -> AbstractContextManager[Transport]:
        return self._paused()

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        raise NotImplementedError


class FakeEsptool:
    """Stands in for esptool.main and the chip's ROM loader class; records into `calls`."""

    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session
        self.calls: list[str] = [] if session is None else session.calls
        self.argv: list[str] | None = None
        self.loaders: list[FakeEsptool.Loader] = []
        self.main_error: BaseException | None = None
        self.connect_error: BaseException | None = None
        self.output = "esptool v5.4.0\nConnected to ESP32-S3\nWriting at 0x00020000"

    class Loader:
        def __init__(self, owner: FakeEsptool, raw: serial.Serial, baud: int) -> None:
            self.owner = owner
            self.raw = raw
            self.baud = baud

        def connect(self, mode: str) -> None:
            self.owner.calls.append(f"connect:{mode}")
            if self.owner.connect_error is not None:
                raise self.owner.connect_error

    def loader(self, raw: serial.Serial, baud: int) -> FakeEsptool.Loader:
        self.calls.append("loader")
        made = FakeEsptool.Loader(self, raw, baud)
        self.loaders.append(made)
        return made

    def main(self, argv: list[str], esp: Any = None) -> None:
        self.calls.append("main")
        self.argv = argv
        if self.session is not None:
            assert self.session.paused, "esptool ran while the reader was live"
            assert self.session.locked, "esptool ran outside the device lock"
            assert esp is self.loaders[-1], "main did not get the connected loader"
        # Split mid-line, as Rich writes a progress bar in pieces.
        for chunk in (self.output[:20], self.output[20:], " 100.0%\n"):
            print(chunk, end="")
        if self.main_error is not None:
            raise self.main_error


@pytest.fixture
def session(tmp_path: Path) -> FakeSession:
    return FakeSession(tmp_path)


@pytest.fixture
def fake_esptool(session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> FakeEsptool:
    fake = FakeEsptool(session)
    monkeypatch.setattr(esptool, "main", fake.main)
    monkeypatch.setitem(esptool.targets.CHIP_DEFS, CHIP, fake.loader)
    return fake


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    for relative in FLASHER_ARGS_JSON["flash_files"].values():
        path = build / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(app_image() if relative == "app.bin" else relative.encode())
    (build / artifacts.FLASHER_ARGS).write_text(json.dumps(FLASHER_ARGS_JSON), encoding="utf-8")
    return build


def flash(
    session: FakeSession, specs: list[str], *, after: str = "hard-reset"
) -> tuple[FlashResult, list[tuple[int, str, str]]]:
    files, flash_args = artifacts.resolve(specs, None, {})
    result = flash_on_session(
        session,
        files,
        chip=CHIP,
        before="default-reset",
        after=after,
        baud=FLASH_BAUD,
        flash_args=flash_args,
    )
    return result, files


def build_argv(build: Path) -> list[str]:
    return [
        "--chip",
        "esp32s3",
        "--baud",
        "921600",
        "--before",
        "no-reset",
        "--after",
        "no-reset-stub",
        "write-flash",
        "--flash-mode",
        "dio",
        "--flash-size",
        "keep",
        "--flash-freq",
        "80m",
        "0x0",
        str(build / "bootloader" / "bootloader.bin"),
        "0x10000",
        str(build / "partition_table" / "partition-table.bin"),
        "0x1c000",
        str(build / "ota_data_initial.bin"),
        "0x20000",
        str(build / "app.bin"),
    ]


@pytest.mark.parametrize("spec", ["build dir", "flasher_args.json"])
def test_argv_for_a_build_matches_the_design(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path, spec: str
) -> None:
    target = build_dir if spec == "build dir" else build_dir / artifacts.FLASHER_ARGS
    flash(session, [str(target)])
    assert fake_esptool.argv == build_argv(build_dir.resolve())


def test_argv_for_addr_file_pairs_matches_the_design(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    app = build_dir.resolve() / "app.bin"
    otadata = build_dir.resolve() / "ota_data_initial.bin"
    flash(session, [f"0x20000={app}", f"0x1c000={otadata}"])
    assert fake_esptool.argv == [
        "--chip",
        "esp32s3",
        "--baud",
        "921600",
        "--before",
        "no-reset",
        "--after",
        "no-reset-stub",
        "write-flash",
        "--flash-mode",
        "keep",
        "--flash-size",
        "keep",
        "--flash-freq",
        "keep",
        "0x1c000",
        str(otadata),
        "0x20000",
        str(app),
    ]


def test_hard_reset_sequence(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    result, _ = flash(session, [str(build_dir)])
    assert session.calls == [
        f"note:{BEGIN_NOTE}",
        "pause",
        "get_settings",
        "loader",
        "connect:default-reset",
        "main",
        "apply_settings",
        "resume",
        "note:flash end status=ok",
        "reset:False:5.0:flash",
    ]
    assert session.transport.restored == [CONSOLE_SETTINGS]
    loader = fake_esptool.loaders[0]
    assert loader.raw is session.transport.raw
    assert loader.baud == esptool.loader.ESPLoader.ESP_ROM_BAUD
    assert result.reset is BOOT_RESET
    assert session.locked == 0


def test_no_reset_moves_the_gate_to_bootloader_after_the_pause(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    result, _ = flash(session, [str(build_dir)], after="no-reset")
    assert session.calls[-4:] == [
        "apply_settings",
        "resume",
        "note:flash end status=ok",
        "set_gate:bootloader:flash",
    ]
    assert not any(call.startswith("reset:") for call in session.calls)
    assert result.reset is None


def test_result_carries_hashes_app_desc_and_the_session_reset(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    result, files = flash(session, [str(build_dir)])
    assert [(f.addr, f.path, f.name) for f in result.files] == files
    for file in result.files:
        content = Path(file.path).read_bytes()
        assert file.sha256 == hashlib.sha256(content).hexdigest()
        assert file.size == len(content)
    assert result.app == EXPECTED_APP
    assert result.baud == FLASH_BAUD
    assert result.reset is BOOT_RESET
    assert result.duration >= 0


def test_flash_event_and_esptool_output_become_events(
    session: FakeSession,
    fake_esptool: FakeEsptool,
    build_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, _ = flash(session, [str(build_dir)])
    logs = [e["line"] for e in session.events if e["type"] == "flash_log"]
    assert logs == ["esptool v5.4.0", "Connected to ESP32-S3", "Writing at 0x00020000 100.0%"]
    (event,) = [e for e in session.events if e["type"] == "flash"]
    assert event["status"] == "ok"
    assert event["error"] is None
    assert event["offset"] == 0  # the `## flash begin` note
    assert event["after"] == "hard-reset"
    assert event["app"]["elf_sha256"] == ELF_SHA.hex()
    assert [f["sha256"] for f in event["files"]] == [f.sha256 for f in result.files]
    assert capsys.readouterr().out == ""


def test_notes_are_meta_lines_through_the_session(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    flash(session, [str(build_dir)])
    texts = [line.text for line in session.lines]
    assert texts[0] == f"## {BEGIN_NOTE}"
    assert texts[1].startswith("## flash end status=ok duration=")
    assert all(line.meta for line in session.lines)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: esptool.FatalError("Failed to write to target RAM"), id="fatal"),
        pytest.param(lambda: serial.SerialException("device disconnected"), id="serial"),
        pytest.param(lambda: SystemExit(2), id="usage-exit"),
        pytest.param(lambda: StopIteration(), id="stopped-responding"),
    ],
)
def test_esptool_failure_is_exit_4_and_still_restores_and_resumes(
    session: FakeSession,
    fake_esptool: FakeEsptool,
    build_dir: Path,
    failure: Callable[[], BaseException],
) -> None:
    fake_esptool.main_error = failure()
    with pytest.raises(DeviceError) as caught:
        flash(session, [str(build_dir)])
    assert caught.value.exit_code == 4
    assert session.calls[-5:] == [
        "main",
        "apply_settings",
        "resume",
        "note:flash end status=error",
        "set_gate:bootloader:flash",
    ]
    assert session.transport.restored == [CONSOLE_SETTINGS]
    assert not session.paused
    (event,) = [e for e in session.events if e["type"] == "flash"]
    assert event["status"] == "error"
    assert event["error"] == caught.value.message


def test_fatal_error_message_reaches_the_caller(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    fake_esptool.main_error = esptool.FatalError("Failed to write to target RAM")
    with pytest.raises(DeviceError, match="Failed to write to target RAM"):
        flash(session, [str(build_dir)])


def test_unexpected_error_propagates_but_still_restores_and_closes_the_flash(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    fake_esptool.main_error = KeyError("not an esptool failure")
    with pytest.raises(KeyError):
        flash(session, [str(build_dir)])
    assert session.calls[-5:] == [
        "main",
        "apply_settings",
        "resume",
        "note:flash end status=error",
        "set_gate:bootloader:flash",
    ]


def test_connect_failure_that_closed_the_port_keeps_the_esptool_error(
    session: FakeSession, fake_esptool: FakeEsptool, build_dir: Path
) -> None:
    # esptool closes the port when connect gives up, so restoring settings fails as well.
    fake_esptool.connect_error = esptool.FatalError("Failed to connect to ESP32-S3: No serial data")
    session.transport.restore_error = serial.PortNotOpenError()
    with pytest.raises(DeviceError, match="Failed to connect to ESP32-S3"):
        flash(session, [str(build_dir)])
    assert "main" not in session.calls
    assert session.calls[-4:] == [
        "apply_settings",
        "resume",
        "note:flash end status=error",
        "set_gate:bootloader:flash",
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"after": "watchdog-reset"}, "invalid after"),
        ({"chip": "esp99"}, "unknown chip"),
        ({"flash_args": {"flash-mode": "dio"}}, "flash_args must set exactly"),
    ],
)
def test_bad_input_is_usage_before_anything_happens(
    session: FakeSession,
    fake_esptool: FakeEsptool,
    build_dir: Path,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    files, flash_args = artifacts.resolve([str(build_dir)], None, {})
    args: dict[str, Any] = {
        "chip": CHIP,
        "before": "default-reset",
        "after": "hard-reset",
        "baud": FLASH_BAUD,
        "flash_args": flash_args,
    }
    args.update(kwargs)
    with pytest.raises(UsageError, match=message):
        flash_on_session(session, files, **args)
    assert session.calls == []
    assert session.lines == []


def test_unreadable_file_is_usage_before_anything_happens(
    session: FakeSession, fake_esptool: FakeEsptool, tmp_path: Path
) -> None:
    missing = [(0x20000, str(tmp_path / "gone.bin"), "app")]
    with pytest.raises(UsageError):
        flash_on_session(
            session,
            missing,
            chip=CHIP,
            before="default-reset",
            after="hard-reset",
            baud=FLASH_BAUD,
            flash_args=dict.fromkeys(artifacts.FLASH_ARG_KEYS, "keep"),
        )
    assert session.calls == []


# Direct mode


@pytest.fixture
def direct_esptool(monkeypatch: pytest.MonkeyPatch) -> FakeEsptool:
    fake = FakeEsptool()
    monkeypatch.setattr(esptool, "main", fake.main)
    return fake


def test_direct_argv_lets_esptool_open_and_reset(
    direct_esptool: FakeEsptool, build_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files, flash_args = artifacts.resolve([str(build_dir)], ["app"], {})
    result = flash_direct(
        "/dev/cu.fake",
        files,
        chip=CHIP,
        before="default-reset",
        after="hard-reset",
        baud=FLASH_BAUD,
        flash_args=flash_args,
    )
    app = str(build_dir.resolve() / "app.bin")
    assert direct_esptool.argv == [
        "--chip",
        "esp32s3",
        "--port",
        "/dev/cu.fake",
        "--baud",
        "921600",
        "--before",
        "default-reset",
        "--after",
        "hard-reset",
        "write-flash",
        "--flash-mode",
        "dio",
        "--flash-size",
        "keep",
        "--flash-freq",
        "80m",
        "0x20000",
        app,
    ]
    assert result.app == EXPECTED_APP
    assert result.reset is None
    assert result.files[0].sha256 == hashlib.sha256(app_image()).hexdigest()
    out = capsys.readouterr()
    assert out.out == ""
    assert "esptool v5.4.0" in out.err


def test_direct_esptool_failure_is_exit_4(direct_esptool: FakeEsptool, build_dir: Path) -> None:
    direct_esptool.main_error = esptool.FatalError("Failed to connect to ESP32-S3")
    files, flash_args = artifacts.resolve([str(build_dir)], None, {})
    with pytest.raises(DeviceError, match="Failed to connect") as caught:
        flash_direct(
            "/dev/cu.fake",
            files,
            chip=CHIP,
            before="default-reset",
            after="no-reset",
            baud=FLASH_BAUD,
            flash_args=flash_args,
        )
    assert caught.value.exit_code == 4


def test_direct_on_a_port_a_daemon_holds_is_port_busy(fakedev: FakeDevice, build_dir: Path) -> None:
    # Real esptool: its exclusive open fails before any byte reaches the device.
    files, flash_args = artifacts.resolve([str(build_dir)], ["app"], {})
    with serial.Serial(fakedev.path, exclusive=True), pytest.raises(DeviceError) as caught:
        flash_direct(
            fakedev.path,
            files,
            chip=CHIP,
            before="default-reset",
            after="hard-reset",
            baud=FLASH_BAUD,
            flash_args=flash_args,
        )
    assert caught.value.message.startswith(f"port busy: {fakedev.path}")
    assert caught.value.exit_code == 4
    assert fakedev.received == []
