"""Client auto-attach against a spawned fake daemon (design Section 3.3)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from benchctl import client as client_mod
from benchctl import paths
from benchctl.client import DAEMON_ARGV_ENV, Client, NotAttached, daemon_pid
from benchctl.errors import BenchctlError, DeviceError, UsageError
from benchctl.types import ResetInfo, Status
from conftest import UNIT, install_profile
from fake_daemon import FAKE_DAEMON_ENV, daemon_argv, reap_daemons, register_unit


@pytest.fixture
def spawnable(config_dir: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    install_profile(config_dir)
    register_unit(config_dir)
    monkeypatch.setenv(DAEMON_ARGV_ENV, daemon_argv())
    monkeypatch.setenv(FAKE_DAEMON_ENV, json.dumps({"progress_s": 0.1}))
    yield state_dir
    reap_daemons(state_dir)


def test_auto_attach_spawns_the_daemon_and_says_hello(spawnable: Path) -> None:
    with Client(UNIT, owner="alice", spawn=True) as client:
        hello = client.connect()
        assert hello["unit"] == UNIT
        status = client.status()
        assert isinstance(status, Status)
        assert status.pid != os.getpid()
        assert daemon_pid(UNIT) == status.pid
        assert isinstance(status.last_reset, ResetInfo)
        assert (spawnable / UNIT / "daemon.log").exists()
        client.detach()
    assert daemon_pid(UNIT) is None
    assert not paths.socket_path(UNIT).exists()


def test_without_spawn_nothing_starts(spawnable: Path) -> None:
    with Client(UNIT) as client, pytest.raises(NotAttached):
        client.connect()
    assert not paths.pid_path(UNIT).exists()


def test_two_clients_racing_attach_share_one_daemon(
    spawnable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = threading.Barrier(2)
    pids: list[int] = []
    errors: list[BaseException] = []
    spawned: list[int] = []
    real_spawn = Client._spawn

    def counting_spawn(self: Client) -> subprocess.Popen[bytes]:
        proc = real_spawn(self)
        spawned.append(proc.pid)
        return proc

    monkeypatch.setattr(Client, "_spawn", counting_spawn)

    def attach(owner: str) -> None:
        try:
            with Client(UNIT, owner=owner, spawn=True) as client:
                start.wait()
                client.connect()
                pids.append(client.status().pid)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attach, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(pids) == 2
    assert pids[0] == pids[1] == daemon_pid(UNIT)
    if len(spawned) == 2:  # both saw no daemon: the loser exited 3 and its client found the winner
        assert "another daemon holds" in paths.daemon_log_path(UNIT).read_text()
    with Client(UNIT, owner="a") as client:
        client.detach()


def test_a_daemon_that_cannot_start_reports_its_error(spawnable: Path, config_dir: Path) -> None:
    (config_dir / "units.toml").unlink()  # the daemon will not find its unit
    client = Client(UNIT, spawn=True, attach_timeout=10)
    with client, pytest.raises(UsageError, match="u1"):
        client.connect()
    assert daemon_pid(UNIT) is None


def test_an_older_error_in_daemon_log_is_not_relayed(
    spawnable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = paths.daemon_log_path(UNIT)
    log.parent.mkdir(parents=True)
    stale = {"ok": False, "error": {"code": "usage", "message": "old", "data": {}}}
    log.write_text(json.dumps(stale) + "\n")
    # A daemon that dies without printing its error JSON.
    monkeypatch.setenv(DAEMON_ARGV_ENV, f"{sys.executable} -c 'raise SystemExit(5)'")
    client = Client(UNIT, spawn=True)
    with client, pytest.raises(DeviceError, match="exited with status 5"):
        client.connect()


def test_a_race_losers_busy_line_is_not_the_error(spawnable: Path) -> None:
    log = paths.daemon_log_path(UNIT)
    log.parent.mkdir(parents=True)
    log.write_text("")
    busy = {"ok": False, "error": {"code": "busy", "message": "lost", "data": {}}}
    with log.open("a") as out:
        out.write(json.dumps(busy) + "\n")
    assert client_mod._startup_failure(UNIT, 4, 0).code == "device"


def test_a_pidfile_naming_an_unrelated_live_process_is_stale(spawnable: Path) -> None:
    # After a reboot the pidfile's pid can belong to anything; lease.lock is free then.
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        paths.unit_dir(UNIT).mkdir(parents=True)
        paths.pid_path(UNIT).write_text(f"{unrelated.pid}\n")
        with Client(UNIT, spawn=True) as client:
            assert client.status().pid not in (unrelated.pid, os.getpid())
            client.detach()
    finally:
        unrelated.kill()
        unrelated.wait()


def test_a_lost_race_is_forgotten_and_spawned_again(
    spawnable: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "lost-once"
    config = {"progress_s": 0.1, "lose_race_once": str(marker)}
    monkeypatch.setenv(FAKE_DAEMON_ENV, json.dumps(config))
    with Client(UNIT, spawn=True) as client:
        assert client.connect()["unit"] == UNIT  # no daemon ever won: we spawned twice
        assert marker.exists()
        client.detach()


def test_a_stale_socket_is_replaced(spawnable: Path) -> None:
    sock_path = paths.socket_path(UNIT)
    sock_path.parent.mkdir(parents=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))  # bound, never listening: what a crashed daemon leaves
    stale.close()
    paths.pid_path(UNIT).write_text("999999999\n")
    with Client(UNIT, spawn=True) as client:
        assert client.connect()["unit"] == UNIT
        client.detach()


def test_socket_paths_past_the_af_unix_limit_are_usage(
    spawnable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BENCHCTL_STATE_DIR", "/tmp/" + "x" * 120)
    with Client(UNIT, spawn=True) as client, pytest.raises(UsageError, match="longer than"):
        client.connect()


def test_a_daemon_that_goes_silent_is_an_error(
    spawnable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_mod, "SILENCE_S", 0.3)
    monkeypatch.setenv(FAKE_DAEMON_ENV, json.dumps({"progress_s": 5.0}))
    with Client(UNIT, spawn=True) as client, pytest.raises(BenchctlError, match="silent"):
        client.wait("never", "attach", 2.0, absent=None, window=None, order=None)
    with Client(UNIT) as client:
        client.detach()
