"""The real daemon serving a FakeSession (design Section 6.2, U5).

Run as `python tests/fake_daemon.py --unit U`; the CLI tests point BENCHCTL_DAEMON_ARGV here.
U6's flash/capture are replaced with canned versions before serving, so U5's tests need only
the scaffold's stubs. Knobs come from the FAKE_DAEMON_ENV JSON, since the test process can only
steer a spawned daemon through its environment.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

from benchctl import capture, daemon, flash, paths
from benchctl.artifacts import read_app_desc, sha256_file
from benchctl.defaults import PROGRESS_S, REDACTED, RESET_WAIT_S, iso_now
from benchctl.errors import InternalError, TimeoutError_, UsageError
from benchctl.lease import pid_alive
from benchctl.profile import Profile
from benchctl.registry import Unit
from benchctl.types import (
    CmdResult,
    Cursor,
    Event,
    FlashFile,
    FlashResult,
    GateState,
    Identity,
    LogLine,
    Offset,
    ResetInfo,
    Status,
    Subscription,
    Transport,
    WaitResult,
)

FAKE_DAEMON_ENV = "FAKE_DAEMON_CONFIG"
FAKE_DAEMON = Path(__file__).resolve()
APP_VERSION = "1.2.3-4-gabc"
FAKE_MAC = "AA:BB:CC:DD:EE:01"
RST_POWERON = "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)"
RST_DOWNLOAD = "rst:0x1 (POWERON),boot:0x0 (DOWNLOAD(USB/UART0))"
BOOT_LINES = (
    "I (290) app_init: Project name:     fakeapp",
    f"I (291) app_init: App version:      {APP_VERSION}",
    "I (305) app_init: ELF file SHA256:  0123456789abcdef",
    "I (1150) console: shell started",
)
REPLIES = {
    "version": [f"Application Version: {APP_VERSION}"],
    "secret": ["sip_user=bench sip_pass=***"],
    "fail": ["ERROR: failed"],
}
NOT_FOUND = "command not found"
_POLL_S = 0.02
_REAP_S = 2.0


def knobs() -> dict[str, Any]:
    raw = os.environ.get(FAKE_DAEMON_ENV)
    data = json.loads(raw) if raw else {}
    return data if isinstance(data, dict) else {}


class FakeSession:
    """A `types.Session` over an in-memory log. Replies, resets and identity are canned;
    `wait` really scans the stored lines, so mark/wait-since semantics hold."""

    def __init__(self, unit: Unit, profile: Profile) -> None:
        self.unit = unit.name
        self.profile = profile
        self.session_dir = paths.sessions_dir(unit.name) / paths.timestamp()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        config = knobs()
        self._fields: dict[str, str] = dict(
            config.get("identity", {"fw_version": APP_VERSION, "mac": FAKE_MAC})
        )
        self._expect = dict(unit.expect)
        # Knobs for the daemon's lazy probe: U4 probes only inside identity(), and slowly.
        self._lazy = bool(config.get("identity_lazy", False))
        self._probe_s = float(config.get("identity_delay_s", 0.0))
        self._never_ready = bool(config.get("identity_timeout", False))
        self._refuse_cmds = bool(config.get("shell_not_ready", False))
        self._device = threading.RLock()
        self._cond = threading.Condition()
        self._lines: list[LogLine] = []
        self._marks: dict[str, Offset] = {}
        self._subs: list[Subscription] = []
        self._t0 = time.monotonic()
        self._attached_at = iso_now()
        self._seq = 0
        self._boot: Offset = 0
        self._gate = GateState("ready", 0.0, "pattern")
        self._last_reset: ResetInfo | None = None
        self.armed: list[tuple[str, float]] = []
        self._append(f"## attach unit={unit.name} port=fake baud={profile.console.baud}")
        self._last_reset = self._boot_sequence(RST_POWERON, expected=False, cause=None)
        self._identity: Identity | None = None if self._lazy else self._probe()

    # --- internals -------------------------------------------------------------------

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _append(self, text: str) -> LogLine:
        with self._cond:
            t = self._now()
            line = LogLine(
                len(self._lines), t, time.strftime("%H:%M:%S.000"), text, text[:3] == "## "
            )
            self._lines.append(line)
            self._subs = [sub for sub in self._subs if sub.offer(line)]
            self._cond.notify_all()
        return line

    def _boot_sequence(self, rst: str, *, expected: bool, cause: str | None) -> ResetInfo:
        line = self._append(rst)
        self._boot = line.offset
        if "DOWNLOAD" not in rst:
            for text in BOOT_LINES:
                self._append(text)
        info = ResetInfo(
            offset=line.offset,
            t=line.t,
            code=1,
            reason="POWERON",
            boot_mode="DOWNLOAD" if "DOWNLOAD" in rst else "SPI_FAST_FLASH_BOOT",
            elf_sha=None if "DOWNLOAD" in rst else "0123456789abcdef",
            expected=expected,
            cause=cause,
            context=[] if expected else [item.text for item in self._lines[-20:-1]],
        )
        self._last_reset = info
        self.emit({"type": "reset", **_reset_fields(info), "offset": info.offset})
        return info

    def _probe(self) -> Identity:
        time.sleep(self._probe_s)
        if self._never_ready:
            raise TimeoutError_("identity: shell not ready (state=booting)")
        mismatches = {
            name: (want, self._fields.get(name, ""))
            for name, want in self._expect.items()
            if self._fields.get(name) != want
        }
        return Identity(dict(self._fields), not mismatches, mismatches)

    # --- Session ---------------------------------------------------------------------

    def status(self) -> Status:
        with self._cond:
            offset = len(self._lines)
        return Status(
            unit=self.unit,
            pid=os.getpid(),
            attached_at=self._attached_at,
            port="fake",
            connected=True,
            baud=self.profile.console.baud,
            lease={},
            identity=self._identity,
            last_reset=self._last_reset,
            offset=offset,
            gate=self._gate,
            session_dir=str(self.session_dir),
        )

    def mark(self, name: str | None) -> Offset:
        with self._cond:
            offset = len(self._lines)
            if name is None:
                return offset
            self._marks[name] = offset
        self._append(f"## mark {name}")
        return offset

    def note(self, text: str) -> Offset:
        return self._append(f"## {text}").offset

    def resolve(self, cursor: str) -> Offset:
        parsed = Cursor.parse(cursor)
        if parsed.kind == "attach":
            return 0
        if parsed.kind == "boot":
            return self._boot
        if parsed.kind == "offset" and parsed.offset is not None:
            return parsed.offset
        with self._cond:
            if parsed.mark not in self._marks:
                raise UsageError(f"unknown mark {parsed.mark!r}")
            return self._marks[parsed.mark]

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
        start = time.monotonic()
        with self._device:
            self.profile.check_allowed(line)
            if self._refuse_cmds and not force:
                # Section 4.6: nothing is sent, offset is the next one, line names the state.
                refusal = "shell not ready (state=booting)"
                return CmdResult(len(self._lines), "timeout", refusal, [], None, 0.0)
            tx = self.note(f"tx {REDACTED if secret else line}")
            word = line.split()[0] if line.split() else ""
            if word == "reboot":
                self._boot_sequence(RST_POWERON, expected=True, cause=f"cmd:{line}")
                return CmdResult(tx, "ok", None, [], None, time.monotonic() - start)
            if word == "slow":
                time.sleep(timeout)
                return CmdResult(tx, "timeout", None, [], None, time.monotonic() - start)
            replies = REPLIES.get(word, [NOT_FOUND])
            for text in replies:
                self._append(text)
        duration = time.monotonic() - start
        for text in replies:
            if any(re.search(marker, text) for marker in error):
                return CmdResult(tx, "error", text, [], None, duration)
        if expect is not None:
            for text in replies:
                match = re.search(expect, text)
                if match:
                    return CmdResult(tx, "ok", text, list(match.groups()), None, duration)
            return CmdResult(tx, "timeout", None, [], None, duration)
        wants_capture = capture if capture is not None else self.profile.console.prompt is not None
        captured = "\n".join(replies) if wants_capture else None
        return CmdResult(tx, "ok", None, [], captured, duration)

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
        start = time.monotonic()
        if progress is not None:
            progress({"note": f"waiting for {pattern or absent or order}"})
        span = timeout if pattern or order else (window if window is not None else timeout)
        deadline = start + span
        with self._cond:
            while True:
                found = self._scan(pattern, since, absent, order, time.monotonic() - start)
                if found is not None:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    duration = time.monotonic() - start
                    result: Literal["timeout", "absent_ok"] = (
                        "absent_ok" if pattern is None and not order else "timeout"
                    )
                    return WaitResult(result, None, [], None, None, duration, [])
                self._cond.wait(remaining)

    def _scan(
        self,
        pattern: str | None,
        since: Offset,
        absent: str | None,
        order: list[str] | None,
        duration: float,
    ) -> WaitResult | None:
        lines = [line for line in self._lines[since:] if not line.meta]  # device lines only
        if order:
            hits: list[Offset] = []
            for line in lines:
                if len(hits) < len(order) and re.search(order[len(hits)], line.text):
                    hits.append(line.offset)
            if len(hits) == len(order):
                return WaitResult("order_ok", None, [], hits[-1], None, duration, hits)
            return None
        for line in lines:
            if absent is not None and re.search(absent, line.text):
                return WaitResult(
                    "absent_violated", line.text, [], line.offset, line.t, duration, []
                )
            match = re.search(pattern, line.text) if pattern is not None else None
            if match:
                groups = [group or "" for group in match.groups()]
                return WaitResult("match", line.text, groups, line.offset, line.t, duration, [])
        return None

    def log(
        self, since: Offset, until: Offset | None, grep: str | None, limit: int
    ) -> list[LogLine]:
        with self._cond:
            lines = self._lines[since:until]
        if grep is not None:
            lines = [line for line in lines if re.search(grep, line.text)]
        return lines[:limit]

    def reset(
        self, bootloader: bool, wait_rst: float = RESET_WAIT_S, *, cause: str = "reset"
    ) -> ResetInfo | None:
        with self._device:
            self.note(f"reset requested bootloader={str(bootloader).lower()}")
            if bootloader:
                info = self._boot_sequence(RST_DOWNLOAD, expected=True, cause=cause)
                self._gate = GateState("bootloader", self._now(), "reset")
                return info
            self._gate = GateState("ready", self._now(), "pattern")
            return self._boot_sequence(RST_POWERON, expected=True, cause=cause)

    def set_gate(self, state: Literal["booting", "bootloader"], method: str | None = None) -> None:
        self._gate = GateState(state, self._now(), method)

    def arm_reset(self, cause: str, window: float) -> None:
        self.armed.append((cause, window))

    def identity(self, reprobe: bool = False) -> Identity:
        if reprobe or self._identity is None:
            self._identity = self._probe()
        return self._identity

    def gate_check(self, since: Offset, timeout: float) -> dict[str, Any]:
        env_gate = self.profile.env_gate
        texts = [line.text for line in self.log(since, None, None, 1_000_000)]
        missing = [p for p in (env_gate.require if env_gate else []) if not _any(p, texts)]
        return {"ok": not missing, "missing": missing, "forbidden_hits": []}

    def subscribe(self) -> Subscription:
        feed = Subscription()
        with self._cond:
            self._subs.append(feed)
        return feed

    @contextlib.contextmanager
    def device_lock(self) -> Iterator[None]:
        with self._device:
            yield

    @contextlib.contextmanager
    def pause_reader(self) -> Iterator[Transport]:
        raise InternalError("the fake session has no transport")
        yield  # pragma: no cover

    def emit(self, event: Event) -> None:
        with self._cond:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": iso_now(),
                "t": round(self._now(), 3),
                "unit": self.unit,
                "fw": self._fields.get("fw_version"),
                "dev": self._fields.get("mac"),
                "offset": None,
                **event,
            }
            with (self.session_dir / "events.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(record) + "\n")
            self._subs = [sub for sub in self._subs if sub.offer(record)]

    def close(self) -> None:
        with self._cond:
            for sub in self._subs:
                sub.close()
            self._subs = []


def _any(pattern: str, texts: list[str]) -> bool:
    return any(re.search(pattern, text) for text in texts)


def _reset_fields(info: ResetInfo) -> dict[str, Any]:
    return {
        "code": info.code,
        "reason": info.reason,
        "boot_mode": info.boot_mode,
        "elf_sha": info.elf_sha,
        "expected": info.expected,
        "cause": info.cause,
        "context": info.context,
    }


# --- canned U6 -------------------------------------------------------------------------


def fake_flash_on_session(
    session: Any,
    files: list[tuple[int, str, str]],
    *,
    chip: str,
    before: str,
    after: str,
    baud: int,
    flash_args: dict[str, str],
) -> FlashResult:
    start = time.monotonic()
    images = [FlashFile(addr, path, name, *sha256_file(path)) for addr, path, name in files]
    app = next((read_app_desc(path) for _, path, name in files if name == "app"), None)
    session.note(f"flash begin {' '.join(f.name for f in images)} baud={baud} after={after}")
    session.note(f"flash end status=ok chip={chip} before={before}")
    reset: ResetInfo | None = None
    if after == "hard-reset":
        reset = session.reset(False, RESET_WAIT_S, cause="flash")
    else:
        session.set_gate("bootloader", method="flash")
    return FlashResult(images, app, baud, time.monotonic() - start, reset)


def fake_capture_on_session(session: Any, seconds: float) -> tuple[list[LogLine], ResetInfo | None]:
    reset = session.reset(False, RESET_WAIT_S)
    since = 0 if reset is None else reset.offset
    lines: list[LogLine] = session.log(since, None, None, 10_000)
    return lines, reset


# --- test helpers ----------------------------------------------------------------------


def register_unit(
    config_dir: Path, unit: str = "u1", *, expect: dict[str, str] | None = None
) -> Path:
    """`units.toml` with `unit` on the example profile. The fake session never opens a port,
    so the match only has to be well-formed."""
    # conftest pulls in pytest; the spawned daemon never needs it.
    from conftest import PROFILE_NAME, dump_toml

    path = config_dir / "units.toml"
    entry: dict[str, Any] = {"profile": PROFILE_NAME, "console": {"match": {"serial": "FAKE"}}}
    if expect:
        entry["expect"] = expect
    path.write_text(dump_toml({"units": {unit: entry}}), encoding="utf-8")
    return path


def daemon_argv() -> str:
    """The BENCHCTL_DAEMON_ARGV that spawns this file with the test interpreter."""
    return f"{sys.executable} {FAKE_DAEMON}"


def reap_daemons(state_dir: Path) -> None:
    """Stop every daemon under `state_dir` that a failed test left running."""
    for pid_file in state_dir.glob("*/daemon.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if pid == os.getpid() or not pid_alive(pid):
            continue
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + _REAP_S
        while time.monotonic() < deadline and not _reaped(pid):
            time.sleep(_POLL_S)
        if not _reaped(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            _reaped(pid)


def _reaped(pid: int) -> bool:
    # A daemon the test process spawned is its child: until waited for, a zombie looks alive.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return not pid_alive(pid)
    return done == pid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--profile", default=os.environ.get("BENCHCTL_PROFILE"))
    args = parser.parse_args(argv)
    config = knobs()
    marker = config.get("lose_race_once")
    if isinstance(marker, str) and not Path(marker).exists():
        # Act out a daemon that lost the lease.lock race and exited, once.
        Path(marker).touch()
        print(
            json.dumps({"ok": False, "error": {"code": "busy", "message": "lost"}}), file=sys.stderr
        )
        return 3
    flash.flash_on_session = fake_flash_on_session
    capture.capture_on_session = fake_capture_on_session
    progress = float(config.get("progress_s", PROGRESS_S))
    return daemon.main(
        args.unit, args.profile, session_factory=FakeSession, progress_interval=progress
    )


if __name__ == "__main__":
    sys.exit(main())
