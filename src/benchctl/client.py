"""Client side of the socket protocol (design Sections 3.3, 7.2).

`Client` implements `types.DeviceClient`: one request in flight per connection, progress
frames passed to a hook, and auto-attach (spawn the daemon, poll for its socket) when asked.
`Channel` is the JSON-lines framing, shared with the daemon.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from benchctl import paths
from benchctl.errors import BenchctlError, DeviceError, UsageError, from_dict
from benchctl.lease import default_owner, pid_alive
from benchctl.types import (
    AppDesc,
    CmdResult,
    FlashFile,
    FlashResult,
    GateState,
    Identity,
    LogLine,
    Offset,
    ResetInfo,
    Status,
    Subscription,
    WaitResult,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

PROTO = 1
DAEMON_ARGV_ENV = "BENCHCTL_DAEMON_ARGV"
PROFILE_ENV = "BENCHCTL_PROFILE"
ATTACH_TIMEOUT_S = 15.0
SPAWN_POLL_S = 0.1
# A daemon that loses the lease.lock race exits with the `busy` status.
LOST_RACE_EXIT = 3
# Progress frames come every 5 s during blocking ops; this much silence means a dead daemon.
SILENCE_S = 30.0
RECV_TICK_S = 0.2
DETACH_WAIT_S = 5.0
# sun_path is 104 bytes on macOS (108 on Linux), terminator included.
SOCKET_PATH_MAX = 103
_RECV_CHUNK = 65536

ProgressHook = Callable[[dict[str, Any] | None], None]


class NotAttached(DeviceError):
    """No daemon answers on the unit's socket and the caller asked not to spawn one."""


