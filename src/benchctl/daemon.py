"""The per-unit daemon (design Sections 3, 4.2, 7.2): owns the session, serves the socket.

Startup order is lock first: a daemon that loses the `lease.lock` race exits 3 without
touching the pidfile, the socket or the port. The session is built before the socket is
bound, so a connected client always talks to a live session.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
import re
import select
import signal
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import FrameType
from typing import Any

from benchctl import __version__, paths
from benchctl import capture as capture_mod
from benchctl import flash as flash_mod
from benchctl.client import PROTO, Channel, check_socket_path
from benchctl.defaults import LOG_LIMIT, PROGRESS_S, RESET_WAIT_S, WAIT_TIMEOUT_S
from benchctl.errors import (
    BenchctlError,
    BusyError,
    DeviceError,
    InternalError,
    TimeoutError_,
    UsageError,
)
from benchctl.lease import LeaseManager, PortLock, default_owner
from benchctl.profile import FLASH_AFTER, Profile, load_profile
from benchctl.registry import Unit, load_registry
from benchctl.types import Cursor, Event, Identity, LogLine, Offset, Session

LEASE_FLUSH_S = 5.0
ACCEPT_TICK_S = 0.2
TAIL_TICK_S = 0.2
LISTEN_BACKLOG = 16

# Section 3.4's mutating set.
MUTATING_OPS = frozenset(
    {"cmd", "reset", "flash", "mark", "arm_reset", "detach", "identity", "capture"}
)
# Mutating ops an unverified daemon still runs: `identity` reports or overrides the
# mismatch itself, and `detach` is the other way out (Section 4.12).
UNVERIFIED_OK = frozenset({"identity", "detach"})
PROGRESS_OPS = frozenset({"cmd", "wait", "flash", "gate", "identity", "reset", "capture"})

# Builds the attached session; it owns port resolution and its sessions/<attach-ts>/ dir.
SessionFactory = Callable[[Unit, Profile], Session]


def _jsonable(value: Any) -> Any:
    is_instance = dataclasses.is_dataclass(value) and not isinstance(value, type)
    return dataclasses.asdict(value) if is_instance else value


def _error_frame(req_id: Any, error: BenchctlError) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "error": error.to_dict()}


def _protocol(message: str) -> BenchctlError:
    return BenchctlError(message, code="protocol")


# --- argument parsing --------------------------------------------------------------------


class _Args:
    """One request's `args`, with errors that name the op and key (code `usage`)."""

    def __init__(self, op: str, args: Any) -> None:
        if not isinstance(args, Mapping):
            raise UsageError(f"{op}: args must be an object")
        self.op = op
        self.data = args

    def _bad(self, key: str, expected: str) -> UsageError:
        return UsageError(f"{self.op}: {key}: expected {expected}, got {self.data.get(key)!r}")

    def text(self, key: str, default: str | None = None) -> str | None:
        value = self.data.get(key)
        if value is None:
            return default
        if not isinstance(value, str):
            raise self._bad(key, "a string")
        return value

    def req_text(self, key: str) -> str:
        value = self.text(key)
        if value is None:
            raise UsageError(f"{self.op}: {key}: required")
        return value

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.data.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self._bad(key, "true or false")
        return value

    def opt_flag(self, key: str) -> bool | None:
        return None if self.data.get(key) is None else self.flag(key)

    def number(self, key: str, default: float | None) -> float | None:
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise self._bad(key, "a non-negative number")
        return float(value)

    def seconds(self, key: str, default: float) -> float:
        value = self.number(key, default)
        return default if value is None else value

    def integer(self, key: str, default: int) -> int:
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise self._bad(key, "a positive integer")
        return value

    def regex(self, key: str) -> str | None:
        pattern = self.text(key)
        if pattern is not None:
            _compile(f"{self.op}: {key}", pattern)
        return pattern

    def regex_list(self, key: str) -> list[str] | None:
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise self._bad(key, "a list of strings")
        for item in value:
            _compile(f"{self.op}: {key}", item)
        return list(value)


