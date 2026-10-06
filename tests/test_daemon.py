"""The daemon in-process over a FakeSession: protocol, ops, lease, identity (design 3, 4.2, 7.2)."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from benchctl import capture, daemon, flash, paths
from benchctl.client import Channel, Client
from benchctl.errors import BenchctlError, BusyError, DeviceError, UsageError
from benchctl.lease import PortLock
from benchctl.types import LogLine
from conftest import PROFILE_NAME, UNIT, install_profile
from fake_daemon import (
    APP_VERSION,
    FAKE_DAEMON_ENV,
    FAKE_MAC,
    FakeSession,
    fake_capture_on_session,
    fake_flash_on_session,
    register_unit,
)

PROGRESS_S = 0.05
_START_S = 5.0
_POLL_S = 0.01


@dataclass
class Served:
    stop: threading.Event
    thread: threading.Thread
    status: list[int]

    def client(self, owner: str = "alice", **kwargs: Any) -> Client:
        return Client(UNIT, owner=owner, **kwargs)

    def join(self) -> int:
        self.thread.join(_START_S)
        assert not self.thread.is_alive()
        return self.status[0]


def _wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _START_S
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(_POLL_S)


def start_daemon(expect: dict[str, str] | None = None) -> Served:
    stop = threading.Event()
    status: list[int] = []
    thread = threading.Thread(
        target=lambda: status.append(
            daemon.serve(
                UNIT,
                None,
                session_factory=FakeSession,
                stop=stop,
                progress_interval=PROGRESS_S,
            )
        ),
        daemon=True,
    )
    thread.start()
    _wait_for(lambda: paths.socket_path(UNIT).exists() or not thread.is_alive())
    return Served(stop, thread, status)


@pytest.fixture
def config(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install_profile(config_dir)
    register_unit(config_dir)
    monkeypatch.setattr(flash, "flash_on_session", fake_flash_on_session)
    monkeypatch.setattr(capture, "capture_on_session", fake_capture_on_session)
    return config_dir


@pytest.fixture
def served(config: Path) -> Iterator[Served]:
    served = start_daemon()
    yield served
    served.stop.set()
    served.thread.join(_START_S)


def raw_channel() -> Channel:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(paths.socket_path(UNIT)))
    return Channel(sock)


def exchange(channel: Channel, request: dict[str, Any]) -> dict[str, Any]:
    channel.send(request)
    reply = channel.recv(_START_S)
    assert reply is not None
    return reply


# --- protocol ----------------------------------------------------------------------------


def installed_profile(config: Path) -> Path:
    return config / "profiles" / PROFILE_NAME / "profile.toml"


def test_first_request_must_be_hello(served: Served, config: Path) -> None:
    channel = raw_channel()
    reply = exchange(channel, {"id": "1", "op": "status", "args": {}})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "protocol"
    hello = exchange(channel, {"id": "2", "op": "hello", "args": {"owner": "a", "client": "t"}})
    assert hello["ok"] is True
    result = hello["result"]
    assert result["daemon"].startswith("benchctl ")
    assert result["proto"] == 1
    assert result["unit"] == UNIT
    assert isinstance(result["offset"], int)
    assert result["lease"] is None
    assert result["owner_ok"] is True
    assert result["profile"] == {"name": PROFILE_NAME, "path": str(installed_profile(config))}
    channel.close()


def test_hello_refuses_another_profile(served: Served, config: Path) -> None:
    channel = raw_channel()
    other = {"owner": "a", "profile": "/elsewhere/profile.toml"}
    refused = exchange(channel, {"id": "1", "op": "hello", "args": other})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "usage"
    assert refused["error"]["message"].startswith(f"daemon for {UNIT} runs profile ")
    assert refused["error"]["data"]["client_profile"] == "/elsewhere/profile.toml"
    status = exchange(channel, {"id": "2", "op": "status", "args": {}})
    assert status["error"] == {
        "code": "protocol",
        "message": "first request must be hello",
        "data": {},
    }
    own = {"owner": "a", "profile": str(installed_profile(config))}
    assert exchange(channel, {"id": "3", "op": "hello", "args": own})["ok"] is True
    channel.close()


def test_unknown_op_and_bad_frames_are_protocol_errors(served: Served) -> None:
    channel = raw_channel()
    exchange(channel, {"id": "1", "op": "hello", "args": {"owner": "a"}})
    reply = exchange(channel, {"id": "2", "op": "frobnicate", "args": {}})
    assert reply == {
        "id": "2",
        "ok": False,
        "error": {"code": "protocol", "message": "unknown op 'frobnicate'", "data": {}},
    }
    channel.sock.sendall(b"not json\n")
    bad = channel.recv(_START_S)
    assert bad is not None
    assert bad["id"] is None
    assert bad["error"]["code"] == "protocol"
    channel.close()


def test_bad_args_are_usage_errors(served: Served) -> None:
    with served.client() as client, pytest.raises(UsageError, match="timeout"):
        client.request("wait", {"pattern": "x", "since": 0, "timeout": "soon"})


# --- cursors and marks -------------------------------------------------------------------


def test_cursor_strings_resolve_and_unknown_marks_are_usage(served: Served) -> None:
    with served.client() as client:
        assert client.resolve("attach") == 0
        assert client.resolve("boot") == 1  # the fake's POWERON line
        offset = client.mark("m1")
        assert client.resolve("m1") == offset
        lines, _ = client.log("m1", None, None, 10)
        assert lines[0].text == "## mark m1"
        with pytest.raises(UsageError, match="unknown mark"):
            client.wait("x", "nope", 0.1, absent=None, window=None, order=None)
        for op, args in (
            ("log", {"since": "nope"}),
            ("tail", {"since": "nope"}),
            ("gate", {"since": "nope"}),
        ):
            if op == "tail":
                feed = client.tail("nope", False)
                with pytest.raises(UsageError):
                    feed.get(_START_S)
            else:
                with pytest.raises(UsageError):
                    client.request(op, args)
        with pytest.raises(UsageError):
            client.request("log", {"since": -1})


@pytest.mark.parametrize("name", ["", "   ", "attach", "boot", "12", "-3"])
def test_mark_rejects_names_a_cursor_cannot_resolve(served: Served, name: str) -> None:
    with served.client() as client, pytest.raises(UsageError):
        client.mark(name)


def test_mark_without_name_returns_the_next_offset(served: Served) -> None:
    with served.client() as client:
        before = client.status().offset
        assert client.mark(None) == before
        assert client.status().offset == before  # no meta line for a bare mark


# --- ops ---------------------------------------------------------------------------------


def test_cmd_wait_log_and_the_events_they_leave(served: Served) -> None:
    with served.client() as client:
        start = client.mark("start")
        result = client.cmd(
            "version",
            expect=r"Version: (\S+)",
            error=[],
            capture=None,
            timeout=1.0,
            reboots=False,
            secret=False,
            force=False,
            source="cmd",
        )
        assert result.status == "ok"
        assert result.groups == [APP_VERSION]
        assert result.tx_offset > start
        found = client.wait(
            "Application Version", "start", 1.0, absent=None, window=None, order=None
        )
        assert found.result == "match"
        lines, nxt = client.log(result.tx_offset, None, None, 100)
        assert lines[0].text == "## tx version"
        assert nxt == client.status().offset
        session_dir = Path(client.status().session_dir)
    kinds = [
        json.loads(line)["type"] for line in (session_dir / "events.jsonl").read_text().splitlines()
    ]
    assert "reply" in kinds
    assert "wait" in kinds


def test_log_next_continues_after_a_limit(served: Served) -> None:
    with served.client() as client:
        lines, nxt = client.log("attach", None, None, 2)
        assert [line.offset for line in lines] == [0, 1]
        assert nxt == 2


def test_deny_list_is_enforced_by_the_daemon(served: Served) -> None:
    with served.client() as client, pytest.raises(UsageError, match="denied: "):
        client.request("cmd", {"line": "deep_sleep 10", "timeout": 1.0})


def test_reset_returns_the_request_offset_and_an_expected_reset(served: Served) -> None:
    with served.client() as client:
        before = client.status().offset
        offset, info = client.reset(False, 1.0)
        assert offset == before
        assert info is not None
        assert info.expected
        assert info.cause == "reset"
        assert info.offset > offset


def test_progress_frames_reach_the_client_during_a_wait(served: Served) -> None:
    seen: list[dict[str, Any]] = []

    def record(progress: dict[str, Any] | None) -> None:
        if progress is not None:
            seen.append(progress)

    with served.client(on_progress=record) as client:
        result = client.wait("never", "attach", 0.3, absent=None, window=None, order=None)
    assert result.result == "timeout"
    assert seen
    assert set(seen[0]) == {"elapsed", "offset", "note"}
    assert seen[0]["note"].startswith("waiting for never")


def test_tail_backfills_then_streams_lines_and_events(served: Served) -> None:
    with served.client() as client:
        feed = client.tail("attach", events=True)
        first = feed.get(_START_S)
        assert isinstance(first, LogLine)
        assert first.offset == 0
        client.mark("live")
        items: list[Any] = []
        deadline = time.monotonic() + _START_S
        while not any(isinstance(i, LogLine) and i.text == "## mark live" for i in items):
            assert time.monotonic() < deadline
            items.append(feed.get(0.1))
        assert any(isinstance(i, dict) and i.get("type") == "lease" for i in items)
        offsets = [i.offset for i in items if isinstance(i, LogLine)]
        assert offsets == sorted(set(offsets))  # no duplicates across backfill and feed
        feed.close()


def test_flash_needs_a_chip_before_touching_the_port(
    config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_path = config / "profiles" / "esp-idf" / "profile.toml"
    profile_path.write_text(profile_path.read_text().replace('chip = "esp32s3"\n', ""))

    def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("flash ran without a chip")

    monkeypatch.setattr(flash, "flash_on_session", must_not_run)
    image = tmp_path / "app.bin"
    image.write_bytes(b"\xe9" * 64)
    served = start_daemon()
    try:
        with served.client() as client, pytest.raises(UsageError, match="chip"):
            client.flash(
                [(0x10000, str(image), "app")], after="hard-reset", baud=460800, flash_args={}
            )
    finally:
        served.stop.set()
        served.join()


def test_flash_and_capture_go_through_u6(served: Served, tmp_path: Path) -> None:
    image = tmp_path / "app.bin"
    image.write_bytes(b"\xe9" * 64)
    with served.client() as client:
        result = client.flash(
            [(0x10000, str(image), "app")], after="hard-reset", baud=921600, flash_args={}
        )
        assert result.files[0].size == 64
        assert len(result.files[0].sha256) == 64
        assert result.reset is not None
        assert result.reset.cause == "flash"
        result = client.flash(
            [(0x10000, str(image), "app")], after="no-reset", baud=921600, flash_args={}
        )
        assert result.reset is None
        assert client.status().gate.state == "bootloader"
        lines, reset = client.capture(0.1)
        assert reset is not None
        assert lines[0].offset == reset.offset


# --- lease -------------------------------------------------------------------------------

MUTATING: tuple[tuple[str, dict[str, Any]], ...] = (
    ("mark", {"name": "x"}),
    ("cmd", {"line": "version", "timeout": 1.0}),
    ("reset", {"bootloader": False}),
    ("arm_reset", {"cause": "step:1", "window": 1.0}),
    ("identity", {"reprobe": False}),
    ("capture", {"seconds": 0.1}),
    ("flash", {"files": [{"addr": 0, "path": "/nonexistent", "name": "app"}]}),
    ("detach", {}),
)
READ_ONLY: tuple[tuple[str, dict[str, Any]], ...] = (
    ("status", {}),
    ("resolve", {"cursor": "attach"}),
    ("wait", {"pattern": "rst:", "since": "attach", "timeout": 0.1}),
    ("log", {"since": "attach"}),
    ("gate", {"since": "boot", "timeout": 0.1}),
)


def test_other_owner_is_busy_for_mutating_ops_only(served: Served) -> None:
    with served.client("alice") as alice, served.client("bob") as bob:
        alice.mark("held")
        for op, args in MUTATING:
            with pytest.raises(BusyError, match=r"^busy: held by alice since "):
                bob.request(op, args)
        for op, args in READ_ONLY:
            bob.request(op, args)
        assert bob.connect()["owner_ok"] is False
        with pytest.raises(BusyError):
            bob.lease("release")
        assert bob.lease("take")["lease"]["owner"] == "bob"
        with pytest.raises(BusyError, match="held by bob"):
            alice.mark(None)
        bob.lease("release")
        alice.mark(None)


def test_a_reader_first_does_not_hold_the_lease(served: Served) -> None:
    with served.client("watcher") as watcher, served.client("driver") as driver:
        watcher.wait("rst:", "attach", 0.1, absent=None, window=None, order=None)
        driver.cmd(
            "version",
            expect=None,
            error=[],
            capture=None,
            timeout=1.0,
            reboots=False,
            secret=False,
            force=False,
            source="cmd",
        )
        assert driver.connect()["lease"] is None or driver.status().lease["owner"] == "driver"


# --- identity ----------------------------------------------------------------------------


def test_identity_mismatch_leaves_the_daemon_unverified_until_override(config: Path) -> None:
    register_unit(config, expect={"mac": "11:22:33:44:55:66"})
    served = start_daemon()
    try:
        with served.client() as client:
            with pytest.raises(DeviceError) as caught:
                client.identity()
            assert caught.value.message == (
                f"identity mismatch: mac expected 11:22:33:44:55:66 got {FAKE_MAC}"
            )
            for op, args in MUTATING[:3]:
                with pytest.raises(DeviceError, match="identity mismatch"):
                    client.request(op, args)
            status = client.status()
            assert status.identity is not None
            assert status.identity.verified is False
            client.log("attach", None, None, 10)
            identity = client.identity(override=True)
            assert identity.verified
            assert identity.mismatches == {"mac": ("11:22:33:44:55:66", FAKE_MAC)}
            client.mark("after")
            assert client.status().identity.verified  # type: ignore[union-attr]
            events = Path(status.session_dir, "events.jsonl").read_text().splitlines()
            overrides = [json.loads(e) for e in events if '"override"' in e]
            assert overrides[0]["verified"] is True
            client.detach()  # detach works for the unverified, too
    finally:
        served.stop.set()
        served.join()


def lazy_identity(monkeypatch: pytest.MonkeyPatch, **knobs: Any) -> None:
    """The fake probes only inside identity(), as the real session does."""
    monkeypatch.setenv(FAKE_DAEMON_ENV, json.dumps({"identity_lazy": True, **knobs}))


def test_the_first_mutating_op_probes_an_unprobed_identity(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_unit(config, expect={"mac": "11:22:33:44:55:66"})
    lazy_identity(monkeypatch)
    served = start_daemon()
    try:
        with served.client() as client:
            assert client.status().identity is None
            client.log("attach", None, None, 10)  # read-only ops never probe
            assert client.status().identity is None
            with pytest.raises(DeviceError, match=r"^identity mismatch: mac expected 11:22"):
                client.mark("first")
            identity = client.status().identity
            assert identity is not None
            assert identity.verified is False
            client.detach()
    finally:
        served.stop.set()
        served.join()


def test_a_shell_that_never_comes_up_is_device_and_not_overridable(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lazy_identity(monkeypatch, identity_timeout=True)
    served = start_daemon()
    try:
        with served.client() as client:
            with pytest.raises(DeviceError, match=r"^identity: shell not ready \(state=") as caught:
                client.mark("first")
            assert caught.value.data["gate"]["state"] == "ready"
            with pytest.raises(DeviceError, match="shell not ready"):
                client.identity(override=True)
            assert client.status().identity is None  # left unprobed: the next op retries
            client.detach()
    finally:
        served.stop.set()
        served.join()


def test_progress_frames_cover_a_slow_identity_probe(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lazy_identity(monkeypatch, identity_delay_s=0.3)
    served = start_daemon()
    seen: list[dict[str, Any]] = []

    def record(progress: dict[str, Any] | None) -> None:
        if progress is not None:
            seen.append(progress)

    try:
        with served.client(on_progress=record) as client:
            client.mark("after-probe")  # `mark` alone never ticks; the pending probe does
            assert client.status().identity is not None
            assert seen
            assert seen[0]["note"] == "identity"
            client.detach()
    finally:
        served.stop.set()
        served.join()


def test_daemon_rejects_pattern_with_order(served: Served) -> None:
    with served.client() as client, pytest.raises(UsageError, match="exclusive"):
        client.wait("x", "attach", 0.1, absent=None, window=None, order=["a", "b"])


# --- lifecycle ---------------------------------------------------------------------------


def test_detach_cleans_up_and_records_why(served: Served) -> None:
    with served.client() as client:
        client.mark("x")
        session_dir = Path(client.status().session_dir)
        client.detach()
    assert served.join() == 0
    assert not paths.socket_path(UNIT).exists()
    assert not paths.pid_path(UNIT).exists()
    assert not paths.lease_path(UNIT).exists()
    events = [json.loads(e) for e in (session_dir / "events.jsonl").read_text().splitlines()]
    assert events[-2]["type"] == "lease"
    assert events[-2]["action"] == "release"
    assert events[-1] == {**events[-1], "type": "detach", "reason": "detach"}


def test_a_second_daemon_loses_the_lock_without_touching_anything(config: Path) -> None:
    paths.unit_dir(UNIT).mkdir(parents=True)
    holder = PortLock(paths.lease_lock_path(UNIT))
    assert holder.acquire()
    try:
        assert daemon.serve(UNIT, None, session_factory=FakeSession) == 3
        assert not paths.pid_path(UNIT).exists()
        assert not paths.socket_path(UNIT).exists()
        assert not paths.sessions_dir(UNIT).exists()
    finally:
        holder.release()


def test_startup_errors_exit_with_their_code(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert daemon.serve("u2", None, session_factory=FakeSession) == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert error["ok"] is False
    assert error["error"]["code"] == "usage"
    assert "u2" in error["error"]["message"]
    assert not paths.pid_path("u2").exists()


def test_internal_errors_do_not_kill_the_daemon(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    with served.client() as client:
        with pytest.raises(BenchctlError) as caught:
            client.request("flash", {"files": [{"addr": 0, "path": "/nonexistent", "name": "app"}]})
        assert caught.value.code in ("internal", "usage")
        assert client.status().unit == UNIT
