"""Unit lease and the daemon's port lock (design Section 3.4).

`lease.lock` is held with flock for the daemon's whole life, so it is the daemon's liveness;
`lease.json` records which owner may drive the unit and is readable by any client.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from benchctl.defaults import iso_now
from benchctl.errors import BusyError, UsageError
from benchctl.types import Event

OWNER_ENV = "BENCHCTL_OWNER"
STALE_ENV = "BENCHCTL_LEASE_STALE_S"
DEFAULT_STALE_S = 1800.0


def default_owner(cwd: Path | None = None) -> str:
    """`BENCHCTL_OWNER`, else `<user>@<host>:<worktree root or cwd>`."""
    explicit = os.environ.get(OWNER_ENV)
    if explicit:
        return explicit
    here = (cwd or Path.cwd()).resolve()
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return f"{user}@{socket.gethostname()}:{_worktree_root(here)}"


def _worktree_root(cwd: Path) -> Path:
    # `.git` is a directory in a checkout and a file in a linked worktree; both mark the root.
    for directory in (cwd, *cwd.parents):
        if (directory / ".git").exists():
            return directory
    return cwd


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stale_after() -> float:
    """Seconds without a request from the owner after which another owner may take over."""
    text = os.environ.get(STALE_ENV)
    if not text:
        return DEFAULT_STALE_S
    try:
        value = float(text)
    except ValueError:
        raise UsageError(f"{STALE_ENV}={text!r}: expected seconds") from None
    if value < 0:
        raise UsageError(f"{STALE_ENV}={text!r}: must not be negative")
    return value


def lock_held(path: Path) -> bool:
    """Whether some process holds `path` (a daemon's `lease.lock`). Probing takes the lock for
    an instant when it is free; a lock that cannot even be opened counts as held."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


class PortLock:
    """`lease.lock`, held exclusively for the daemon's lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another holder has it."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the flock
            self._fd = None


def read_lease(path: Path) -> dict[str, Any] | None:
    """The lease in `path`, or None when there is none or it cannot be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("owner"), str):
        return None
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    # Clients read lease.json without the lock; they must never see half a file.
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(data), encoding="utf-8")
    os.replace(staging, path)


class LeaseManager:
    """The daemon's lease: who may run mutating ops. Every change is written to lease.json.

    Thread-safe; `emit` receives a `lease` event for every acquire, take, stale takeover and
    release.
    """

    def __init__(
        self,
        path: Path,
        emit: Callable[[Event], None],
        *,
        daemon_pid: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._emit = emit
        self._pid = os.getpid() if daemon_pid is None else daemon_pid
        self._clock = clock
        self._lock = threading.Lock()
        self._lease = read_lease(path)
        self._seen = self._parse_seen(self._lease)
        self._dirty = False

    def _parse_seen(self, lease: dict[str, Any] | None) -> float:
        if lease is None:
            return 0.0
        try:
            return datetime.fromisoformat(str(lease["last_seen"])).timestamp()
        except (KeyError, ValueError):
            return 0.0

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._lease is None else dict(self._lease)

    def _stale(self, lease: dict[str, Any]) -> bool:
        # This daemon holds lease.lock, so a lease written by any other daemon pid is a dead
        # daemon's lease.
        if lease.get("daemon_pid") != self._pid:
            return True
        return self._clock() - self._seen > stale_after()

    def owner_ok(self, owner: str) -> bool:
        """Whether `owner` may run mutating ops now (the lease is free, stale or its own)."""
        with self._lock:
            lease = self._lease
            return lease is None or lease["owner"] == owner or self._stale(lease)

    def admit(self, owner: str, *, mutating: bool) -> None:
        """Gate one request. The owner's requests refresh `last_seen`; a mutating request
        acquires a free or stale lease and raises BusyError when another owner holds it."""
        with self._lock:
            lease = self._lease
            if lease is not None and lease["owner"] == owner:
                if lease.get("daemon_pid") != self._pid:
                    self._set(owner, "acquire", None)
                else:
                    self._touch(lease)
                return
            if not mutating:
                return
            if lease is None:
                self._set(owner, "acquire", None)
            elif self._stale(lease):
                self._set(owner, "stale", lease["owner"])
            else:
                raise _busy(lease)

    def take(self, owner: str) -> dict[str, Any]:
        """Transfer the lease to `owner` whoever holds it."""
        with self._lock:
            lease = self._lease
            if lease is None:
                lease = self._set(owner, "acquire", None)
            elif lease["owner"] == owner and lease.get("daemon_pid") == self._pid:
                self._touch(lease)
            else:
                lease = self._set(owner, "take", lease["owner"])
            return dict(lease)

    def touch(self, owner: str) -> None:
        """Refresh `last_seen` while `owner`'s long op runs; never acquires."""
        with self._lock:
            lease = self._lease
            if (
                lease is not None
                and lease["owner"] == owner
                and lease.get("daemon_pid") == self._pid
            ):
                self._touch(lease)

    def release(self, owner: str) -> None:
        """Free the lease. Only its owner may, unless it is stale."""
        with self._lock:
            lease = self._lease
            if lease is None:
                return
            if lease["owner"] != owner and not self._stale(lease):
                raise _busy(lease)
            self._drop(lease)

    def release_all(self) -> None:
        """Free the lease whoever holds it; the daemon is going away."""
        with self._lock:
            if self._lease is not None:
                self._drop(self._lease)

    def flush(self) -> None:
        """Write a `last_seen` refreshed since the last write."""
        with self._lock:
            if self._dirty and self._lease is not None:
                _write_json(self._path, self._lease)
            self._dirty = False

    def _touch(self, lease: dict[str, Any]) -> None:
        self._seen = self._clock()
        lease["last_seen"] = iso_now(self._seen)
        self._dirty = True

    def _set(self, owner: str, action: str, prev: str | None) -> dict[str, Any]:
        self._seen = self._clock()
        stamp = iso_now(self._seen)
        lease = {"owner": owner, "since": stamp, "last_seen": stamp, "daemon_pid": self._pid}
        self._lease = lease
        _write_json(self._path, lease)
        self._dirty = False
        self._emit({"type": "lease", "action": action, "owner": owner, "prev_owner": prev})
        return lease

    def _drop(self, lease: dict[str, Any]) -> None:
        prev = lease["owner"]
        self._lease = None
        self._dirty = False
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        self._emit({"type": "lease", "action": "release", "owner": None, "prev_owner": prev})


def _busy(lease: dict[str, Any]) -> BusyError:
    return BusyError(
        f"busy: held by {lease['owner']} since {lease['since']}", data={"lease": dict(lease)}
    )
