"""One attached unit inside the daemon (design Sections 4.2, 4.5, 4.6, 4.9, 4.12).

`DeviceSession` implements `types.Session`; constructing it (`open_session`, for the daemon)
is the attach: it opens the port, writes meta line 0, resets the unit once and starts two
threads. The reader is the only `transport.read()` caller (LineEngine → Redactor → LogStore
→ IdfDetector and Gate) and runs the reconnect loop; housekeeping ticks the gate, sends
probes and fsyncs.

Locks: the device lock (an RLock) serialises every TX with its reply; the LogStore's
Condition guards all other state. The device lock is always taken first, and the reader
never takes it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from benchctl import paths, registry
from benchctl.errors import (
    BenchctlError,
    DeviceError,
    InternalError,
    TimeoutError_,
    TransportLost,
    UsageError,
)
from benchctl.gate import Gate
from benchctl.idf import Detection, IdfDetector, ResetMatch
from benchctl.linebuf import LineEngine
from benchctl.logstore import LogStore
from benchctl.profile import IdentityField, Profile
from benchctl.redact import Redactor
from benchctl.registry import Unit
from benchctl.transport import READ_TIMEOUT, PySerialTransport
from benchctl.types import (
    CmdResult,
    Cursor,
    Event,
    GateState,
    Identity,
    LogLine,
    ModemLines,
    Offset,
    ResetInfo,
    Status,
    Subscription,
    Transport,
    WaitResult,
)

log = logging.getLogger(__name__)

READ_CHUNK = 4096
CONTEXT_LINES = 20  # stored lines kept with an unexpected reset
RECONNECT_INTERVAL = 0.5  # esp-idf-monitor's cadence
RECONNECT_SLOW_INTERVAL = 5.0
RECONNECT_TIMEOUT = 120.0  # after this long unplugged, retry at the slow interval
PAUSE_ACK_TIMEOUT = 2.0
FSYNC_INTERVAL = 1.0
PROGRESS_INTERVAL = 5.0
JOIN_TIMEOUT = 2.0
MIN_WAKE = 0.005
READER_ERROR_BACKOFF = 0.1
REDACTED = "<redacted>"
FW_FIELD = "fw_version"
DEFAULT_ID_FIELDS = ("mac", "cert_cn")
ATTACH_CAUSE = "attach"
_STAMPED = frozenset({"seq", "ts", "t", "unit", "fw", "dev"})
_MS_DIGITS = 3

CmdMode = Literal["none", "expect", "capture"]


@dataclasses.dataclass(frozen=True, slots=True)
class _Arming:
    cause: str
    until: float  # seconds since attach


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _compile(pattern: str, what: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise UsageError(f"invalid {what} regex {pattern!r}: {exc}") from exc


def _groups(match: re.Match[str]) -> list[str]:
    return [group or "" for group in match.groups()]


def _jsonable(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)


def _new_session_dir(unit: str) -> Path:
    base = paths.sessions_dir(unit)
    base.mkdir(parents=True, exist_ok=True)
    stamp = paths.timestamp()
    for n in itertools.count(1):
        # Two attaches within one second (detach, re-attach) must not share a directory.
        candidate = base / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise AssertionError("unreachable")


class DeviceSession:
    """Implements `types.Session` over a `PySerialTransport`.

    `unit` supplies the port match (re-resolved on every reconnect), the identity
    expectations and `id_field`. `port` and `session_dir` default to resolving the match and
    a fresh `sessions/<attach-ts>/`; `lines` replaces esptool's RTS/DTR reset (tests).
    """

    def __init__(
        self,
        unit: Unit,
        profile: Profile,
        *,
        port: str | None = None,
        session_dir: Path | None = None,
        owner: str | None = None,
        lines: ModemLines | None = None,
    ) -> None:
        self.profile = profile
        self.unit = unit.name
        self._match = dict(unit.match)
        self._expect = dict(unit.expect)
        self._id_fields = (unit.id_field,) if unit.id_field else DEFAULT_ID_FIELDS
        self._lines = lines
        self._baud = profile.console.baud
        self._prompt = profile.console.prompt
        self._redactor = Redactor(rule.pattern for rule in profile.redact)
        self._secrets: list[str] = []  # whole `cmd --secret` lines, masked for the session
        self._path = port or registry.match_port(self._match, registry.list_ports())
        transport = PySerialTransport.open(self._path, self._baud, lines)
        try:
            if session_dir is None:
                session_dir = _new_session_dir(self.unit)
            session_dir.mkdir(parents=True, exist_ok=True)
            self.session_dir = session_dir
            self._store = LogStore(self.session_dir / "serial.log")
            self._events = (self.session_dir / "events.jsonl").open("a", encoding="utf-8")
        except BaseException:
            transport.close()
            raise
        self.attached_at = _iso_now()
        self._cond = self._store.cond
        self._events_closed = False
        self._seq = 0
        self._subs: list[Subscription] = []
        self._device = threading.RLock()
        self._identity: Identity | None = None
        self._identity_lock = threading.Lock()
        self._last_reset: ResetInfo | None = None
        self._arming: _Arming | None = None
        boot = profile.boot
        self._gate = Gate(
            ready=boot.ready,
            prompt=self._prompt,
            settle=boot.settle,
            ready_timeout=boot.ready_timeout,
            probe_interval=boot.probe_interval,
            now=0.0,
        )
        self._engine = LineEngine()
        self._detector = self._new_detector()
        self._transport: Transport | None = transport
        self._lost_at = 0.0
        self._lost_count = 0
        self._next_attempt = 0.0
        self._closing = threading.Event()
        self._kick = threading.Event()  # wakes housekeeping on a gate change
        self._pause_req = threading.Event()
        self._parked = threading.Event()
        self._resume = threading.Event()

        self._reader = threading.Thread(target=self._read_loop, name="reader", daemon=True)
        self._housekeeper = threading.Thread(
            target=self._housekeeping, name="housekeeping", daemon=True
        )
        try:
            with self._cond:
                line = self._note(
                    f"attach unit={self.unit} port={self._path} baud={self._baud} "
                    f"at={self.attached_at}"
                )
                self.emit(
                    {
                        "type": "attach",
                        "offset": line.offset,
                        "port": self._path,
                        "baud": self._baud,
                        "pid": os.getpid(),
                        "owner": owner,
                        "session_dir": str(self.session_dir),
                    }
                )
                self._emit_gate([self._gate.state])
                self._arm(ATTACH_CAUSE)
            # Drop whatever a reset-on-open already printed, so the log shows one boot.
            transport.apply_settings(transport.get_settings())
            self._reader.start()
            self._housekeeper.start()
            transport.reset(False)
        except BaseException:
            self.close()
            raise

    # --- events and meta lines -------------------------------------------------------------

    def emit(self, event: Event) -> None:
        """Stamp the Section 7.5 common fields, append to events.jsonl, offer to subscribers."""
        with self._cond:
            self._seq += 1
            identity = self._identity
            fields = {} if identity is None else identity.fields
            record: Event = {
                "seq": self._seq,
                "ts": _iso_now(),
                "t": round(self._store.now(), _MS_DIGITS),
                "unit": self.unit,
                "fw": fields.get(FW_FIELD),
                "dev": next((fields[f] for f in self._id_fields if f in fields), None),
                "type": event.get("type"),
                "offset": None,
            }
            record.update((key, value) for key, value in event.items() if key not in _STAMPED)
            if not self._events_closed:
                self._events.write(json.dumps(record, default=_jsonable) + "\n")
                self._events.flush()
            self._offer(record)

    def _offer(self, item: LogLine | Event) -> None:
        # A feed whose offer fails is closed or fell behind; it is never offered again.
        self._subs = [sub for sub in self._subs if sub.offer(item)]

    def _note(self, text: str) -> LogLine:
        with self._cond:
            line = self._store.append(self._mask(text), meta=True)
            self._offer(line)
            return line

    def note(self, text: str) -> Offset:
        return self._note(text).offset

    def _mask(self, text: str) -> str:
        """Incoming text as it may be stored: secret lines first, then the profile patterns."""
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return self._redactor.apply(text)

    def _emit_gate(self, changes: list[GateState]) -> None:
        for state in changes:
            self.emit({"type": "gate", "state": state.state, "method": state.method})
        if changes:
            self._kick.set()
            self._cond.notify_all()

    def _gate_now(self) -> GateState:
        """The gate after any transition due by now (caller holds the Condition)."""
        self._emit_gate(self._gate.tick(self._store.now()))
        return self._gate.state

    def _arm(self, cause: str, window: float | None = None) -> None:
        span = self.profile.boot.expected_reset_window if window is None else window
        with self._cond:
            self._arming = _Arming(cause, self._store.now() + span)

    # --- the reader thread -----------------------------------------------------------------

    def _new_detector(self) -> IdfDetector:
        boot = self.profile.boot
        return IdfDetector(boot.reset_patterns, boot.sha_pattern, boot.ms_regression)

    def _read_loop(self) -> None:
        while not self._closing.is_set():
            try:
                self._read_once()
            except Exception:
                # A bug here must not silently stop all logging; say so and keep reading.
                log.exception("reader error")
                self._closing.wait(READER_ERROR_BACKOFF)

    def _read_once(self) -> None:
        if self._pause_req.is_set():
            self._parked.set()
            self._resume.wait()
            return
        transport = self._transport
        if transport is None:
            self._reconnect_once()
            return
        try:
            data = transport.read(READ_CHUNK, READ_TIMEOUT)
        except TransportLost as exc:
            if not self._closing.is_set():
                self._on_lost(transport, exc)
            return
        if data:
            self._on_data(data)

    def _on_data(self, data: bytes) -> None:
        texts = self._engine.feed(data)
        tail = self._mask(self._engine.tail)
        with self._cond:
            for text in texts:
                self._store_device_line(self._mask(text))
            if self._store.set_tail(tail):
                self._emit_gate(self._gate.on_tail(tail, self._store.now()))

    def _store_device_line(self, text: str) -> None:
        line = self._store.append(text)
        self._offer(line)
        self._emit_gate(self._gate.on_line(text, line.t))
        # Device lines only: a `## tx rst:…` meta line must never look like a reset.
        detection = self._detector.feed(text)
        if detection is not None:
            self._on_detection(line, detection)

    def _on_detection(self, line: LogLine, detection: Detection) -> None:
        if detection.reset is not None:
            info = self._classify(line, detection.reset, detection.elf_sha)
            self._last_reset = info
            self._store.boot = line.offset
            fields = dataclasses.asdict(info)
            del fields["offset"], fields["t"]
            # ResetInfo has no room for it, so only the event says the reset was inferred
            # from a device-ms regression rather than seen as an `rst:` line.
            self.emit(
                {
                    "type": "reset",
                    "offset": line.offset,
                    **fields,
                    "inferred": detection.reset.inferred,
                }
            )
            download = (info.boot_mode or "").startswith("DOWNLOAD")
            self._emit_gate(self._gate.on_reset(line.t, download=download))
            return
        sha = detection.elf_sha
        last = self._last_reset
        if last is not None and last.elf_sha is not None:
            return
        if last is not None:
            self._last_reset = dataclasses.replace(last, elf_sha=sha)
        self.emit(
            {
                "type": "elf_sha",
                "offset": line.offset,
                "sha": sha,
                "boot_offset": None if last is None else last.offset,
            }
        )

    def _classify(self, line: LogLine, match: ResetMatch, sha: str | None) -> ResetInfo:
        arming, self._arming = self._arming, None
        # One arming covers one reset: a second reboot inside the window is a real surprise.
        expected = arming is not None and line.t <= arming.until
        context = (
            []
            if expected
            else [prior.text for prior in self._store.before(line.offset, CONTEXT_LINES)]
        )
        return ResetInfo(
            offset=line.offset,
            t=line.t,
            code=match.code,
            reason=match.reason,
            boot_mode=match.boot_mode,
            elf_sha=sha,
            expected=expected,
            cause=arming.cause if expected and arming is not None else None,
            context=context,
        )

    def _on_lost(self, transport: Transport, exc: TransportLost) -> None:
        transport.close()
        with self._cond:
            self._transport = None
            self._lost_count += 1
            self._lost_at = self._store.now()
            self._store.set_tail("")
            line = self._note(f"port lost error={exc}")
            self.emit({"type": "port_lost", "offset": line.offset, "error": str(exc)})
            self._emit_gate(self._gate.on_port_change(self._store.now()))
        self._next_attempt = time.monotonic()

    def _reconnect_once(self) -> None:
        now = time.monotonic()
        if now < self._next_attempt:
            # Short naps keep a pause request or close() from waiting on the full interval.
            self._closing.wait(min(self._next_attempt - now, READ_TIMEOUT))
            return
        gap = self._store.now() - self._lost_at
        interval = RECONNECT_INTERVAL if gap < RECONNECT_TIMEOUT else RECONNECT_SLOW_INTERVAL
        self._next_attempt = now + interval
        try:
            # The path may change (native USB re-enumerates); no candidate while unplugged is
            # simply "not yet".
            path = registry.match_port(self._match, registry.list_ports())
            transport = PySerialTransport.open(path, self._baud, self._lines)
        except (BenchctlError, OSError) as exc:
            log.debug("reconnect: %s", exc)
            return
        with self._cond:
            self._engine = LineEngine()
            self._detector = self._new_detector()
            self._transport = transport
            self._path = path
            gap = self._store.now() - self._lost_at
            line = self._note(f"port back path={path} gap={gap:.3f}")
            self.emit(
                {"type": "port_back", "offset": line.offset, "path": path, "gap": round(gap, 3)}
            )
            self._emit_gate(self._gate.on_port_change(self._store.now()))
        # Lost then back is unknown → unknown, no transition; the probe is due all the same.
        self._kick.set()

    # --- housekeeping ----------------------------------------------------------------------

    def _housekeeping(self) -> None:
        last_sync = time.monotonic()
        while not self._closing.is_set():
            with self._cond:
                self._gate_now()
                now = self._store.now()
                probe_at = self._gate.next_probe() if self._transport is not None else None
                tick_at = self._gate.next_tick(now)
            wait = FSYNC_INTERVAL
            if probe_at is not None and probe_at <= now:
                # Sent or not (a command holds the device), the next chance is an interval away.
                self._probe()
                wait = min(wait, self.profile.boot.probe_interval)
            elif probe_at is not None:
                wait = min(wait, probe_at - now)
            if tick_at is not None:
                wait = min(wait, tick_at - now)
            if time.monotonic() - last_sync >= FSYNC_INTERVAL:
                self._sync()
                last_sync = time.monotonic()
            self._kick.wait(max(wait, MIN_WAKE))
            self._kick.clear()

    def _sync(self) -> None:
        with self._cond:
            self._store.sync()
            if not self._events_closed:
                self._events.flush()
                os.fsync(self._events.fileno())

    def _probe(self) -> None:
        """Send `boot.probe` if still due. Never waits for the device lock: a busy device is
        probed on a later tick."""
        if not self._device.acquire(blocking=False):
            return
        try:
            with self._cond:
                transport = self._transport
                probe_at = self._gate.next_probe()
                now = self._store.now()
                if transport is None or probe_at is None or probe_at > now:
                    return
                self._gate.probe_sent(now)
                line = self._note("probe")
                self.emit(
                    {
                        "type": "tx",
                        "offset": line.offset,
                        "line": self.profile.boot.probe,
                        "source": "probe",
                        "request_id": None,
                    }
                )
            # A loss here is the reader's to report.
            with contextlib.suppress(TransportLost):
                self._send(transport, self.profile.boot.probe)
        finally:
            self._device.release()

    # --- simple accessors ------------------------------------------------------------------

    def status(self) -> Status:
        with self._cond:
            return Status(
                unit=self.unit,
                pid=os.getpid(),
                attached_at=self.attached_at,
                port=self._path,
                connected=self._transport is not None,
                baud=self._baud,
                lease={},  # the daemon owns the lease and fills this in
                identity=self._identity,
                last_reset=self._last_reset,
                offset=self._store.next_offset,
                gate=self._gate_now(),
                session_dir=str(self.session_dir),
            )

    def mark(self, name: str | None) -> Offset:
        """With no name, the offset the next line will get; with one, the offset of its
        `## mark` line."""
        if name is None:
            return self._store.next_offset
        if Cursor.parse(name).kind != "mark":
            raise UsageError(f"mark name {name!r} would read as a cursor; pick another")
        with self._cond:
            line = self._note(f"mark {name}")
            self._store.set_mark(name, line.offset)
            self.emit({"type": "mark", "offset": line.offset, "name": name})
            return line.offset

    def resolve(self, cursor: str) -> Offset:
        return self._store.resolve(cursor)

    def log(
        self, since: Offset, until: Offset | None, grep: str | None, limit: int
    ) -> list[LogLine]:
        if limit < 0:
            raise UsageError(f"limit must not be negative, got {limit}")
        regex = None if grep is None else _compile(grep, "grep")
        lines = self._store.lines(since, until)
        if regex is not None:
            lines = [line for line in lines if regex.search(line.text)]
        return lines[:limit]

    def subscribe(self) -> Subscription:
        sub = Subscription()
        with self._cond:
            self._subs.append(sub)
        return sub

    @contextlib.contextmanager
    def device_lock(self) -> Iterator[None]:
        with self._device:
            yield

    def set_gate(self, state: Literal["booting", "bootloader"], method: str | None = None) -> None:
        with self._cond:
            self._emit_gate(self._gate.set(state, method, self._store.now()))

    def arm_reset(self, cause: str, window: float) -> None:
        self._arm(cause, window)

    # --- TX --------------------------------------------------------------------------------

    def _send(self, transport: Transport, text: str) -> None:
        console = self.profile.console
        data = (text + console.line_ending).encode()
        for start in range(0, len(data), console.tx_chunk):
            if start:
                time.sleep(console.tx_chunk_delay_ms / 1000)
            transport.write(data[start : start + console.tx_chunk])

    def _wait_ready(self, deadline: float) -> str | None:
        """Block until the gate is ready; None then, else the state it was stuck in. Raises
        DeviceError at once in bootloader, where no amount of waiting helps."""
        with self._cond:
            while True:
                state = self._gate_now().state
                if state == "bootloader":
                    raise DeviceError("unit is in bootloader; run `reset`")
                if state == "ready":
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return state
                tick_at = self._gate.next_tick(self._store.now())
                if tick_at is not None:
                    remaining = min(remaining, max(tick_at - self._store.now(), MIN_WAKE))
                self._cond.wait(remaining)

    def _acquire_ready(self, deadline: float, force: bool) -> str | None:
        """Take the device lock with the gate ready (or `force`); on failure return why.

        The ready wait happens without the lock, because reaching ready may need a probe and
        a probe needs the lock.
        """
        while True:
            if not force:
                state = self._wait_ready(deadline)
                if state is not None:
                    return f"shell not ready (state={state})"
            if not self._device.acquire(timeout=max(deadline - time.monotonic(), 0.0)):
                return "device busy"
            if force:
                return None
            with self._cond:
                if self._gate_now().state == "ready":
                    return None
            self._device.release()

    def _cmd_mode(self, expect: re.Pattern[str] | None, capture: bool | None) -> CmdMode:
        if capture and expect is not None:
            raise UsageError("capture and expect are exclusive; use one")
        if capture and self._prompt is None:
            raise UsageError("capture needs console.prompt to know where the reply ends")
        if expect is not None:
            return "expect"
        if capture or (capture is None and self._prompt is not None):
            return "capture"
        return "none"

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
        """Send one line and read its reply. Raises UsageError (deny list, bad regex) before
        anything is sent, DeviceError in bootloader, TransportLost if the port dies first."""
        start = time.monotonic()
        deadline = start + timeout
        self.profile.check_allowed(line)
        if "\r" in line or "\n" in line:
            raise UsageError("a command is one line; it may not contain CR or LF")
        expect_re = None if expect is None else _compile(expect, "expect")
        error_res = [_compile(pattern, "error") for pattern in error]
        mode = self._cmd_mode(expect_re, capture)
        refused = self._acquire_ready(deadline, force)
        if refused is not None:
            return CmdResult(
                self._store.next_offset, "timeout", refused, [], None, time.monotonic() - start
            )
        try:
            shown = REDACTED if secret else self._redactor.apply(line)
            with self._cond:
                transport = self._transport
                if transport is None:
                    raise DeviceError("port not connected")
                # Registered under the Condition, before the bytes go out, so the reader cannot
                # store the shell's echo of the line unmasked.
                masked = line.rstrip()
                if secret and masked and masked not in self._secrets:
                    self._secrets.append(masked)
                if reboots:
                    self._arm(f"cmd:{shown}")
                tx = self._note(f"tx {shown}")
                lost_count = self._lost_count
                self.emit(
                    {
                        "type": "tx",
                        "offset": tx.offset,
                        "line": shown,
                        "source": source,
                        "request_id": None,
                    }
                )
            self._send(transport, line)
            status, matched, groups, captured = self._reply(
                mode, line, shown, tx.offset, expect_re, error_res, deadline, lost_count
            )
        finally:
            self._device.release()
        return CmdResult(tx.offset, status, matched, groups, captured, time.monotonic() - start)

    def _reply(
        self,
        mode: CmdMode,
        sent: str,
        shown: str,
        tx_offset: Offset,
        expect: re.Pattern[str] | None,
        errors: list[re.Pattern[str]],
        deadline: float,
        lost_count: int,
    ) -> tuple[Literal["ok", "error", "timeout"], str | None, list[str], str | None]:
        """(status, line, groups, captured) for the reply to the line sent at `tx_offset`.
        `shown` is the line as it may appear in errors (redacted, or `<redacted>`)."""
        if mode == "none":
            return "ok", None, [], None
        # A secret line's echo is stored masked, ending in `<redacted>`.
        echoes = {sent.rstrip(), self._redactor.apply(sent).rstrip(), shown.rstrip()}
        prompt = self._prompt or ""
        pos = tx_offset + 1
        seen_line = False
        echoed = False
        captured: list[str] = []
        error_line: str | None = None
        with self._cond:
            while True:
                for stored in self._store.lines(pos):
                    pos = stored.offset + 1
                    if stored.meta:
                        continue
                    seen_line = True
                    # The shell echoes the line, completing the pending prompt into one stored
                    # line (`demo> version`); an async log line can land before it.
                    if not echoed and any(stored.text.rstrip().endswith(e) for e in echoes):
                        echoed = True
                        continue
                    # A log line right after the prompt is stored as `demo> I (…) …` and
                    # leaves no prompt in the tail; that prompt still ends the reply.
                    if mode == "capture" and echoed and prompt and stored.text.startswith(prompt):
                        return (
                            ("error" if error_line else "ok"),
                            error_line,
                            [],
                            "\n".join(captured),
                        )
                    if any(regex.search(stored.text) for regex in errors):
                        if mode == "expect":
                            return "error", stored.text, [], None
                        error_line = error_line or stored.text
                    if mode == "expect" and expect is not None:
                        match = expect.search(stored.text)
                        if match is not None:
                            return "ok", stored.text, _groups(match), None
                    if mode == "capture":
                        captured.append(stored.text)
                # The tail still held the previous prompt when the line went out; only a
                # prompt printed after a line stored since then ends the capture.
                if mode == "capture" and seen_line and self._store.tail.endswith(prompt):
                    text = "\n".join(captured)
                    return ("error" if error_line else "ok"), error_line, [], text
                if self._lost_count != lost_count:
                    raise TransportLost(f"port lost waiting for the reply to {shown!r}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (
                        "timeout",
                        error_line,
                        [],
                        "\n".join(captured) if mode == "capture" else None,
                    )
                self._cond.wait(remaining)

    # --- reset -----------------------------------------------------------------------------

    def reset(
        self, bootloader: bool, wait_rst: float = 5.0, *, cause: str = "reset"
    ) -> ResetInfo | None:
        """Reset through the modem lines; the reset line seen within `wait_rst`, else None."""
        with self._device:
            with self._cond:
                transport = self._transport
                if transport is None:
                    raise DeviceError("port not connected")
                self._arm(cause)
                request = self._note(f"reset requested bootloader={str(bootloader).lower()}")
                # Before the pulse, so a fast boot's prompt cannot be undone by this move.
                state: Literal["booting", "bootloader"] = "bootloader" if bootloader else "booting"
                self._emit_gate(self._gate.set(state, None, self._store.now()))
            transport.reset(bootloader)
            deadline = time.monotonic() + wait_rst
            with self._cond:
                while True:
                    last = self._last_reset
                    if last is not None and last.offset > request.offset:
                        return last
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)

    # --- wait ------------------------------------------------------------------------------

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
        """Match device lines (never meta lines) from `since` (Section 7.1 `wait` rules).

        `absent` is watched until the pattern or order completes; `window` extends that watch
        to `window` seconds from the call, and is the whole watch when `absent` stands alone
        (default: `timeout`).
        """
        start = time.monotonic()
        if pattern is not None and order:
            raise UsageError("give a pattern or --order, not both")
        if pattern is None and not order and absent is None:
            raise UsageError("wait needs a pattern, --order or --absent")
        sequence = [
            _compile(p, "pattern") for p in (order or ([] if pattern is None else [pattern]))
        ]
        absent_re = None if absent is None else _compile(absent, "absent")
        # The window governs only the absence watch: without `absent` a match returns at once.
        watch: float | None
        if absent_re is None or (window is None and sequence):
            watch = None
        else:
            watch = timeout if window is None else window
        t_start = self._store.now()
        watch_until = None if watch is None else t_start + watch
        matched: list[LogLine] = []
        last_match: re.Match[str] | None = None
        early: dict[int, LogLine] = {}
        pos = since
        next_progress = start + PROGRESS_INTERVAL

        def result(
            kind: Literal[
                "match", "timeout", "absent_ok", "absent_violated", "order_ok", "order_violated"
            ],
            line: LogLine | None = None,
            groups: list[str] | None = None,
        ) -> WaitResult:
            return WaitResult(
                kind,
                None if line is None else line.text,
                groups or [],
                None if line is None else line.offset,
                None if line is None else line.t,
                time.monotonic() - start,
                [m.offset for m in matched] if order else [],
            )

        while True:
            with self._cond:
                for line in self._store.lines(pos):
                    pos = line.offset + 1
                    if line.meta:
                        continue
                    if len(matched) < len(sequence):
                        match = sequence[len(matched)].search(line.text)
                        if match is not None:
                            matched.append(line)
                            last_match = match
                            continue
                        for later in range(len(matched) + 1, len(sequence)):
                            if later not in early and sequence[later].search(line.text):
                                early[later] = line
                    if absent_re is not None:
                        hit = absent_re.search(line.text)
                        watching = len(matched) < len(sequence) or (
                            watch_until is not None and line.t <= watch_until
                        )
                        if hit is not None and watching:
                            return result("absent_violated", line, _groups(hit))
                now = time.monotonic()
                complete = len(matched) == len(sequence)
                if complete and (watch_until is None or self._store.now() >= watch_until):
                    if not sequence:
                        return result("absent_ok")
                    groups = [] if last_match is None else _groups(last_match)
                    return result("order_ok" if order else "match", matched[-1], groups)
                if not complete and now >= start + timeout:
                    pending = [early[i] for i in range(len(matched), len(sequence)) if i in early]
                    if order and pending:
                        return result("order_violated", pending[0])
                    return result("timeout")
                wakes = [] if progress is None else [next_progress]
                if not complete:
                    wakes.append(start + timeout)
                if watch is not None:
                    wakes.append(start + watch)
                # Never empty: an incomplete match has its timeout, a complete one its watch.
                self._cond.wait(max(min(wakes) - now, MIN_WAKE))
            if progress is not None and time.monotonic() >= next_progress:
                # Outside the lock: the callback writes to a client socket and may block.
                progress(
                    {
                        "elapsed": round(time.monotonic() - start, _MS_DIGITS),
                        "offset": self._store.next_offset,
                        "note": f"waiting since {since}",
                    }
                )
                next_progress += PROGRESS_INTERVAL

    # --- identity and the env gate ---------------------------------------------------------

    def identity(self, reprobe: bool = False) -> Identity:
        """Run the profile's identity fields once the gate is ready, compare them with the
        unit's `expect`, and emit `identity`. Cached until `reprobe`."""
        with self._identity_lock:
            if self._identity is not None and not reprobe:
                return self._identity
            boot = self.profile.boot
            budget = boot.ready_timeout + boot.settle + self.profile.console.timeout
            stuck = self._wait_ready(time.monotonic() + budget)
            if stuck is not None:
                raise TimeoutError_(f"identity: shell not ready (state={stuck})")
            fields: dict[str, str] = {}
            with self._device:
                for spec in self.profile.identity:
                    value = self._identity_value(spec)
                    if value is not None:
                        fields[spec.name] = value
            mismatches = {
                name: (want, fields.get(name, ""))
                for name, want in self._expect.items()
                if fields.get(name) != want
            }
            found = Identity(fields, not mismatches, mismatches)
            with self._cond:
                self._identity = found
                self.emit(
                    {
                        "type": "identity",
                        "fields": fields,
                        "verified": found.verified,
                        "mismatches": {name: list(pair) for name, pair in mismatches.items()},
                    }
                )
            return found

    def _identity_value(self, spec: IdentityField) -> str | None:
        regex = re.compile(spec.regex)
        if spec.source == "boot":
            for line in self._store.lines(self._store.boot):
                match = None if line.meta else regex.search(line.text)
                if match is not None:
                    return match.group(1)
            return None
        timeout = self.profile.console.timeout if spec.timeout is None else spec.timeout
        send = spec.send or ""
        # Capture to the prompt when there is one; without one the field's own regex is the
        # only reply marker available.
        capturing = self._prompt is not None
        reply = self.cmd(
            send,
            expect=None if capturing else spec.regex,
            error=[],
            capture=capturing,
            timeout=timeout,
            reboots=False,
            secret=False,
            force=False,
            source="identity",
        )
        text = reply.captured if capturing else reply.line
        if reply.status != "ok" or text is None:
            return None
        if spec.filter is not None:
            try:
                done = subprocess.run(
                    spec.filter,
                    shell=True,
                    input=text,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return None
            text = done.stdout
        match = regex.search(text)
        return None if match is None else match.group(1)

    def gate_check(self, since: Offset, timeout: float) -> dict[str, Any]:
        """The profile's env gate over device lines from `since`: every `require` seen and no
        `forbid`, within `timeout`. A profile without one passes."""
        env = self.profile.env_gate
        if env is None:
            return {"ok": True, "missing": [], "forbidden_hits": []}
        require = [_compile(p, "require") for p in env.require]
        forbid = [_compile(p, "forbid") for p in env.forbid]
        found: set[int] = set()
        hits: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        pos = since
        with self._cond:
            while True:
                for line in self._store.lines(pos):
                    pos = line.offset + 1
                    if line.meta:
                        continue
                    found.update(i for i, regex in enumerate(require) if regex.search(line.text))
                    hits.extend(
                        {"pattern": regex.pattern, "line": line.text, "offset": line.offset}
                        for regex in forbid
                        if regex.search(line.text)
                    )
                remaining = deadline - time.monotonic()
                if hits or len(found) == len(require) or remaining <= 0:
                    break
                self._cond.wait(remaining)
        missing = [regex.pattern for i, regex in enumerate(require) if i not in found]
        return {"ok": not missing and not hits, "missing": missing, "forbidden_hits": hits}

    # --- pause and close -------------------------------------------------------------------

    @contextlib.contextmanager
    def pause_reader(self) -> Iterator[Transport]:
        """Park the reader (under the device lock) and yield the transport; on exit the
        reader resumes with a fresh LineEngine and IdfDetector."""
        with self._device:
            transport = self._transport
            if transport is None:
                raise DeviceError("port not connected")
            self._resume.clear()
            self._parked.clear()
            self._pause_req.set()
            try:
                if not self._parked.wait(PAUSE_ACK_TIMEOUT):
                    raise InternalError("the reader did not park within 2 s")
                yield transport
            finally:
                # The reader is parked, so its per-reader state can be swapped safely.
                self._engine = LineEngine()
                self._detector = self._new_detector()
                with self._cond:
                    self._store.set_tail("")
                self._pause_req.clear()
                self._resume.set()

    def close(self) -> None:
        """Stop both threads, close the port and the session files. Idempotent."""
        if self._closing.is_set():
            return
        self._closing.set()
        self._resume.set()
        self._kick.set()
        for thread in (self._reader, self._housekeeper):
            if thread.is_alive():
                thread.join(JOIN_TIMEOUT)
        with self._cond:
            transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
        with self._cond:
            self._store.close()
            if not self._events_closed:
                self._events_closed = True
                self._events.close()


def open_session(
    *,
    unit: Unit,
    profile: Profile,
    port: str | None = None,
    session_dir: Path | None = None,
    owner: str | None = None,
    lines: ModemLines | None = None,
) -> DeviceSession:
    """Attach: the daemon's entry point. Raises DeviceError when the port cannot be opened."""
    return DeviceSession(
        unit, profile, port=port, session_dir=session_dir, owner=owner, lines=lines
    )