class Channel:
    """One JSON object per line over a stream socket. Sends are serialised, so frames written
    from different threads (a reply and a progress tick) never interleave."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = bytearray()
        self._send_lock = threading.Lock()

    def send(self, frame: Mapping[str, Any]) -> None:
        data = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
        with self._send_lock:
            self.sock.sendall(data)

    def recv(self, timeout: float | None) -> dict[str, Any] | None:
        """The next frame, or None when `timeout` passes first. Raises EOFError when the peer
        closed and ValueError on a frame that is not a JSON object."""
        while (end := self._buf.find(b"\n")) < 0:
            self.sock.settimeout(timeout)
            try:
                chunk = self.sock.recv(_RECV_CHUNK)
            except TimeoutError:
                return None
            if not chunk:
                raise EOFError
            self._buf += chunk
        line = bytes(self._buf[:end])
        del self._buf[: end + 1]
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise ValueError("frame is not a JSON object")
        return frame

    def close(self) -> None:
        self.sock.close()


def check_socket_path(path: Path) -> None:
    if len(os.fsencode(path)) > SOCKET_PATH_MAX:
        raise UsageError(
            f"socket path {path} is longer than {SOCKET_PATH_MAX} bytes; "
            "set BENCHCTL_STATE_DIR to a shorter directory"
        )


def daemon_pid(unit: str) -> int | None:
    """The pid of the unit's running daemon, from its pidfile."""
    try:
        pid = int(paths.pid_path(unit).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def _try_connect(path: Path) -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        sock.close()
        return None
    except OSError as exc:
        sock.close()
        raise DeviceError(f"cannot connect to {path}: {exc}") from exc
    return sock


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _startup_failure(unit: str, status: int, log_start: int) -> BenchctlError:
    """The error our daemon printed as Section 7.1 JSON on its way out. daemon.log is shared
    and append-only, so only bytes written since our spawn count, and a racing daemon's
    lost-the-lock `busy` line is not ours."""
    log_path = paths.daemon_log_path(unit)
    try:
        with log_path.open("rb") as log:
            log.seek(log_start)
            lines = log.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        error = frame.get("error") if isinstance(frame, dict) else None
        if isinstance(error, dict) and error.get("code") != "busy":
            return from_dict(error)
    return DeviceError(
        f"daemon for {unit} exited with status {status}; see {log_path}",
        data={"log_tail": lines[-5:]},
    )


def _build[D: DataclassInstance](cls: type[D], data: Any, **nested: Any) -> D:
    """A dataclass from its JSON object; unknown keys are ignored so newer daemons still work."""
    if not isinstance(data, Mapping):
        raise BenchctlError(f"expected an object for {cls.__name__}", code="protocol")
    names = {f.name for f in dataclasses.fields(cls)}
    values = {key: value for key, value in data.items() if key in names}
    values.update(nested)
    try:
        return cls(**values)
    except TypeError as exc:
        raise BenchctlError(f"bad {cls.__name__} from daemon: {exc}", code="protocol") from exc


def decode_reset(data: Any) -> ResetInfo | None:
    return None if data is None else _build(ResetInfo, data, context=list(data["context"]))


def decode_identity(data: Any) -> Identity | None:
    if data is None:
        return None
    mismatches = {name: (pair[0], pair[1]) for name, pair in data["mismatches"].items()}
    return _build(Identity, data, fields=dict(data["fields"]), mismatches=mismatches)


def decode_status(data: Any) -> Status:
    return _build(
        Status,
        data,
        lease=dict(data.get("lease") or {}),
        identity=decode_identity(data.get("identity")),
        last_reset=decode_reset(data.get("last_reset")),
        gate=_build(GateState, data["gate"]),
    )


def decode_line(data: Any) -> LogLine:
    return _build(LogLine, data)


def decode_flash(data: Any) -> FlashResult:
    app = data.get("app")
    return _build(
        FlashResult,
        data,
        files=[_build(FlashFile, item) for item in data["files"]],
        app=None if app is None else _build(AppDesc, app),
        reset=decode_reset(data.get("reset")),
    )


class Client:
    """A `DeviceClient` over the unit's socket. Connects on the first request; with `spawn`
    it starts the daemon when none answers, which resets the unit (Section 3.3)."""

    def __init__(
        self,
        unit: str,
        *,
        owner: str | None = None,
        spawn: bool = False,
        attach_timeout: float = ATTACH_TIMEOUT_S,
        on_progress: ProgressHook | None = None,
        spawn_profile: str | None = None,
        client_name: str = "cli",
    ) -> None:
        self.unit = paths.check_unit_name(unit)
        self.owner = owner or default_owner()
        self.spawn = spawn
        self.attach_timeout = attach_timeout
        self.on_progress = on_progress
        self.spawn_profile = spawn_profile
        self.client_name = client_name
        self._channel: Channel | None = None
        self._hello: dict[str, Any] = {}
        self._ids = itertools.count(1)
        self._proc: subprocess.Popen[bytes] | None = None

    # --- connection ------------------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Open the connection if needed; returns the `hello` result. Raises NotAttached when
        no daemon answers and `spawn` is off."""
        if self._channel is None:
            self._channel = Channel(self._open_socket())
            self._hello = self._exchange(
                self._channel, "hello", {"owner": self.owner, "client": self.client_name}
            )
        return self._hello

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Send one request and return its `result`; raises the daemon's error."""
        self.connect()
        if self._channel is None:  # connect() always sets it; this narrows the type
            raise DeviceError("not connected")
        return self._exchange(self._channel, op, args)

    def _open_socket(self) -> socket.socket:
        path = paths.socket_path(self.unit)
        check_socket_path(path)
        sock = _try_connect(path)
        if sock is not None:
            return sock
        if not self.spawn:
            raise NotAttached(f"unit {self.unit} is not attached")
        return self._spawn_and_connect(path)

    def _spawn_and_connect(self, path: Path) -> socket.socket:
        deadline = time.monotonic() + self.attach_timeout
        proc: subprocess.Popen[bytes] | None = None
        log_start = 0
        while True:
            sock = _try_connect(path)
            if sock is not None:
                self._proc = proc
                return sock
            if proc is None:
                # Re-checked every poll: a daemon another client spawned may die starting up.
                if daemon_pid(self.unit) is None:
                    if path.exists():
                        path.unlink(missing_ok=True)  # its daemon is dead: a stale socket
                    log_start = _log_size(paths.daemon_log_path(self.unit))
                    proc = self._spawn()
            elif proc.poll() is not None and proc.returncode != LOST_RACE_EXIT:
                raise _startup_failure(self.unit, proc.returncode, log_start)
            if time.monotonic() >= deadline:
                raise DeviceError(
                    f"daemon for {self.unit} did not come up within {self.attach_timeout:g} s; "
                    f"see {paths.daemon_log_path(self.unit)}"
                )
            time.sleep(SPAWN_POLL_S)

    def _spawn(self) -> subprocess.Popen[bytes]:
        # The env var replaces the program, not `--unit`, so one setting serves every unit.
        override = os.environ.get(DAEMON_ARGV_ENV)
        program = (
            shlex.split(override) if override else [sys.executable, "-m", "benchctl", "daemon"]
        )
        env = dict(os.environ)
        if self.spawn_profile:
            env[PROFILE_ENV] = self.spawn_profile
        log_path = paths.daemon_log_path(self.unit)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            return subprocess.Popen(
                [*program, "--unit", self.unit],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=env,
                start_new_session=True,
            )

    def _exchange(self, channel: Channel, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        req_id = str(next(self._ids))
        try:
            channel.send({"id": req_id, "op": op, "args": dict(args)})
        except OSError as exc:
            raise DeviceError(f"daemon for {self.unit} went away: {exc}") from exc
        heard = time.monotonic()
        while True:
            try:
                frame = channel.recv(RECV_TICK_S)
            except EOFError:
                raise DeviceError(f"daemon for {self.unit} closed the connection") from None
            except ValueError as exc:
                raise BenchctlError(f"bad frame from daemon: {exc}", code="protocol") from exc
            except OSError as exc:
                raise DeviceError(f"daemon for {self.unit} went away: {exc}") from exc
            if frame is None:
                if time.monotonic() - heard > SILENCE_S:
                    raise DeviceError(f"daemon for {self.unit} silent for {SILENCE_S:g} s")
                self._tick(None)
                continue
            heard = time.monotonic()
            # Errors the daemon could not tie to a request carry id null.
            if frame.get("id") not in (req_id, None):
                continue
            if "progress" in frame:
                self._tick(frame["progress"])
            elif frame.get("ok") is True:
                result = frame.get("result")
                return result if isinstance(result, dict) else {}
            elif frame.get("ok") is False:
                raise from_dict(frame.get("error") or {})
            else:
                raise BenchctlError(f"unexpected frame from daemon: {frame}", code="protocol")

    def _tick(self, progress: dict[str, Any] | None) -> None:
        if self.on_progress is not None:
            self.on_progress(progress)

    # --- DeviceClient ----------------------------------------------------------------

    def hello(self, owner: str, client: str) -> dict[str, Any]:
        self.owner, self.client_name = owner, client
        if self._channel is None:
            return self.connect()
        self._hello = self._exchange(self._channel, "hello", {"owner": owner, "client": client})
        return self._hello

    def status(self) -> Status:
        return decode_status(self.request("status", {}))

    def mark(self, name: str | None) -> Offset:
        return int(self.request("mark", {"name": name})["offset"])

    def resolve(self, cursor: str) -> Offset:
        return int(self.request("resolve", {"cursor": cursor})["offset"])

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
        args = {
            "line": line,
            "expect": expect,
            "error": list(error),
            "capture": capture,
            "timeout": timeout,
            "reboots": reboots,
            "secret": secret,
            "force": force,
            "source": source,
        }
        return _build(CmdResult, self.request("cmd", args))

    def wait(
        self,
        pattern: str | None,
        since: Offset | str,
        timeout: float,
        *,
        absent: str | None,
        window: float | None,
        order: list[str] | None,
    ) -> WaitResult:
        args = {
            "pattern": pattern,
            "since": since,
            "timeout": timeout,
            "absent": absent,
            "window": window,
            "order": order,
        }
        return _build(WaitResult, self.request("wait", args))

    def log(
        self, since: Offset | str, until: Offset | str | None, grep: str | None, limit: int
    ) -> tuple[list[LogLine], Offset]:
        result = self.request("log", {"since": since, "until": until, "grep": grep, "limit": limit})
        return [decode_line(line) for line in result["lines"]], int(result["next"])

    def reset(self, bootloader: bool, wait_rst: float = 5.0) -> tuple[Offset, ResetInfo | None]:
        result = self.request("reset", {"bootloader": bootloader, "wait_rst": wait_rst})
        return int(result["offset"]), decode_reset(result.get("reset"))

    def arm_reset(self, cause: str, window: float) -> None:
        self.request("arm_reset", {"cause": cause, "window": window})

    def flash(
        self,
        files: list[tuple[int, str, str]],
        *,
        after: str,
        baud: int,
        flash_args: dict[str, str],
    ) -> FlashResult:
        args = {
            "files": [{"addr": addr, "path": path, "name": name} for addr, path, name in files],
            "after": after,
            "baud": baud,
            "flash_args": dict(flash_args),
        }
        return decode_flash(self.request("flash", args))

    def capture(self, seconds: float) -> tuple[list[LogLine], ResetInfo | None]:
        result = self.request("capture", {"seconds": seconds})
        return [decode_line(line) for line in result["lines"]], decode_reset(result.get("reset"))

    def identity(self, reprobe: bool = False, *, override: bool = False) -> Identity:
        """`override` is `attach --no-verify`: accept a mismatched identity for this daemon."""
        result = decode_identity(
            self.request("identity", {"reprobe": reprobe, "override": override})
        )
        if result is None:
            raise BenchctlError("daemon returned no identity", code="protocol")
        return result

    def gate(self, since: Offset | str, timeout: float) -> dict[str, Any]:
        return self.request("gate", {"since": since, "timeout": timeout})

    def tail(self, since: Offset | str, events: bool) -> Subscription:
        """Stream from a second connection: one connection carries one request at a time and
        a tail never ends on its own. Closing the Subscription closes that connection."""
        channel = Channel(self._open_socket())
        self._exchange(channel, "hello", {"owner": self.owner, "client": self.client_name})
        feed = Subscription()
        channel.send({"id": "tail", "op": "tail", "args": {"since": since, "events": events}})
        threading.Thread(
            target=_pump_tail, args=(channel, feed), name=f"tail-{self.unit}", daemon=True
        ).start()
        return feed

    def lease(self, action: Literal["take", "release"]) -> dict[str, Any]:
        return self.request("lease", {"action": action})

    def detach(self) -> None:
        """Stop the daemon and return once it has cleaned up (pidfile gone), bounded."""
        self.request("detach", {})
        self.close()
        pid_file = paths.pid_path(self.unit)
        deadline = time.monotonic() + DETACH_WAIT_S
        while pid_file.exists() and time.monotonic() < deadline:
            time.sleep(SPAWN_POLL_S)
        if self._proc is not None:
            self._proc.poll()  # reap our own child so it does not linger as a zombie


def _pump_tail(channel: Channel, feed: Subscription) -> None:
    try:
        while not feed.closed:
            try:
                frame = channel.recv(RECV_TICK_S)
            except EOFError:
                feed.fail(DeviceError("daemon closed the tail"))
                return
            except (OSError, ValueError) as exc:
                feed.fail(DeviceError(f"tail failed: {exc}"))
                return
            if frame is None:
                continue
            if frame.get("ok") is False:
                feed.fail(from_dict(frame.get("error") or {}))
                return
            try:
                item = decode_line(frame["line"]) if "line" in frame else dict(frame["event"])
            except (BenchctlError, KeyError, TypeError, ValueError) as exc:
                feed.fail(BenchctlError(f"bad tail frame: {exc}", code="protocol"))
                return
            if not feed.offer(item):
                return
    finally:
        channel.close()
