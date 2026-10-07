"""DeviceSession against the fake device through the real PySerialTransport (design Section 8,
U4), plus hail's port-level cases."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from benchctl import session as session_mod
from benchctl.errors import DeviceError, TransportLost, UsageError
from benchctl.profile import EnvGate, IdentityField, Profile
from benchctl.registry import Unit, load_registry
from benchctl.session import DeviceSession
from benchctl.types import Event, LogLine, ModemLines, Session, Subscription
from conftest import PROFILE_NAME, UNIT, FakeDeviceFactory, dump_toml
from fakedev import APP_VERSION, ELF_SHA_PREFIX, PROMPT, SECRET, DeviceScript, FakeDevice

VERSION_REPLY = f"Application Version: {APP_VERSION}"
SLOW_S = 80.0  # device seconds: 4 s of wall time, far past any bound below
POLL_S = 0.01
SETTLE_BOUND_S = 5.0


def until(predicate: Callable[[], bool], timeout: float = SETTLE_BOUND_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(POLL_S)


def register(config_dir: Path, device: FakeDevice, expect: dict[str, str] | None = None) -> Unit:
    unit: dict[str, Any] = {
        "profile": PROFILE_NAME,
        "console": {"match": {"serial": device.serial_number}},
    }
    if expect:
        unit["expect"] = expect
    (config_dir / "units.toml").write_text(dump_toml({"units": {UNIT: unit}}), encoding="utf-8")
    return load_registry(config_dir).unit(UNIT)


def events(session: DeviceSession, kind: str | None = None) -> list[Event]:
    path = session.session_dir / "events.jsonl"
    records = [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in records if kind is None or r["type"] == kind]


def texts(session: DeviceSession, since: int = 0) -> list[str]:
    return [line.text for line in session.log(since, None, None, 100_000)]


def ready(session: DeviceSession) -> bool:
    return session.status().gate.state == "ready"


def cmd(session: DeviceSession, line: str, **overrides: Any) -> Any:
    args: dict[str, Any] = {
        "expect": None,
        "error": [],
        "capture": None,
        "timeout": 2.0,
        "reboots": False,
        "secret": False,
        "force": False,
        "source": "cmd",
    }
    args.update(overrides)
    return session.cmd(line, **args)


SessionFactory = Callable[..., DeviceSession]


@pytest.fixture
def make_session(config_dir: Path, profile: Profile) -> Iterator[SessionFactory]:
    opened: list[DeviceSession] = []

    def make(
        device: FakeDevice,
        *,
        with_profile: Profile | None = None,
        expect: dict[str, str] | None = None,
        lines: ModemLines | None = None,
    ) -> DeviceSession:
        unit = register(config_dir, device, expect)
        session = DeviceSession(
            unit, with_profile or profile, owner="tester", lines=lines or device.reset
        )
        opened.append(session)
        return session

    yield make
    for session in opened:
        session.close()


@pytest.fixture
def session(
    registry: Path, config_dir: Path, profile: Profile, fake_lines: ModemLines
) -> Iterator[DeviceSession]:
    opened = DeviceSession(load_registry(config_dir).unit(UNIT), profile, lines=fake_lines)
    yield opened
    opened.close()


@pytest.fixture
def ready_session(session: DeviceSession) -> DeviceSession:
    until(lambda: ready(session))
    return session


def test_device_session_satisfies_the_session_protocol(session: DeviceSession) -> None:
    typed: Session = session
    assert typed.unit == UNIT


def test_open_session_takes_the_daemons_port_and_session_dir(
    registry: Path,
    config_dir: Path,
    state_dir: Path,
    profile: Profile,
    fakedev: FakeDevice,
    fake_lines: ModemLines,
) -> None:
    session_dir = state_dir / UNIT / "sessions" / "20261006T120000"
    session_dir.mkdir(parents=True)
    unit = load_registry(config_dir).unit(UNIT)
    opened = session_mod.open_session(
        unit=unit,
        profile=profile,
        port=fakedev.path,
        session_dir=session_dir,
        owner="me@bench:/repo",
        lines=fake_lines,
    )
    try:
        assert opened.session_dir == session_dir
        until(lambda: ready(opened))
        attach = events(opened, "attach")[0]
        assert (attach["owner"], attach["session_dir"]) == ("me@bench:/repo", str(session_dir))
    finally:
        opened.close()


# --- attach, offsets, resets -------------------------------------------------------------


def test_attach_resets_once_and_stores_boot_lines_after_meta_line_zero(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    lines = ready_session.log(0, None, None, 10_000)
    assert fakedev.resets == ["poweron"]
    assert [line.offset for line in lines] == list(range(len(lines)))
    assert lines[0].meta
    assert lines[0].text.startswith(f"## attach unit={UNIT} port={fakedev.path} baud=115200 at=")
    assert not any(line.meta for line in lines[1:])
    body = [line.text for line in lines[1:]]
    assert body[0] == "ESP-ROM:esp32s3-20210327"
    assert "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)" in body
    assert "I (420) board: die temp 41.5°C" in body
    assert body[-1] == "I (1150) console: shell started"

    status = ready_session.status()
    reset = status.last_reset
    assert reset is not None
    assert (reset.code, reset.reason, reset.boot_mode) == (1, "POWERON", "SPI_FAST_FLASH_BOOT")
    assert reset.elf_sha == ELF_SHA_PREFIX
    assert (reset.expected, reset.cause) == (True, "attach")
    assert ready_session.resolve("boot") == reset.offset
    assert lines[reset.offset].text.startswith("rst:")
    assert status.connected and status.port == fakedev.path
    assert status.offset == len(lines)

    attach = events(ready_session, "attach")[0]
    assert (attach["offset"], attach["port"], attach["baud"]) == (0, fakedev.path, 115200)
    [sha] = events(ready_session, "elf_sha")
    assert (sha["sha"], sha["boot_offset"]) == (ELF_SHA_PREFIX, reset.offset)
    serial_log = (ready_session.session_dir / "serial.log").read_text(encoding="utf-8")
    assert serial_log.splitlines()[0].endswith(lines[0].text)


def test_unexpected_reset_carries_context_and_reset_op_is_expected(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    before = ready_session.status().offset
    fakedev.reset()  # nobody armed it: a crash or the operator's button
    until(lambda: (r := ready_session.status().last_reset) is not None and r.offset > before)
    surprise = ready_session.status().last_reset
    assert surprise is not None
    assert (surprise.expected, surprise.cause) == (False, None)
    assert surprise.context == texts(ready_session)[max(surprise.offset - 20, 0) : surprise.offset]
    assert "I (1150) console: shell started" in surprise.context
    [event] = [e for e in events(ready_session, "reset") if e["offset"] == surprise.offset]
    assert (event["expected"], event["inferred"], event["context"]) == (
        False,
        False,
        surprise.context,
    )

    requested = ready_session.status().offset
    info = ready_session.reset(False, wait_rst=2.0)
    assert info is not None
    assert (info.expected, info.cause, info.context) == (True, "reset", [])
    assert texts(ready_session)[requested] == "## reset requested bootloader=false"
    assert info.offset > requested
    flashed = ready_session.reset(False, wait_rst=2.0, cause="flash")
    assert flashed is not None and flashed.cause == "flash"
    assert fakedev.resets == ["poweron", "poweron", "poweron", "poweron"]


def test_reboots_cmd_arms_the_reset_and_a_plain_one_does_not(
    ready_session: DeviceSession,
) -> None:
    sent = cmd(ready_session, "reboot", reboots=True, capture=False)
    until(
        lambda: (r := ready_session.status().last_reset) is not None and r.offset > sent.tx_offset
    )
    expected = ready_session.status().last_reset
    assert expected is not None and (expected.expected, expected.cause) == (True, "cmd:reboot")

    until(lambda: ready(ready_session))
    plain = cmd(ready_session, "reboot", capture=False)
    until(
        lambda: (r := ready_session.status().last_reset) is not None and r.offset > plain.tx_offset
    )
    surprise = ready_session.status().last_reset
    assert surprise is not None and not surprise.expected


# --- the gate ------------------------------------------------------------------------------


def test_gate_runs_booting_settling_ready_and_cmd_blocks_until_ready(
    session: DeviceSession, fakedev: FakeDevice
) -> None:
    result = cmd(session, "version")  # sent at once: the gate holds it back
    assert result.status == "ok"
    assert result.captured == VERSION_REPLY
    [record] = fakedev.received
    assert (record.line, record.outcome) == ("version", "answered")
    assert record.since_reset is not None and record.since_reset >= fakedev.script.blind_s

    gates = [(e["state"], e["method"]) for e in events(session, "gate")]
    assert gates == [("booting", None), ("settling", "pattern"), ("ready", "pattern")]
    ready_seq = events(session, "gate")[-1]["seq"]
    assert events(session, "tx")[0]["seq"] > ready_seq
    settle = session.profile.boot.settle
    settling, ready_state = events(session, "gate")[1:]
    assert ready_state["t"] - settling["t"] >= settle - 0.01


def test_bootloader_refuses_cmd_until_reset(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    info = ready_session.reset(True, wait_rst=2.0)
    assert info is not None and info.boot_mode == "DOWNLOAD" and info.expected
    assert ready_session.status().gate.state == "bootloader"
    with pytest.raises(DeviceError, match="unit is in bootloader; run `reset`"):
        cmd(ready_session, "version")
    ready_session.set_gate("bootloader", method="flash")
    assert (
        events(ready_session, "gate")[-1]["state"],
        events(ready_session, "gate")[-1]["method"],
    ) == (
        "bootloader",
        "flash",
    )

    ready_session.reset(False, wait_rst=2.0)
    assert cmd(ready_session, "version").captured == VERSION_REPLY
    assert fakedev.resets[-2:] == ["download", "poweron"]


def test_attach_without_a_reset_reaches_ready_by_probing(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory, profile: Profile
) -> None:
    # An adapter that does not reset on open: the unit sits at its shell, saying nothing.
    device = make_fakedev()
    quick = dataclasses.replace(profile, boot=dataclasses.replace(profile.boot, ready_timeout=0.1))
    session = make_session(device, with_profile=quick, lines=lambda bootloader: None)
    until(lambda: ready(session))
    assert "## probe" in texts(session)
    assert events(session, "gate")[-1]["method"] == "probe"
    assert cmd(session, "version").captured == VERSION_REPLY


def test_profile_without_prompt_trusts_the_boot_after_ready_timeout(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory, profile: Profile
) -> None:
    device = make_fakedev()
    bare = dataclasses.replace(
        profile,
        console=dataclasses.replace(profile.console, prompt=None),
        boot=dataclasses.replace(profile.boot, ready=None, ready_timeout=0.1),
        commands={},
    )
    session = make_session(device, with_profile=bare)
    until(lambda: ready(session))
    assert "## probe" not in texts(session)
    sent = cmd(session, "version")
    assert (sent.status, sent.captured) == ("ok", None), "no prompt: fire and forget"
    with pytest.raises(UsageError, match=r"capture needs console\.prompt"):
        cmd(session, "version", capture=True)
    reply = cmd(session, "version", expect=r"Version:\s*(\S+)")
    assert reply.groups == [APP_VERSION]


# --- cmd -----------------------------------------------------------------------------------


def test_cmd_returns_its_tx_offset_and_a_capture_ending_at_the_prompt(
    ready_session: DeviceSession,
) -> None:
    result = cmd(ready_session, "version")
    lines = ready_session.log(result.tx_offset, None, None, 10)
    assert (lines[0].text, lines[0].meta) == ("## tx version", True)
    assert lines[1].text == f"{PROMPT}version", "the echo completes the pending prompt"
    assert result.captured == VERSION_REPLY
    assert ready_session.status().offset == result.tx_offset + 3

    reply = cmd(ready_session, "version", expect=r"Version:\s*(\S+)")
    assert (reply.status, reply.line, reply.groups) == ("ok", VERSION_REPLY, [APP_VERSION])

    failed = cmd(ready_session, "bogus", expect="CLEARED", error=["not found"])
    assert (failed.status, failed.line) == ("error", "command not found")

    captured_error = cmd(ready_session, "bogus", error=["not found"])
    assert (captured_error.status, captured_error.line) == ("error", "command not found")

    fired = cmd(ready_session, "version", capture=False)
    assert (fired.status, fired.captured, fired.line) == ("ok", None, None)

    tx = events(ready_session, "tx")[0]
    assert (tx["line"], tx["source"], tx["offset"]) == ("version", "cmd", result.tx_offset)


def test_deny_list_refuses_before_anything_is_sent(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    before = ready_session.status().offset
    with pytest.raises(UsageError, match="denied: the console stops answering"):
        cmd(ready_session, "deep_sleep 10")
    assert fakedev.received == []
    assert ready_session.status().offset == before
    with pytest.raises(UsageError, match="one line"):
        cmd(ready_session, "version\rreboot")


def test_capture_ends_at_a_log_line_printed_after_the_prompt(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory
) -> None:
    # The log line lands right after the prompt, so the prompt never sits alone in the tail.
    noisy = {"version": f"{VERSION_REPLY}\r\n{PROMPT}I (5000) wifi: beacon"}
    device = make_fakedev(DeviceScript(replies=noisy))
    session = make_session(device)
    until(lambda: ready(session))
    result = cmd(session, "version")
    assert (result.status, result.captured) == ("ok", VERSION_REPLY)


def test_shell_not_ready_is_a_timeout_result(session: DeviceSession) -> None:
    result = cmd(session, "version", timeout=0.0)
    assert (result.status, result.line) == ("timeout", "shell not ready (state=booting)")
    assert "## tx version" not in texts(session)


def test_expect_times_out_on_a_quiet_live_port(ready_session: DeviceSession) -> None:
    # hail: expect_times_out_on_a_quiet_live_port. The unit answers, never with the marker.
    started = time.monotonic()
    result = cmd(ready_session, "version", expect="CLEARED", error=["ERROR"], timeout=0.15)
    assert (result.status, result.line) == ("timeout", None)
    assert 0.15 <= time.monotonic() - started < 2.0


def test_expect_errors_on_disconnect(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory
) -> None:
    # hail: expect_errors_on_disconnect. The port dies while the reply is outstanding.
    device = make_fakedev(DeviceScript(slow_s=SLOW_S))
    session = make_session(device)
    until(lambda: ready(session))
    vanish = threading.Timer(0.1, device.vanish)
    vanish.start()
    started = time.monotonic()
    try:
        with pytest.raises(TransportLost, match="port lost waiting for the reply"):
            cmd(session, "slow", expect="slow: done", timeout=6.0)
    finally:
        vanish.join()
    assert time.monotonic() - started < 2.0


def test_port_lost_error_never_carries_a_secret_line(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory
) -> None:
    device = make_fakedev(DeviceScript(slow_s=SLOW_S))
    session = make_session(device)
    until(lambda: ready(session))
    vanish = threading.Timer(0.1, device.vanish)
    vanish.start()
    try:
        with pytest.raises(TransportLost) as lost:
            cmd(session, f"slow {SECRET}", expect="slow: done", timeout=6.0, secret=True)
    finally:
        vanish.join()
    assert SECRET not in str(lost.value)
    assert "<redacted>" in str(lost.value)


# --- wait ----------------------------------------------------------------------------------


def test_wait_since_a_mark_matches_and_absent_passes_and_fails(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    mark = ready_session.mark("before_version")
    assert ready_session.resolve("before_version") == mark
    assert texts(ready_session)[mark] == "## mark before_version"
    cmd(ready_session, "version")
    found = ready_session.wait(
        r"Version:\s*(\S+)", mark, 1.0, absent=None, window=None, order=None, progress=None
    )
    assert (found.result, found.line, found.groups) == ("match", VERSION_REPLY, [APP_VERSION])
    assert found.offset is not None and found.offset > mark

    quiet = ready_session.wait(
        None, mark, 0.1, absent="rst:", window=0.1, order=None, progress=None
    )
    assert quiet.result == "absent_ok"
    assert quiet.duration >= 0.1

    before_reset = ready_session.mark("before_reset")
    fakedev.reset()
    hit = ready_session.wait(
        None, before_reset, 1.0, absent="rst:", window=1.0, order=None, progress=None
    )
    assert hit.result == "absent_violated"
    assert hit.line is not None and hit.line.startswith("rst:0x1 (POWERON)")

    # The pattern must match with no absent hit before it.
    after_rst = ready_session.wait(
        "shell started", before_reset, 1.0, absent="rst:", window=None, order=None, progress=None
    )
    assert after_rst.result == "absent_violated"
    boot = ready_session.resolve("boot")
    clean = ready_session.wait(
        "shell started", boot + 1, 1.0, absent="rst:", window=None, order=None, progress=None
    )
    assert clean.result == "match"


def test_a_line_matching_pattern_and_absent_violates(ready_session: DeviceSession) -> None:
    both = ready_session.wait(
        r"console: shell started",
        ready_session.resolve("boot"),
        1.0,
        absent="shell started",
        window=None,
        order=None,
        progress=None,
    )
    assert (both.result, both.line) == ("absent_violated", "I (1150) console: shell started")


def test_wait_order_and_meta_lines_never_match(ready_session: DeviceSession) -> None:
    mark = ready_session.mark("m")
    cmd(ready_session, "version")
    ok = ready_session.wait(
        None,
        mark,
        0.5,
        absent=None,
        window=None,
        order=["version$", r"Version:\s*\S+"],
        progress=None,
    )
    assert ok.result == "order_ok"
    assert len(ok.order) == 2 and ok.order[0] < ok.order[1]
    backwards = ready_session.wait(
        None,
        mark,
        0.1,
        absent=None,
        window=None,
        order=[r"Version:\s*\S+", "version$"],
        progress=None,
    )
    assert backwards.result == "order_violated"
    assert backwards.line == f"{PROMPT}version"
    meta = ready_session.wait(
        "tx version", mark, 0.1, absent=None, window=None, order=None, progress=None
    )
    assert meta.result == "timeout"
    with pytest.raises(UsageError):
        ready_session.wait(None, mark, 0.1, absent=None, window=None, order=None, progress=None)


def test_window_without_absent_does_not_hold_the_match(ready_session: DeviceSession) -> None:
    found = ready_session.wait(
        "shell started", 0, 2.0, absent=None, window=2.0, order=None, progress=None
    )
    assert found.result == "match"
    assert found.duration < 1.0


def test_wait_reports_progress_and_times_out(
    ready_session: DeviceSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("benchctl.session.PROGRESS_S", 0.05)
    beats: list[dict[str, Any]] = []
    result = ready_session.wait(
        "never printed", 0, 0.2, absent=None, window=None, order=None, progress=beats.append
    )
    assert result.result == "timeout"
    assert len(beats) >= 2
    assert beats[0]["offset"] == ready_session.status().offset


# --- reconnect -----------------------------------------------------------------------------


def test_vanish_then_reappear_reconnects_on_the_new_path(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    old_path = fakedev.path
    fakedev.vanish()
    until(lambda: not ready_session.status().connected)
    new_path = fakedev.reappear()
    assert new_path != old_path
    until(lambda: ready_session.status().connected and ready(ready_session))

    [lost] = events(ready_session, "port_lost")
    [back] = events(ready_session, "port_back")
    assert back["path"] == new_path and back["gap"] > 0
    assert ready_session.status().port == new_path
    log_texts = texts(ready_session)
    assert log_texts[lost["offset"]].startswith("## port lost error=")
    assert log_texts[back["offset"]].startswith(f"## port back path={new_path} gap=")
    assert "## probe" in log_texts[back["offset"] :]
    gates = [(e["state"], e["method"]) for e in events(ready_session, "gate")][-3:]
    assert gates == [("unknown", None), ("settling", "probe"), ("ready", "probe")]

    assert cmd(ready_session, "version").captured == VERSION_REPLY


def test_a_boot_after_port_back_is_an_expected_reset(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    # Native USB, or an adapter that resets on open: the unit boots as the port comes back.
    fakedev.vanish()
    until(lambda: not ready_session.status().connected)
    fakedev.reappear()
    until(lambda: ready_session.status().connected)
    fakedev.reset()
    [back] = events(ready_session, "port_back")
    until(lambda: any(e["offset"] > back["offset"] for e in events(ready_session, "reset")))
    [reset] = [e for e in events(ready_session, "reset") if e["offset"] > back["offset"]]
    assert (reset["expected"], reset["cause"]) == (True, "port_back")


def test_reconnect_failures_are_noted_once_per_distinct_message(
    ready_session: DeviceSession,
    fakedev: FakeDevice,
    ports_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("benchctl.session.RECONNECT_INTERVAL", 0.02)
    serial = fakedev.serial_number

    def failures() -> list[str]:
        return [t for t in texts(ready_session) if t.startswith("## reconnect failed ")]

    def ambiguous() -> None:
        twins = [
            {"device": f"/dev/benchctl-twin-{n}", "vid": None, "pid": None, "serial_number": serial}
            for n in (1, 2)
        ]
        ports_file.write_text(json.dumps(twins), encoding="utf-8")

    unplugged = f"## reconnect failed no port matches serial={serial} (present: none)"
    twins_prefix = f"## reconnect failed 2 ports match serial={serial}: "

    def noted_both(since: int) -> bool:
        later = failures()[since:]
        return unplugged in later and any(t.startswith(twins_prefix) for t in later)

    # The first attempt may still see the old path listed (`cannot open …`): another message.
    fakedev.vanish()
    until(lambda: unplugged in failures())
    ambiguous()
    until(lambda: noted_both(0))
    time.sleep(0.2)  # ten more attempts on messages already noted
    first = failures()
    assert len(first) == len(set(first))

    fakedev.reappear()
    until(lambda: len(events(ready_session, "port_back")) == 1)
    fakedev.vanish()
    until(lambda: unplugged in failures()[len(first) :])
    ambiguous()
    until(lambda: noted_both(len(first)))
    again = failures()[len(first) :]
    assert len(again) == len(set(again)), "noted again, once, after a successful reopen"


def test_pause_reader_parks_and_a_port_closed_underneath_recovers(
    ready_session: DeviceSession, fakedev: FakeDevice
) -> None:
    with ready_session.pause_reader() as transport:
        assert transport.path == fakedev.path
        transport.write(b"version\r")
        raw = b""
        deadline = time.monotonic() + 1.0
        while PROMPT.encode() not in raw.split(b"\r\n")[-1] and time.monotonic() < deadline:
            raw += transport.read(4096, 0.05)
        assert VERSION_REPLY.encode() in raw, "the parked reader left the reply to us"
    assert VERSION_REPLY not in texts(ready_session)

    # esptool closes the handle when its connect fails; the reader reconnects on its own.
    with ready_session.pause_reader() as transport:
        transport.raw.close()
    until(lambda: bool(events(ready_session, "port_back")) and ready(ready_session))
    assert events(ready_session, "port_back")[0]["path"] == fakedev.path
    assert cmd(ready_session, "version").captured == VERSION_REPLY


# --- redaction, identity, env gate, feeds ----------------------------------------------------


def test_secret_echo_is_masked_in_store_and_serial_log(ready_session: DeviceSession) -> None:
    result = cmd(ready_session, f"wifi set bench {SECRET}", secret=True)
    assert (result.status, result.captured) == ("ok", "command not found")
    stored = texts(ready_session)
    assert stored[result.tx_offset] == "## tx <redacted>"
    assert stored[result.tx_offset + 1] == f"{PROMPT}<redacted>"
    assert not any(SECRET in text for text in stored)
    ready_session.close()
    serial_log = (ready_session.session_dir / "serial.log").read_text(encoding="utf-8")
    assert SECRET not in serial_log


def test_secret_echo_after_no_wait_is_masked(ready_session: DeviceSession) -> None:
    # Fire and forget: cmd returns at the TX, and the echo is stored after it returned.
    sent = cmd(ready_session, f"wifi set bench {SECRET}", secret=True, capture=False)
    until(lambda: len(texts(ready_session)) > sent.tx_offset + 1)
    assert texts(ready_session)[sent.tx_offset + 1] == f"{PROMPT}<redacted>"
    assert not any(SECRET in text for text in texts(ready_session))


def test_redaction_happens_before_storage(ready_session: DeviceSession) -> None:
    result = cmd(ready_session, "secret")
    assert result.captured == "sip_user=bench sip_pass=***"
    hidden = cmd(ready_session, "secret", secret=True)
    assert texts(ready_session)[hidden.tx_offset] == "## tx <redacted>"
    ready_session.note(f"operator typed sip_pass={SECRET}")
    ready_session.close()
    serial_log = (ready_session.session_dir / "serial.log").read_text(encoding="utf-8")
    event_log = (ready_session.session_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "sip_pass=***" in serial_log
    assert SECRET not in serial_log
    assert SECRET not in event_log


def test_identity_reads_boot_and_cmd_fields_and_flags_mismatches(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory, profile: Profile
) -> None:
    device = make_fakedev()
    with_cmd_fields = dataclasses.replace(
        profile,
        identity=[
            *profile.identity,
            IdentityField(name="app", source="cmd", send="version", regex=r"Version:\s*(\S+)"),
            IdentityField(
                name="shout",
                source="cmd",
                send="version",
                regex=r"VERSION:\s*(\S+)",
                filter="tr a-z A-Z",
            ),
        ],
    )
    session = make_session(
        device, with_profile=with_cmd_fields, expect={"project": "fakeapp", "fw_version": "9.9.9"}
    )
    found = session.identity()
    assert found.fields == {
        "fw_version": APP_VERSION,
        "project": "fakeapp",
        "app": APP_VERSION,
        "shout": APP_VERSION.upper(),
    }
    assert not found.verified
    assert found.mismatches == {"fw_version": ("9.9.9", APP_VERSION)}
    assert session.identity() is found, "cached until reprobe"
    assert session.status().identity == found
    [event] = events(session, "identity")
    assert event["mismatches"] == {"fw_version": ["9.9.9", APP_VERSION]}
    assert [e["source"] for e in events(session, "tx")] == ["identity", "identity"]
    session.mark("tagged")
    tagged = events(session, "mark")[-1]
    assert (tagged["fw"], tagged["dev"]) == (APP_VERSION, None), "no mac or cert_cn field"


def test_gate_check_requires_and_forbids_and_passes_without_an_env_gate(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory, profile: Profile
) -> None:
    device = make_fakedev()
    session = make_session(
        device,
        with_profile=dataclasses.replace(
            profile, env_gate=EnvGate(require=["shell started", "never"], forbid=["die temp"])
        ),
    )
    until(lambda: ready(session))
    checked = session.gate_check(0, 0.1)
    assert checked["ok"] is False
    assert checked["missing"] == ["never"]
    [hit] = checked["forbidden_hits"]
    assert hit["line"] == "I (420) board: die temp 41.5°C"
    assert session.log(hit["offset"], hit["offset"] + 1, None, 1)[0].text == hit["line"]

    session.profile = dataclasses.replace(profile, env_gate=EnvGate(require=["shell started"]))
    assert session.gate_check(0, 0.1) == {"ok": True, "missing": [], "forbidden_hits": []}
    session.profile = dataclasses.replace(profile, env_gate=None)
    assert session.gate_check(0, 0.1) == {"ok": True, "missing": [], "forbidden_hits": []}


def test_forbid_only_gate_watches_its_whole_timeout(
    make_fakedev: FakeDeviceFactory, make_session: SessionFactory, profile: Profile
) -> None:
    device = make_fakedev(DeviceScript(slow_s=10.0))  # `slow` answers 0.5 s later
    session = make_session(
        device, with_profile=dataclasses.replace(profile, env_gate=EnvGate(forbid=["slow: done"]))
    )
    until(lambda: ready(session))
    since = session.mark(None)
    cmd(session, "slow", capture=False)
    started = time.monotonic()
    caught = session.gate_check(since, 3.0)
    assert caught["ok"] is False
    [hit] = caught["forbidden_hits"]
    assert hit["line"] == "slow: done"
    assert time.monotonic() - started < 2.0

    since = session.mark(None)
    started = time.monotonic()
    assert session.gate_check(since, 0.3) == {"ok": True, "missing": [], "forbidden_hits": []}
    assert time.monotonic() - started >= 0.3


def test_log_marks_and_cursor_rules(ready_session: DeviceSession) -> None:
    assert ready_session.mark(None) == ready_session.status().offset
    with pytest.raises(UsageError):
        ready_session.mark("boot")
    with pytest.raises(UsageError):
        ready_session.mark("12")
    with pytest.raises(UsageError, match="unknown mark"):
        ready_session.resolve("nope")
    picked = ready_session.log(0, None, r"\(\d+\) boot:", 1)
    assert [line.text for line in picked] == ["I (24) boot: ESP-IDF v5.4 2nd stage bootloader"]
    assert ready_session.log(0, 2, None, 100) == ready_session.log(0, None, None, 2)
    with pytest.raises(UsageError):
        ready_session.log(0, None, None, -1)
    with pytest.raises(UsageError):
        ready_session.log(0, None, "(", 1)


def test_subscribers_get_lines_and_events_and_dead_feeds_are_dropped(
    ready_session: DeviceSession,
) -> None:
    feed = ready_session.subscribe()
    gone = ready_session.subscribe()
    gone.close()
    cmd(ready_session, "version")
    items: list[LogLine | Event] = []
    while (item := feed.get(0.2)) is not None:
        items.append(item)
    assert any(isinstance(i, LogLine) and i.text == "## tx version" for i in items)
    assert any(isinstance(i, LogLine) and i.text == VERSION_REPLY for i in items)
    assert [i["type"] for i in items if isinstance(i, dict)] == ["tx"]
    assert gone not in ready_session._subs
    full = Subscription(maxsize=1)
    ready_session._subs.append(full)
    ready_session.note("one")
    ready_session.note("two")
    assert full not in ready_session._subs