def _compile(where: str, pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise UsageError(f"{where}: invalid regex {pattern!r}: {exc}") from None


def _mismatch_message(identity: Identity) -> str:
    parts = [
        f"{name} expected {expected} got {got}"
        for name, (expected, got) in identity.mismatches.items()
    ]
    return "identity mismatch: " + ("; ".join(parts) or "unverified")


@dataclasses.dataclass
class _Conn:
    channel: Channel
    owner: str | None = None


@dataclasses.dataclass(frozen=True)
class _Request:
    id: str
    note: Callable[[dict[str, Any]], None]  # updates the progress frame's note


# --- the daemon --------------------------------------------------------------------------


class Daemon:
    """Serves one session on one socket. `run` returns when `stop` is set (by `detach` or the
    caller); every op runs on the connection's handler thread."""

    def __init__(
        self,
        session: Session,
        unit: Unit,
        lease: LeaseManager,
        *,
        stop: threading.Event | None = None,
        progress_interval: float = PROGRESS_S,
    ) -> None:
        self.session = session
        self.unit = unit
        self.lease = lease
        self.progress_interval = progress_interval
        self.stop = threading.Event() if stop is None else stop
        self.stop_reason = "signal"
        self._override = False
        self._state_lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._ops: dict[str, Callable[[_Args, _Conn, _Request], dict[str, Any]]] = {
            "hello": self._op_hello,
            "status": self._op_status,
            "mark": self._op_mark,
            "resolve": self._op_resolve,
            "cmd": self._op_cmd,
            "wait": self._op_wait,
            "log": self._op_log,
            "reset": self._op_reset,
            "arm_reset": self._op_arm_reset,
            "flash": self._op_flash,
            "capture": self._op_capture,
            "identity": self._op_identity,
            "gate": self._op_gate,
            "lease": self._op_lease,
            "detach": self._op_detach,
        }

    # --- lifecycle -------------------------------------------------------------------

    def run(self, listener: socket.socket) -> None:
        housekeeping = threading.Thread(target=self._housekeep, name="housekeeping", daemon=True)
        housekeeping.start()
        listener.settimeout(ACCEPT_TICK_S)
        while not self.stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stop.is_set():
                    break
                raise
            conn.settimeout(None)
            with self._state_lock:
                self._conns.add(conn)
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        self._close_conns()

    def finish(self) -> None:
        """Release the lease and record the detach; the session is still open."""
        self.lease.release_all()
        self._emit({"type": "detach", "reason": self.stop_reason})

    def _close_conns(self) -> None:
        with self._state_lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)

    def _housekeep(self) -> None:
        while not self.stop.wait(LEASE_FLUSH_S):
            try:
                self.lease.flush()
            except OSError as exc:
                print(f"benchctl daemon: lease flush failed: {exc}", file=sys.stderr)

    # --- connections -----------------------------------------------------------------

    def _serve_conn(self, sock: socket.socket) -> None:
        conn = _Conn(Channel(sock))
        try:
            while not self.stop.is_set():
                try:
                    frame = conn.channel.recv(None)
                except (EOFError, OSError):
                    return
                except ValueError as exc:
                    conn.channel.send(_error_frame(None, _protocol(f"bad frame: {exc}")))
                    return
                if frame is None:
                    continue
                req_id, op = frame.get("id"), frame.get("op")
                if not isinstance(op, str) or not isinstance(req_id, str):
                    conn.channel.send(_error_frame(req_id, _protocol("request needs id and op")))
                    continue
                if conn.owner is None and op != "hello":
                    conn.channel.send(
                        _error_frame(req_id, _protocol("first request must be hello"))
                    )
                    continue
                if op == "tail":
                    self._tail(conn, req_id, frame.get("args"))
                    return
                reply = self._handle(conn, req_id, op, frame.get("args", {}))
                try:
                    conn.channel.send(reply)
                finally:
                    # Stopping only after the reply is sent, so the client hears its detach.
                    if op == "detach" and reply.get("ok") is True:
                        self.stop.set()
        except OSError:
            return
        finally:
            with self._state_lock:
                self._conns.discard(sock)
            conn.channel.close()

    def _handle(self, conn: _Conn, req_id: str, op: str, raw_args: Any) -> dict[str, Any]:
        handler = self._ops.get(op)
        if handler is None:
            return _error_frame(req_id, _protocol(f"unknown op {op!r}"))
        try:
            args = _Args(op, raw_args)
            owner = args.req_text("owner") if op == "hello" else conn.owner
            if owner is None:
                raise _protocol("first request must be hello")
            self.lease.admit(owner, mutating=op in MUTATING_OPS)
            verify = op in MUTATING_OPS and op not in UNVERIFIED_OK
            # The probe waits for shell-ready, so the client needs ticks even on a quick op.
            probe = verify and self.session.status().identity is None
            with self._progress(conn.channel, req_id, op, owner, always=probe) as note:
                if verify:
                    self._require_verified(note)
                result = handler(args, conn, _Request(req_id, note))
        except BenchctlError as exc:
            return _error_frame(req_id, exc)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return _error_frame(req_id, InternalError(f"{op}: {type(exc).__name__}: {exc}"))
        return {"id": req_id, "ok": True, "result": result}

    @contextlib.contextmanager
    def _progress(
        self, channel: Channel, req_id: str, op: str, owner: str, *, always: bool = False
    ) -> Iterator[Callable[[dict[str, Any]], None]]:
        """Send `{elapsed, offset, note}` every `progress_interval` until the op returns, so a
        client tells a slow wait from a dead daemon, and keep `owner`'s lease fresh: a long
        wait is not idleness (Section 3.4). The yielded callback updates the note."""
        latest: dict[str, Any] = {"note": op}

        def update(progress: dict[str, Any]) -> None:
            latest.update(progress)

        if op not in PROGRESS_OPS and not always:
            yield update
            return
        done = threading.Event()
        start = time.monotonic()

        def tick() -> None:
            while not done.wait(self.progress_interval):
                self.lease.touch(owner)
                offset: Any
                try:
                    offset = self.session.status().offset
                except Exception:  # a progress tick must never break the op it reports on
                    offset = latest.get("offset")
                frame = {
                    "elapsed": round(time.monotonic() - start, 3),
                    "offset": offset,
                    "note": str(latest.get("note", op)),
                }
                try:
                    channel.send({"id": req_id, "progress": frame})
                except OSError:
                    return

        ticker = threading.Thread(target=tick, name=f"progress-{op}", daemon=True)
        ticker.start()
        try:
            yield update
        finally:
            done.set()
            ticker.join()

    # --- helpers ---------------------------------------------------------------------

    def _cursor(self, args: _Args, key: str, default: str | None = None) -> Offset | None:
        value = args.data.get(key, default)
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0:
                raise UsageError(f"{args.op}: {key}: offsets are not negative")
            return value
        if not isinstance(value, str):
            raise UsageError(f"{args.op}: {key}: expected an offset or a cursor")
        Cursor.parse(value)
        return self.session.resolve(value)

    def _identity_view(self, identity: Identity | None) -> Identity | None:
        if identity is not None and not identity.verified and self._override:
            return dataclasses.replace(identity, verified=True)
        return identity

    def _require_verified(self, note: Callable[[dict[str, Any]], None]) -> None:
        identity = self.session.status().identity
        if identity is None:
            # The session probes only when asked; an auto-attaching `cmd` must not drive a
            # unit nobody has identified.
            note({"note": "identity"})
            identity = self._probe(False)
        if not identity.verified and not self._override:
            raise DeviceError(_mismatch_message(identity), data={"identity": _jsonable(identity)})

    def _probe(self, reprobe: bool) -> Identity:
        """`session.identity`, with a shell that never came up as `device`: there is no
        identity to override, and the next mutating op probes again."""
        try:
            return self.session.identity(reprobe)
        except TimeoutError_ as exc:
            gate = self.session.status().gate
            raise DeviceError(
                f"identity: shell not ready (state={gate.state})",
                data={"gate": _jsonable(gate), "detail": exc.message},
            ) from exc

    def _emit(self, event: Event) -> None:
        try:
            self.session.emit(event)
        except Exception:
            traceback.print_exc(file=sys.stderr)

    # --- ops -------------------------------------------------------------------------

    def _op_hello(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        owner = args.req_text("owner")
        own = self.session.profile
        profile = {"name": own.name, "path": str(own.path)}
        # Redaction and the deny list are this profile's; a client rendering with another
        # would believe checks the daemon does not run. Refused before greeting.
        wanted = args.text("profile")
        if wanted is not None and Path(wanted).resolve() != own.path.resolve():
            raise UsageError(
                f"daemon for {self.unit.name} runs profile {own.name} ({own.path}); "
                f"this command resolved {wanted}; pass --profile {own.name} or detach first",
                data={"daemon_profile": profile, "client_profile": wanted},
            )
        conn.owner = owner
        return {
            "daemon": f"benchctl {__version__}",
            "proto": PROTO,
            "unit": self.unit.name,
            "offset": self.session.status().offset,
            "lease": self.lease.current(),
            "owner_ok": self.lease.owner_ok(owner),
            "profile": profile,
        }

    def _op_status(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        status = self.session.status()
        status = dataclasses.replace(
            status,
            lease=self.lease.current() or {},
            identity=self._identity_view(status.identity),
        )
        return dataclasses.asdict(status)

    def _op_mark(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        name = args.text("name")
        if name is not None:
            # A name that parses as a keyword or offset could never be resolved as a mark.
            if not name.strip():
                raise UsageError("mark: name must not be empty")
            if Cursor.parse(name).kind != "mark":
                raise UsageError(f"mark: {name!r} is a cursor keyword or offset, not a name")
        return {"offset": self.session.mark(name), "name": name}

    def _op_resolve(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        args.req_text("cursor")
        return {"offset": self._cursor(args, "cursor")}

    def _op_cmd(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        line = args.req_text("line")
        self.session.profile.check_allowed(line)
        result = self.session.cmd(
            line,
            expect=args.regex("expect"),
            error=args.regex_list("error") or [],
            capture=args.opt_flag("capture"),
            timeout=args.seconds("timeout", self.session.profile.console.timeout),
            reboots=args.flag("reboots"),
            secret=args.flag("secret"),
            force=args.flag("force"),
            source=args.text("source", "cmd") or "cmd",
        )
        reply = {
            key: value for key, value in dataclasses.asdict(result).items() if key != "tx_offset"
        }
        self._emit({"type": "reply", "request_id": req.id, **reply, "offset": result.tx_offset})
        return dataclasses.asdict(result)

    def _op_wait(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        pattern, absent = args.regex("pattern"), args.regex("absent")
        order = args.regex_list("order")
        if pattern is None and absent is None and not order:
            raise UsageError("wait: needs a pattern, absent or order")
        if pattern is not None and order:
            raise UsageError("wait: pattern and order are exclusive")
        since = self._cursor(args, "since")
        if since is None:
            raise UsageError("wait: since: required")
        result = self.session.wait(
            pattern,
            since,
            args.seconds("timeout", WAIT_TIMEOUT_S),
            absent=absent,
            window=args.number("window", None),
            order=order,
            progress=req.note,
        )
        self._emit(
            {
                "type": "wait",
                "request_id": req.id,
                "pattern": pattern,
                "since": since,
                "result": result.result,
                "line": result.line,
                "groups": result.groups,
                "duration": result.duration,
                "order": result.order,
                "offset": result.offset,
            }
        )
        return dataclasses.asdict(result)

    def _op_log(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        since = self._cursor(args, "since", "attach") or 0
        until = self._cursor(args, "until")
        limit = args.integer("limit", LOG_LIMIT)
        end = self.session.status().offset  # before the query, so `next` never skips a line
        lines = self.session.log(since, until, args.regex("grep"), limit)
        if lines and len(lines) >= limit:
            nxt = lines[-1].offset + 1
        else:
            nxt = max(since, end if until is None else min(end, until))
            if lines:
                nxt = max(nxt, lines[-1].offset + 1)
        return {"lines": [dataclasses.asdict(line) for line in lines], "next": nxt}

    def _op_reset(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        offset = self.session.status().offset
        info = self.session.reset(args.flag("bootloader"), args.seconds("wait_rst", RESET_WAIT_S))
        return {"offset": offset, "reset": _jsonable(info)}

    def _op_arm_reset(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        window = args.seconds("window", self.session.profile.boot.expected_reset_window)
        self.session.arm_reset(args.req_text("cause"), window)
        return {}

    def _op_flash(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        profile = self.session.profile
        if profile.flash.chip is None:
            raise UsageError(f"flash: profile {profile.name} sets no [profile] chip")
        files = _flash_files(args)
        after = args.text("after", profile.flash.after) or profile.flash.after
        if after not in FLASH_AFTER:
            raise UsageError(f"flash: after: one of {', '.join(FLASH_AFTER)}, got {after!r}")
        flash_args = args.data.get("flash_args") or {}
        if not isinstance(flash_args, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in flash_args.items()
        ):
            raise UsageError("flash: flash_args: expected an object of strings")
        with self.session.device_lock():
            result = flash_mod.flash_on_session(
                self.session,
                files,
                chip=profile.flash.chip,
                before=profile.flash.before,
                after=after,
                baud=args.integer("baud", profile.flash.baud),
                flash_args=flash_args,
            )
        return dataclasses.asdict(result)

    def _op_capture(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        seconds = args.number("seconds", None)
        if seconds is None or seconds <= 0:
            raise UsageError("capture: seconds: expected a positive number")
        lines, reset = capture_mod.capture_on_session(self.session, seconds)
        return {"lines": [dataclasses.asdict(line) for line in lines], "reset": _jsonable(reset)}

    def _op_identity(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        identity = self._probe(args.flag("reprobe"))
        if not identity.verified:
            if args.flag("override") and not self._override:
                self._override = True
                self._emit(
                    {
                        "type": "identity",
                        "fields": identity.fields,
                        "verified": True,
                        "mismatches": identity.mismatches,
                        "override": True,
                    }
                )
            if not self._override:
                raise DeviceError(
                    _mismatch_message(identity), data={"identity": _jsonable(identity)}
                )
        return dataclasses.asdict(self._identity_view(identity) or identity)

    def _op_gate(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        since = self._cursor(args, "since", "boot") or 0
        env_gate = self.session.profile.env_gate
        default = WAIT_TIMEOUT_S if env_gate is None else env_gate.timeout
        return self.session.gate_check(since, args.seconds("timeout", default))

    def _op_lease(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        action = args.req_text("action")
        owner = conn.owner or ""
        if action == "take":
            return {"lease": self.lease.take(owner)}
        if action == "release":
            self.lease.release(owner)
            return {"lease": None}
        raise UsageError(f"lease: action: take or release, got {action!r}")

    def _op_detach(self, args: _Args, conn: _Conn, req: _Request) -> dict[str, Any]:
        self.stop_reason = "detach"  # _serve_conn stops the daemon once the reply is out
        return {}

    # --- tail ------------------------------------------------------------------------

    def _tail(self, conn: _Conn, req_id: str, raw_args: Any) -> None:
        try:
            args = _Args("tail", raw_args)
            since = self._cursor(args, "since") or 0
            events = args.flag("events")
            feed = self.session.subscribe()
        except BenchctlError as exc:
            conn.channel.send(_error_frame(req_id, exc))
            return
        try:
            # Subscribed first, so a line stored during the backfill arrives on the feed.
            nxt = since
            while True:
                batch = self.session.log(nxt, None, None, LOG_LIMIT)
                for line in batch:
                    conn.channel.send({"id": req_id, "line": dataclasses.asdict(line)})
                    nxt = line.offset + 1
                if len(batch) < LOG_LIMIT:
                    break
            touched = time.monotonic()
            while not self.stop.is_set() and not _peer_closed(conn.channel.sock):
                if conn.owner is not None and time.monotonic() - touched >= TAIL_TICK_S:
                    self.lease.touch(conn.owner)  # watching is not idleness either
                    touched = time.monotonic()
                try:
                    item = feed.get(TAIL_TICK_S)
                except BenchctlError as exc:
                    conn.channel.send(_error_frame(req_id, exc))
                    return
                if isinstance(item, LogLine):
                    if item.offset >= nxt:
                        conn.channel.send({"id": req_id, "line": dataclasses.asdict(item)})
                        nxt = item.offset + 1
                elif item is not None and events:
                    conn.channel.send({"id": req_id, "event": item})
        except OSError:
            return
        finally:
            feed.close()


def _peer_closed(sock: socket.socket) -> bool:
    """True once the client hung up; anything else it sends during a tail is ignored."""
    readable, _, _ = select.select([sock], [], [], 0)
    if not readable:
        return False
    try:
        return not sock.recv(4096, socket.MSG_PEEK)
    except OSError:
        return True


def _flash_files(args: _Args) -> list[tuple[int, str, str]]:
    raw = args.data.get("files")
    if not isinstance(raw, list) or not raw:
        raise UsageError("flash: files: expected a non-empty list of {addr, path, name}")
    files: list[tuple[int, str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise UsageError("flash: files: expected objects")
        addr, path, name = item.get("addr"), item.get("path"), item.get("name")
        if isinstance(addr, bool) or not isinstance(addr, int) or addr < 0:
            raise UsageError(f"flash: files: bad addr {addr!r}")
        if not isinstance(path, str) or not isinstance(name, str):
            raise UsageError("flash: files: path and name must be strings")
        files.append((addr, path, name))
    return files


# --- process lifecycle -------------------------------------------------------------------


def open_session(unit: Unit, profile: Profile) -> Session:
    """The production session (U4). Imported here, not at module level, so the daemon and
    its tests build and run without the session module."""
    module: Any = importlib.import_module("benchctl.session")
    session: Session = module.open_session(unit=unit, profile=profile, owner=default_owner())
    return session


def _fail(error: BenchctlError) -> int:
    # The spawning client reads this last stderr line back out of daemon.log.
    print(json.dumps({"ok": False, "error": error.to_dict()}), file=sys.stderr, flush=True)
    return error.exit_code


def _bind(path: Path) -> socket.socket:
    path.unlink(missing_ok=True)  # we hold lease.lock: any socket file here is stale
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(LISTEN_BACKLOG)
    except OSError:
        listener.close()
        raise
    return listener


def serve(
    unit_name: str,
    profile_arg: str | None,
    *,
    session_factory: SessionFactory | None = None,
    stop: threading.Event | None = None,
    progress_interval: float = PROGRESS_S,
) -> int:
    """Run the daemon for `unit_name` until detach or `stop`; returns the exit status."""
    try:
        paths.check_unit_name(unit_name)
        sock_path = paths.socket_path(unit_name)
        check_socket_path(sock_path)
        paths.unit_dir(unit_name).mkdir(parents=True, exist_ok=True)
    except BenchctlError as exc:
        return _fail(exc)
    lock = PortLock(paths.lease_lock_path(unit_name))
    if not lock.acquire():
        return _fail(BusyError(f"unit {unit_name}: another daemon holds {lock.path}"))
    pid_file = paths.pid_path(unit_name)
    session: Session | None = None
    try:
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        config = paths.config_dir()
        unit = load_registry(config).unit(unit_name)
        profile = load_profile(profile_arg or unit.profile, Path.cwd(), config)
        session = (session_factory or open_session)(unit, profile)
        daemon = Daemon(
            session,
            unit,
            LeaseManager(paths.lease_path(unit_name), session.emit),
            stop=stop,
            progress_interval=progress_interval,
        )
        listener = _bind(sock_path)
        try:
            daemon.run(listener)
        finally:
            listener.close()
            sock_path.unlink(missing_ok=True)
        daemon.finish()
        return 0
    except BenchctlError as exc:
        return _fail(exc)
    except OSError as exc:
        return _fail(DeviceError(f"daemon for {unit_name}: {exc}"))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return _fail(InternalError(f"daemon for {unit_name}: {type(exc).__name__}: {exc}"))
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()
        pid_file.unlink(missing_ok=True)
        lock.release()


def main(
    unit_name: str,
    profile_arg: str | None,
    *,
    session_factory: SessionFactory | None = None,
    progress_interval: float = PROGRESS_S,
) -> int:
    """The `daemon` verb: `serve` in the foreground, stopped by SIGTERM or SIGINT."""
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return serve(
            unit_name,
            profile_arg,
            session_factory=session_factory,
            stop=stop,
            progress_interval=progress_interval,
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
