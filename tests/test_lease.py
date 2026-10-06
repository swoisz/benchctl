"""Lease and port lock (design Section 3.4)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from benchctl.errors import BusyError, UsageError
from benchctl.lease import (
    DEFAULT_STALE_S,
    OWNER_ENV,
    STALE_ENV,
    LeaseManager,
    PortLock,
    default_owner,
    pid_alive,
    read_lease,
    stale_after,
)
from benchctl.types import Event

DEAD_PID = 2**22 + 12345  # above every default pid_max


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def lease_file(tmp_path: Path) -> Path:
    return tmp_path / "lease.json"


@pytest.fixture
def manager(lease_file: Path, events: list[Event], clock: Clock) -> LeaseManager:
    return LeaseManager(lease_file, events.append, daemon_pid=4242, clock=clock)


def test_owner_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OWNER_ENV, "agent-7")
    assert default_owner() == "agent-7"


def test_owner_is_user_at_host_and_worktree_root(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")  # a linked worktree's .git is a file
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    owner = default_owner(nested)
    assert "@" in owner
    assert owner.endswith(f":{tmp_path.resolve()}")


def test_owner_outside_a_checkout_is_the_cwd(tmp_path: Path) -> None:
    assert default_owner(tmp_path).endswith(f":{tmp_path.resolve()}")


def test_port_lock_is_exclusive_until_released(tmp_path: Path) -> None:
    first, second = PortLock(tmp_path / "lease.lock"), PortLock(tmp_path / "lease.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(DEAD_PID)
    assert not pid_alive(0)


def test_stale_after_reads_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert stale_after() == DEFAULT_STALE_S
    monkeypatch.setenv(STALE_ENV, "2.5")
    assert stale_after() == 2.5
    monkeypatch.setenv(STALE_ENV, "soon")
    with pytest.raises(UsageError):
        stale_after()


def test_first_mutating_request_acquires_and_writes_the_file(
    manager: LeaseManager, lease_file: Path, events: list[Event]
) -> None:
    manager.admit("alice", mutating=False)
    assert manager.current() is None
    manager.admit("alice", mutating=True)
    data = json.loads(lease_file.read_text())
    assert set(data) == {"owner", "since", "last_seen", "daemon_pid"}
    assert data["owner"] == "alice"
    assert data["daemon_pid"] == 4242
    assert events == [{"type": "lease", "action": "acquire", "owner": "alice", "prev_owner": None}]


def test_other_owner_is_busy_for_mutating_and_admitted_read_only(manager: LeaseManager) -> None:
    manager.admit("alice", mutating=True)
    manager.admit("bob", mutating=False)
    with pytest.raises(BusyError) as caught:
        manager.admit("bob", mutating=True)
    since = manager.current()["since"]  # type: ignore[index]
    assert caught.value.message == f"busy: held by alice since {since}"
    assert caught.value.exit_code == 3
    assert manager.owner_ok("alice")
    assert not manager.owner_ok("bob")


def test_take_transfers(manager: LeaseManager, events: list[Event]) -> None:
    manager.admit("alice", mutating=True)
    taken = manager.take("bob")
    assert taken["owner"] == "bob"
    assert events[-1] == {"type": "lease", "action": "take", "owner": "bob", "prev_owner": "alice"}
    with pytest.raises(BusyError):
        manager.admit("alice", mutating=True)


def test_idle_lease_goes_stale_and_is_taken_over(
    manager: LeaseManager, clock: Clock, events: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STALE_ENV, "60")
    manager.admit("alice", mutating=True)
    clock.now += 30
    manager.admit("alice", mutating=False)  # the owner's reads keep it fresh
    clock.now += 59
    with pytest.raises(BusyError):
        manager.admit("bob", mutating=True)
    clock.now += 2
    manager.admit("bob", mutating=True)
    assert manager.current()["owner"] == "bob"  # type: ignore[index]
    assert events[-1]["action"] == "stale"
    assert events[-1]["prev_owner"] == "alice"


def test_a_dead_daemons_lease_is_stale(lease_file: Path, events: list[Event], clock: Clock) -> None:
    lease_file.write_text(
        json.dumps(
            {
                "owner": "alice",
                "since": "x",
                "last_seen": "2999-01-01T00:00:00+00:00",
                "daemon_pid": 1,
            }
        )
    )
    manager = LeaseManager(lease_file, events.append, daemon_pid=4242, clock=clock)
    assert manager.owner_ok("bob")
    manager.admit("bob", mutating=True)
    assert events[-1]["action"] == "stale"


def test_release_by_owner_only(manager: LeaseManager, lease_file: Path) -> None:
    manager.admit("alice", mutating=True)
    with pytest.raises(BusyError):
        manager.release("bob")
    manager.release("alice")
    assert manager.current() is None
    assert not lease_file.exists()
    assert read_lease(lease_file) is None


def test_flush_writes_a_refreshed_last_seen(
    manager: LeaseManager, lease_file: Path, clock: Clock
) -> None:
    manager.admit("alice", mutating=True)
    before = json.loads(lease_file.read_text())["last_seen"]
    clock.now += 10
    manager.admit("alice", mutating=False)
    assert json.loads(lease_file.read_text())["last_seen"] == before
    manager.flush()
    assert json.loads(lease_file.read_text())["last_seen"] != before


def test_release_all_on_shutdown(manager: LeaseManager, events: list[Event]) -> None:
    manager.admit("alice", mutating=True)
    manager.release_all()
    assert manager.current() is None
    assert events[-1] == {
        "type": "lease",
        "action": "release",
        "owner": None,
        "prev_owner": "alice",
    }
