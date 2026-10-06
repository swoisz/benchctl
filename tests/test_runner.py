"""The scenario runner against an in-memory FakeClient (design Sections 5 and 7.5-7.8)."""

from __future__ import annotations

import io
import json
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pytest

from benchctl import errors
from benchctl.profile import Command, Profile
from benchctl.runner import (
    RunResult,
    compute_budget,
    run_scenario,
    step_timeout,
)
from benchctl.scenario import Scenario, load_scenario
from benchctl.types import (
    CmdResult,
    DeviceClient,
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
    WaitResult,
)

FW = "1.2.3-4-gabc"
MAC = "AA:BB:CC:DD:EE:FF"
RUNNER_CURSORS = ("prev", "step", "trigger")
SERIAL_LINE = re.compile(r"\d\d:\d\d:\d\d\.\d{3} \+\d{5}\.\d{3} .*")
WaitOutcome = Literal[
    "match", "timeout", "absent_ok", "absent_violated", "order_ok", "order_violated"
]


class FakeClient:
    """An in-memory daemon behind the DeviceClient Protocol: a growing log with offsets and
    marks, scripted replies, resets with arming, and a tail feed of lines and events."""

    def __init__(
        self, *, identity: Identity | None = None, gate: dict[str, Any] | None = None
    ) -> None:
        self._cond = threading.Condition()
        self._t0 = time.monotonic()
        self._seq = 0
        self._subs: list[tuple[Subscription, bool]] = []
        self.lines: list[LogLine] = []
        self.marks: dict[str, Offset] = {}
        self.boot: Offset = 0
        self.armed: str | None = None
        self.calls: list[tuple[str, float, dict[str, Any]]] = []
        self.replies: dict[str, list[str]] = {"version": [f"Application Version: {FW}"]}
        self.reboot_lines: set[str] = set()
        self.identity_result = identity or Identity({"fw_version": FW, "mac": MAC}, True, {})
        self.gate_result = gate or {"ok": True, "missing": [], "forbidden_hits": []}
        self.reset_answers = True
        self.on_wait: Callable[[dict[str, Any]], None] | None = None
        self.on_cmd: Callable[[str], CmdResult | None] | None = None
        self.raise_in: dict[str, BaseException] = {}  # op -> raised once, when called
        self.line("## attach unit=u1 port=/dev/fake baud=115200", meta=True)

    # --- the simulated device --------------------------------------------------------

    def now(self) -> float:
        return time.monotonic() - self._t0

    def mono(self, line: LogLine) -> float:
        """When `line` was stored, on the test's monotonic clock."""
        return self._t0 + line.t

    def line(self, text: str, *, meta: bool = False) -> LogLine:
        with self._cond:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            stored = LogLine(len(self.lines), self.now(), ts, text, meta)
            self.lines.append(stored)
            self._offer(stored, is_event=False)
            self._cond.notify_all()
        return stored

    def line_later(self, delay: float, text: str) -> None:
        timer = threading.Timer(delay, self.line, args=(text,))
        timer.daemon = True
        timer.start()

    def find(self, text: str) -> LogLine:
        return next(line for line in self.lines if line.text == text)

    def event(self, kind: str, offset: Offset | None, **fields: Any) -> Event:
        with self._cond:
            self._seq += 1
            record: Event = {
                "seq": self._seq,
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "t": round(self.now(), 3),
                "unit": "u1",
                "fw": FW,
                "dev": MAC,
                "type": kind,
                "offset": offset,
                **fields,
            }
            self._offer(record, is_event=True)
        return record

    def device_reset(self, reason: str = "RTC_SW_CPU_RST", rst_code: int = 0xC) -> ResetInfo:
        stored = self.line(f"rst:{rst_code:#x} ({reason}),boot:0x8 (SPI_FAST_FLASH_BOOT)")
        cause, self.armed = self.armed, None
        self.boot = stored.offset
        info = ResetInfo(
            stored.offset,
            stored.t,
            rst_code,
            reason,
            "SPI_FAST_FLASH_BOOT",
            None,
            cause is not None,
            cause,
            [],
        )
        self.event(
            "reset",
            stored.offset,
            code=rst_code,
            reason=reason,
            boot_mode=info.boot_mode,
            elf_sha=None,
            expected=info.expected,
            cause=cause,
            context=[],
        )
        return info

    def _offer(self, item: LogLine | Event, *, is_event: bool) -> None:
        # Called under the lock, so subscribers see items in log order.
        for sub, events in list(self._subs):
            if (events or not is_event) and not sub.offer(item):
                self._subs.remove((sub, events))

    def _offset(self, cursor: Offset | str) -> Offset:
        if isinstance(cursor, int):
            return cursor
        if cursor in RUNNER_CURSORS:
            raise AssertionError(f"runner cursor {cursor!r} reached the daemon")
        if cursor == "attach":
            return 0
        if cursor == "boot":
            return self.boot
        if cursor in self.marks:
            return self.marks[cursor]
        raise errors.UsageError(f"unknown mark {cursor!r}")

    def _call(self, op: str, **args: Any) -> None:
        self.calls.append((op, time.monotonic(), args))
        if op in self.raise_in:
            raise self.raise_in.pop(op)

    def ops(self, op: str) -> list[dict[str, Any]]:
        return [args for name, _, args in self.calls if name == op]

    def step_starts(self) -> list[Offset]:
        """Offsets of the runner's unnamed marks: run start, each step's start, run end."""
        return [args["offset"] for args in self.ops("mark") if args["name"] is None]

    def mark_time(self, name: str) -> float:
        return next(t for op, t, args in self.calls if op == "mark" and args["name"] == name)

    # --- DeviceClient ----------------------------------------------------------------

    def hello(self, owner: str, client: str) -> dict[str, Any]:
        self._call("hello", owner=owner, client=client)
        return {"daemon": "fake", "proto": 1, "unit": "u1", "offset": len(self.lines)}

    def status(self) -> Status:
        self._call("status")
        attached = datetime.fromtimestamp(time.time() - self.now()).astimezone()
        return Status(
            unit="u1",
            pid=os.getpid(),
            attached_at=attached.isoformat(),
            port="/dev/fake",
            connected=True,
            baud=115200,
            lease={},
            identity=self.identity_result,
            last_reset=None,
            offset=len(self.lines),
            gate=GateState("ready", 0.0, "pattern"),
            session_dir="/tmp/fake-session",
        )

    def mark(self, name: str | None) -> Offset:
        with self._cond:
            offset = len(self.lines)
        self._call("mark", name=name, offset=offset)
        if name is not None:
            self.marks[name] = offset
            self.line(f"## mark {name}", meta=True)
            self.event("mark", offset, name=name)
        return offset

    def resolve(self, cursor: str) -> Offset:
        self._call("resolve", cursor=cursor)
        return self._offset(cursor)

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
        self._call(
            "cmd",
            line=line,
            expect=expect,
            error=error,
            capture=capture,
            timeout=timeout,
            reboots=reboots,
            source=source,
        )
        if self.on_cmd is not None:
            override = self.on_cmd(line)
            if override is not None:
                return override
        if reboots:
            self.armed = f"cmd:{line}"
        tx = self.line(f"## tx {line}", meta=True)
        self.event("tx", tx.offset, line=line, source=source, request_id="r1")
        stored = [self.line(text) for text in self.replies.get(line, ["command not found"])]
        if line in self.reboot_lines:
            self.device_reset()
        status: Literal["ok", "error", "timeout"] = "ok"
        hit: str | None = None
        errored = [s.text for s in stored if any(re.search(p, s.text) for p in error)]
        if errored:
            status, hit = "error", errored[0]
        elif expect is not None:
            hit = next((s.text for s in stored if re.search(expect, s.text)), None)
            status = "ok" if hit is not None else "timeout"
        captured = "\n".join(s.text for s in stored) if capture else None
        return CmdResult(tx.offset, status, hit, [], captured, 0.01)

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
        args: dict[str, Any] = {"pattern": pattern, "since": since, "timeout": timeout}
        args |= {"absent": absent, "window": window, "order": order}
        self._call("wait", **args)
        if self.on_wait is not None:
            self.on_wait(args)
        begun = time.monotonic()
        position = self._offset(since)
        # U4's contract (Section 7.1): device lines only; with a pattern or order, `absent`
        # is watched until the match and a declared window holds the match to its end; with
        # `absent` alone the watch is the window, else the timeout.
        sequence = list(order or ([] if pattern is None else [pattern]))
        watch = window if window is not None or sequence else timeout
        watch_until = None if watch is None else begun + watch
        matched: list[LogLine] = []
        with self._cond:
            while True:
                for stored in self.lines[position:]:
                    position = stored.offset + 1
                    if stored.meta:
                        continue
                    if len(matched) < len(sequence) and re.search(
                        sequence[len(matched)], stored.text
                    ):
                        matched.append(stored)
                        continue
                    watching = len(matched) < len(sequence) or (
                        watch_until is not None and self.mono(stored) <= watch_until
                    )
                    if absent is not None and watching and re.search(absent, stored.text):
                        return self._waited("absent_violated", stored, begun, matched, order)
                now = time.monotonic()
                complete = len(matched) == len(sequence)
                if complete and (watch_until is None or now >= watch_until):
                    if not sequence:
                        return self._waited("absent_ok", None, begun, matched, order)
                    kind: WaitOutcome = "order_ok" if order else "match"
                    return self._waited(kind, matched[-1], begun, matched, order)
                if not complete and now >= begun + timeout:
                    return self._waited("timeout", None, begun, matched, order)
                wakes = [] if complete else [begun + timeout]
                if watch_until is not None:
                    wakes.append(watch_until)
                self._cond.wait(max(min(wakes) - now, 0.001))

    def _waited(
        self,
        result: WaitOutcome,
        stored: LogLine | None,
        begun: float,
        matched: list[LogLine],
        order: list[str] | None,
    ) -> WaitResult:
        offsets = [line.offset for line in matched] if order else []
        duration = time.monotonic() - begun
        if stored is None:
            return WaitResult(result, None, [], None, None, duration, offsets)
        return WaitResult(result, stored.text, [], stored.offset, stored.t, duration, offsets)

    def log(
        self, since: Offset | str, until: Offset | str | None, grep: str | None, limit: int
    ) -> tuple[list[LogLine], Offset]:
        self._call("log", since=since, until=until, limit=limit)
        start = self._offset(since)
        with self._cond:
            end = len(self.lines) if until is None else self._offset(until)
            window = self.lines[start:end]
        picked = [line for line in window if grep is None or re.search(grep, line.text)][:limit]
        return picked, picked[-1].offset + 1 if picked else start

    def reset(self, bootloader: bool, wait_rst: float = 5.0) -> tuple[Offset, ResetInfo | None]:
        self._call("reset", bootloader=bootloader, wait_rst=wait_rst)
        requested = self.line(f"## reset requested bootloader={str(bootloader).lower()}", meta=True)
        if not self.reset_answers:
            return requested.offset, None
        self.armed = "reset"
        return requested.offset, self.device_reset("POWERON", 0x1)

    def arm_reset(self, cause: str, window: float) -> None:
        self._call("arm_reset", cause=cause, window=window)
        self.armed = cause

    def flash(
        self,
        files: list[tuple[int, str, str]],
        *,
        after: str,
        baud: int,
        flash_args: dict[str, str],
    ) -> FlashResult:
        self._call("flash", files=files, after=after, baud=baud, flash_args=flash_args)
        self.line("## flash begin", meta=True)
        self.line("## flash end status=ok", meta=True)
        reset = None
        if after == "hard-reset":
            self.armed = "flash"
            reset = self.device_reset()
        flashed = [FlashFile(addr, path, name, "0" * 64, 0) for addr, path, name in files]
        return FlashResult(flashed, None, baud, 0.1, reset)

    def capture(self, seconds: float) -> tuple[list[LogLine], ResetInfo | None]:
        self._call("capture", seconds=seconds)
        return [], None

    def identity(self, reprobe: bool = False) -> Identity:
        self._call("identity", reprobe=reprobe)
        return self.identity_result

    def gate(self, since: Offset | str, timeout: float) -> dict[str, Any]:
        self._call("gate", since=since, timeout=timeout)
        return dict(self.gate_result)

    def tail(self, since: Offset | str, events: bool) -> Subscription:
        self._call("tail", since=since, events=events)
        sub = Subscription()
        with self._cond:
            for stored in self.lines[self._offset(since) :]:
                sub.offer(stored)
            self._subs.append((sub, events))
        return sub

    def lease(self, action: Literal["take", "release"]) -> dict[str, Any]:
        self._call("lease", action=action)
        return {"owner": "test"}

    def detach(self) -> None:
        self._call("detach")


def as_device_client(fake: FakeClient) -> DeviceClient:
    """mypy checks here that FakeClient implements the whole Protocol, `hello` included."""
    return fake


def scenario(tmp_path: Path, body: str, name: str = "t") -> Scenario:
    path = tmp_path / "scenario-under-test.toml"
    path.write_text(f"name = {json.dumps(name)}\n{body}", encoding="utf-8")
    return load_scenario(path)


def run(
    fake: FakeClient,
    loaded: Scenario,
    profile: Profile,
    tmp_path: Path,
    *,
    variables: dict[str, str] | None = None,
    skip_gate: bool = False,
    dry_run: bool = False,
) -> tuple[RunResult, str]:
    out = io.StringIO()
    result = run_scenario(
        as_device_client(fake),
        loaded,
        profile,
        run_dir=tmp_path / "run",
        variables={} if variables is None else variables,
        skip_gate=skip_gate,
        dry_run=dry_run,
        prompt_out=out,
    )
    return result, out.getvalue()


def events_of(result: RunResult) -> list[dict[str, Any]]:
    text = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def results_of(step_list: list[dict[str, object]]) -> list[object]:
    return [step["result"] for step in step_list]


def undo_body(tmp_path: Path, third: str) -> str:
    cwd = json.dumps(str(tmp_path))
    return f"""
[[step]]
kind = "external"
run = "true"
undo = "echo A >> undo.log"
cwd = {cwd}

[[step]]
kind = "external"
run = "true"
undo = "echo B >> undo.log"
cwd = {cwd}
{third}
[[step]]
kind = "mark"
name = "after"
"""


# --- `at` and triggers -------------------------------------------------------------------


def test_at_counts_from_the_last_trigger_match_not_the_previous_step(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    body = """
[[step]]
kind = "wait"
pattern = "^ready$"
since = "attach"
timeout = "1s"

[[step]]
kind = "external"
run = "sleep 0.15"

[[step]]
kind = "mark"
name = "go"
at = "+0.3s"
"""
    fake.line_later(0.1, "ready")
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    since_match = fake.mark_time("go") - fake.mono(fake.find("ready"))
    # From the end of the external step it would be at least 0.45 s.
    assert 0.3 <= since_match < 0.42


def test_at_counts_from_a_stale_match_not_from_when_the_wait_returned(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    fake.line("ready")
    # Device chatter during the run is how the runner learns the daemon's clock.
    fake.line_later(0.1, "tick")
    body = """
[[step]]
kind = "external"
run = "sleep 0.3"

[[step]]
kind = "wait"
pattern = "^ready$"
since = "attach"
timeout = "1s"

[[step]]
kind = "mark"
name = "go"
at = "+0.45s"
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    since_match = fake.mark_time("go") - fake.mono(fake.find("ready"))
    # From the wait's return (0.3 s after "ready") it would be at least 0.75 s.
    assert 0.45 <= since_match < 0.6


def test_at_before_any_trigger_counts_from_scenario_start(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = '[[step]]\nkind = "mark"\nname = "go"\nat = "+0.2s"\n'
    called = time.monotonic()
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass"
    assert 0.2 <= fake.mark_time("go") - called < 0.35


# --- cursors -----------------------------------------------------------------------------


def test_since_resolution_for_runner_and_daemon_cursors(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    fake.line("I (12) boot: first device line")
    printed: list[LogLine] = []

    def print_late_once(args: dict[str, Any]) -> None:
        if args["pattern"] == "late" and not printed:
            printed.append(fake.line("late"))

    fake.on_wait = print_late_once
    body = """
[[step]]
kind = "mark"
name = "m1"

[[step]]
kind = "cmd"
send = "version"

[[step]]
name = "prev"
kind = "wait"
pattern = "Application Version"
timeout = "0.5s"

[[step]]
name = "step"
kind = "wait"
pattern = "late"
since = "step"
timeout = "0.5s"

[[step]]
name = "trigger"
kind = "wait"
pattern = "late"
since = "trigger"
timeout = "0.5s"

[[step]]
name = "mark"
kind = "wait"
pattern = "Application Version"
since = "m1"
timeout = "0.5s"

[[step]]
name = "attach"
kind = "wait"
pattern = "first device line"
since = "attach"
timeout = "0.5s"
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert results_of(result.steps) == ["pass"] * 7, result.steps
    starts = fake.step_starts()[1:-1]  # step 1..7
    sinces = [args["since"] for args in fake.ops("wait")]
    assert sinces == [starts[1], starts[3], printed[0].offset, "m1", "attach"]
    assert all(type(since) is int for since in sinces[:3])


def test_prev_on_the_first_step_is_the_run_start(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = '[[step]]\nkind = "wait"\nabsent = "rst:"\nwindow = "0.05s"\ntimeout = "0.05s"\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    run_start = fake.step_starts()[0]
    assert fake.ops("wait")[0]["since"] == run_start
    assert fake.ops("wait")[0]["window"] == 0.05


def test_trigger_before_any_trigger_is_the_run_start(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = """
[[step]]
kind = "wait"
absent = "rst:"
since = "trigger"
timeout = "0.05s"
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass"
    assert fake.ops("wait")[0]["since"] == fake.step_starts()[0]
    assert fake.ops("wait")[0]["window"] is None
    assert result.steps[0]["detail"] == "no `rst:` in 0.05 s"


def test_a_pattern_wait_with_absent_and_no_window_returns_on_its_match(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    fake.on_wait = lambda args: fake.line_later(0.05, "held for the call")
    body = """
[[step]]
kind = "wait"
pattern = "held for the call"
absent = "rst:"
timeout = "1s"
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    assert fake.ops("wait")[0]["window"] is None
    duration = result.steps[0]["duration"]
    assert isinstance(duration, float) and duration < 0.5
    held = fake.find("held for the call")
    assert result.steps[0]["detail"] == f"`{held.text}` @ {held.offset}; no `rst:` before it"


def test_a_declared_window_holds_a_matched_wait_and_watches_absent(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()

    def device(args: dict[str, Any]) -> None:
        fake.line_later(0.02, "held for the call")
        fake.line_later(0.1, "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)")

    fake.on_wait = device
    body = """
[[step]]
kind = "wait"
pattern = "held for the call"
absent = "rst:"
window = "0.3s"
timeout = "1s"
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert fake.ops("wait")[0]["window"] == 0.3
    assert results_of(result.steps) == ["fail"]
    assert "absent_violated" in str(result.steps[0]["detail"])


# --- human -------------------------------------------------------------------------------


class _NoInput:
    """A stdin that fails the test if the runner ever reads it."""

    def read(self, size: int = -1) -> str:
        raise AssertionError("the runner read stdin")

    def readline(self, size: int = -1) -> str:
        raise AssertionError("the runner read stdin")

    def fileno(self) -> int:
        raise AssertionError("the runner touched stdin")


def test_human_step_passes_on_its_confirm_line_without_input(
    tmp_path: Path, profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", _NoInput())
    fake = FakeClient()

    def operator_presses_reset(args: dict[str, Any]) -> None:
        timer = threading.Timer(0.05, fake.device_reset, args=("POWERON", 0x1))
        timer.daemon = True
        timer.start()

    fake.on_wait = operator_presses_reset
    body = """
[[step]]
name = "operator resets the unit"
kind = "human"
prompt = "Press the unit's RESET button."
confirm = 'rst:0x[0-9a-fA-F]+'
expect_reset = true
timeout = "1s"
"""
    result, out = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    assert "\a[step 1] Press the unit's RESET button." in out
    rst = next(line for line in fake.lines if line.text.startswith("rst:"))
    assert fake.ops("wait")[0]["since"] == fake.step_starts()[1]
    window = 1.0 + profile.boot.expected_reset_window
    assert fake.ops("arm_reset") == [{"cause": "step:1", "window": window}]
    human = [event for event in events_of(result) if event["type"] == "human"]
    assert [(h["result"], h["line"], h["offset"]) for h in human] == [
        ("confirmed", rst.text, rst.offset)
    ]
    resets = [event for event in events_of(result) if event["type"] == "reset"]
    assert [(r["expected"], r["cause"]) for r in resets] == [(True, "step:1")]
    results_md = (result.run_dir / "results.md").read_text(encoding="utf-8")
    assert "Resets: 1 expected (step 1), 0 unexpected." in results_md


def test_human_step_fails_when_nobody_confirms(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = '[[step]]\nkind = "human"\nprompt = "p"\nconfirm = "never"\ntimeout = "0.1s"\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "fail"
    assert result.steps[0]["detail"] == "not confirmed within 0.1s"
    human = [event for event in events_of(result) if event["type"] == "human"]
    assert [h["result"] for h in human] == ["timeout"]


# --- undo --------------------------------------------------------------------------------


def test_undo_runs_in_reverse_after_success(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    result, _ = run(fake, scenario(tmp_path, undo_body(tmp_path, "")), profile, tmp_path)
    assert result.result == "pass", result.steps
    assert (tmp_path / "undo.log").read_text() == "B\nA\n"
    phases = [(e["phase"], e["cmd"]) for e in events_of(result) if e["type"] == "external"]
    assert phases == [
        ("run", "true"),
        ("run", "true"),
        ("undo", "echo B >> undo.log"),
        ("undo", "echo A >> undo.log"),
    ]


def test_undo_runs_in_reverse_after_a_failed_step(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    failing = '\n[[step]]\nkind = "external"\nrun = "exit 3"\n'
    result, _ = run(fake, scenario(tmp_path, undo_body(tmp_path, failing)), profile, tmp_path)
    assert result.result == "fail"
    assert results_of(result.steps) == ["pass", "pass", "fail", "skip"]
    assert result.steps[2]["detail"] == "exit 3"
    assert result.steps[3]["detail"] == "not run: step 3 failed"
    assert result.exit_code == 1
    assert (tmp_path / "undo.log").read_text() == "B\nA\n"


@pytest.mark.parametrize(
    ("signum", "detail"),
    [(signal.SIGINT, "interrupted by SIGINT"), (signal.SIGTERM, "interrupted by SIGTERM")],
)
def test_undo_runs_in_reverse_on_an_interrupt(
    tmp_path: Path, profile: Profile, signum: int, detail: str
) -> None:
    fake = FakeClient()
    fake.on_wait = lambda args: os.kill(os.getpid(), signum)
    waiting = '\n[[step]]\nkind = "wait"\npattern = "never"\ntimeout = "1s"\n'
    previous = signal.getsignal(signal.SIGTERM)
    # A shell starts background jobs with SIGINT ignored; Ctrl-C reaches a foreground CLI,
    # whose Python turns it into KeyboardInterrupt.
    sigint = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        result, _ = run(fake, scenario(tmp_path, undo_body(tmp_path, waiting)), profile, tmp_path)
    finally:
        signal.signal(signal.SIGINT, sigint)
    assert result.result == "fail"
    assert results_of(result.steps) == ["pass", "pass", "error", "skip"]
    assert result.steps[2]["detail"] == detail
    assert result.steps[3]["detail"] == f"not run: {detail}"
    assert (tmp_path / "undo.log").read_text() == "B\nA\n"
    assert (result.run_dir / "results.md").is_file()
    assert signal.getsignal(signal.SIGTERM) == previous


class _InterruptingOut(io.StringIO):
    """prompt_out that raises KeyboardInterrupt the first time `trigger` is written: an
    interrupt landing between steps, outside any step's own handling."""

    def __init__(self, trigger: str) -> None:
        super().__init__()
        self.trigger = trigger
        self.fired = False

    def write(self, text: str, /) -> int:
        if not self.fired and self.trigger in text:
            self.fired = True
            raise KeyboardInterrupt
        return super().write(text)


def test_undo_runs_in_reverse_on_an_interrupt_between_steps(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    # Step 2's progress line: both undos are pushed and step 3 has not started.
    out = _InterruptingOut("[2/3] pass")
    result = run_scenario(
        as_device_client(fake),
        scenario(tmp_path, undo_body(tmp_path, "")),
        profile,
        run_dir=tmp_path / "run",
        variables={},
        skip_gate=False,
        dry_run=False,
        prompt_out=out,
    )
    assert out.fired
    assert result.result == "fail"
    assert results_of(result.steps) == ["pass", "pass", "skip"]
    assert result.steps[2]["detail"] == "not run: interrupted by SIGINT"
    assert fake.ops("mark")[-1]["name"] is None, "step 3 (a named mark) never ran"
    assert (tmp_path / "undo.log").read_text() == "B\nA\n"
    assert (result.run_dir / "results.md").is_file()
    assert json.loads((result.run_dir / "summary.json").read_text())["result"] == "fail"


def test_an_interrupt_during_the_preconditions_fails_the_run(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    fake.raise_in["gate"] = KeyboardInterrupt()
    body = "[preconditions]\nenv_gate = true\n" + '[[step]]\nkind = "cmd"\nsend = "version"\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "fail"
    assert results_of(result.steps) == ["skip"]
    assert result.steps[0]["detail"] == "not run: interrupted by SIGINT"
    assert fake.ops("cmd") == []
    assert (result.run_dir / "results.md").is_file()


def test_an_interrupt_during_the_final_log_fetch_still_writes_the_report(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    body = '[[step]]\nkind = "mark"\nname = "m"\n'
    # The mark step's evidence lookup is the first `log` call; the serial.log fetch the next.
    original = fake.log
    calls: list[int] = []

    def log(
        since: Offset | str, until: Offset | str | None, grep: str | None, limit: int
    ) -> tuple[list[LogLine], Offset]:
        calls.append(limit)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return original(since, until, grep, limit)

    fake.log = log  # type: ignore[method-assign]
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert calls[1] > 1, "the second log call is the serial.log fetch"
    assert result.result == "fail"
    assert events_of(result)[-1]["result"] == "fail"
    results_md = (result.run_dir / "results.md").read_text(encoding="utf-8")
    assert results_md.startswith("## t — fail (")
    assert "- serial.log is incomplete: interrupted by SIGINT" in results_md


def test_a_failed_undo_fails_the_run_but_not_the_other_undos(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    cwd = json.dumps(str(tmp_path))
    body = f"""
[[step]]
kind = "external"
run = "true"
undo = "echo A >> undo.log"
cwd = {cwd}

[[step]]
kind = "external"
run = "true"
undo = "exit 7"
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "fail"
    assert results_of(result.steps) == ["pass", "pass"]
    assert (tmp_path / "undo.log").read_text() == "A\n"
    undos = [(e["cmd"], e["exit"]) for e in events_of(result) if e.get("phase") == "undo"]
    assert undos == [("exit 7", 7), ("echo A >> undo.log", 0)]
    results_md = (result.run_dir / "results.md").read_text(encoding="utf-8")
    assert "- Undo of step 2 failed (exit 7): `exit 7`" in results_md


# --- the run directory -------------------------------------------------------------------

RUN_DIR_SCENARIO = """
budget = "1m"

[[step]]
name = "place a call"
kind = "human"
prompt = "Place a call to the unit."
confirm = "ENTERED new state:.*CALL_ACTIVE"
timeout = "1s"

[[step]]
name = "reboot rpc"
kind = "external"
run = "true"

[[step]]
kind = "cmd"
send = "mqtt_silent 600"

[[step]]
name = "held for the call"
kind = "wait"
pattern = "held for the call"
absent = "rst:"
window = "0.2s"
timeout = "0.5s"

[[step]]
kind = "cmd"
command = "restart"

[[step]]
kind = "mark"
name = "done"
"""


def test_run_dir_holds_the_six_files_and_results_md_follows_7_7(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    fake.line("before the run")
    fake.replies["mqtt_silent 600"] = ["OK"]
    fake.replies["restart"] = []
    fake.reboot_lines.add("restart")

    def device(args: dict[str, Any]) -> None:
        if args["pattern"] == "ENTERED new state:.*CALL_ACTIVE":
            fake.line_later(0.05, "I (812) sm: ENTERED new state: CALL_ACTIVE")
        if args["pattern"] == "held for the call":
            fake.line_later(0.05, "… held for the call")

    fake.on_wait = device
    loaded = scenario(tmp_path, RUN_DIR_SCENARIO, name="restart held during a call")
    result, out = run(fake, loaded, profile, tmp_path)
    assert result.result == "pass", result.steps
    run_dir = result.run_dir
    files = sorted(path.name for path in run_dir.iterdir())
    assert files == [
        "events.jsonl",
        "profile.toml",
        "results.md",
        "scenario.toml",
        "serial.log",
        "summary.json",
    ]
    assert (run_dir / "scenario.toml").read_bytes() == loaded.path.read_bytes()
    assert (run_dir / "profile.toml").read_bytes() == profile.path.read_bytes()

    # serial.log: the run's slice in the session log's line format.
    first, end = fake.step_starts()[0], fake.step_starts()[-1]
    serial = (run_dir / "serial.log").read_text(encoding="utf-8").splitlines()
    assert all(SERIAL_LINE.fullmatch(line) for line in serial), serial
    assert [line.split(" ", 2)[2] for line in serial] == [
        line.text for line in fake.lines[first:end]
    ]
    assert "before the run" not in "\n".join(serial)

    # events.jsonl: renumbered, daemon records keep daemon_seq, run start and end bracket it.
    events = events_of(result)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert (events[0]["type"], events[0]["phase"]) == ("run", "start")
    assert (events[-1]["type"], events[-1]["phase"], events[-1]["result"]) == (
        "run",
        "end",
        "pass",
    )
    runner_types = {"run", "step", "human", "external"}
    for event in events:
        assert ("daemon_seq" in event) == (event["type"] not in runner_types), event
    assert [e["type"] for e in events if "daemon_seq" in e] == ["tx", "tx", "reset", "mark"]
    steps = [event for event in events if event["type"] == "step"]
    assert [(s["index"], s["result"]) for s in steps] == [(n, "pass") for n in range(1, 7)]

    # summary.json: the `run --json` result.
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary == json.loads(json.dumps(result.to_json("u1")))
    assert (summary["ok"], summary["unit"], summary["result"]) == (True, "u1", "pass")
    assert summary["budget"] == {"declared": 60.0, "computed": compute_budget(loaded, profile)}
    assert [set(step) for step in summary["steps"]] == [
        {"index", "name", "kind", "result", "duration", "detail"}
    ] * 6

    # results.md
    call = fake.find("I (812) sm: ENTERED new state: CALL_ACTIVE")
    held = fake.find("… held for the call")
    tx = fake.find("## tx mqtt_silent 600")
    restart = fake.find("## tx restart")
    done = fake.find("## mark done")
    lines = (run_dir / "results.md").read_text(encoding="utf-8").splitlines()
    assert re.fullmatch(
        rf"## restart held during a call — pass \(u1, fw {re.escape(FW)}, \d{{4}}-\d\d-\d\d\)",
        lines[0],
    )
    assert lines[1:4] == [
        "",
        "| # | Step | Kind | Result | t (s) | Evidence |",
        "|---|------|------|--------|-------|----------|",
    ]
    cells = [[cell.strip() for cell in row.strip("|").split(" | ")] for row in lines[4:10]]
    assert [row[:4] for row in cells] == [
        ["1", "place a call", "human", "pass"],
        ["2", "reboot rpc", "external", "pass"],
        ["3", "mqtt_silent 600", "cmd", "pass"],
        ["4", "held for the call", "wait", "pass"],
        ["5", "restart", "cmd", "pass"],
        ["6", "done", "mark", "pass"],
    ]
    times = [float(row[4]) for row in cells]
    assert times == sorted(times)
    assert [row[5] for row in cells] == [
        f"`{call.text}` @ {call.offset}",
        "exit 0",
        f"`{tx.text}` @ {tx.offset}",
        f"`{held.text}` @ {held.offset}; no `rst:` in 0.2 s",
        f"`{restart.text}` @ {restart.offset}",
        f"`{done.text}` @ {done.offset}",
    ]
    assert lines[10:] == ["", f"Resets: 1 expected (step 5), 0 unexpected. Run dir: `{run_dir}`."]
    assert "worst case: 1m (declared)" in out


def test_an_unexpected_reset_is_counted_against_its_step(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = """
[[step]]
kind = "wait"
pattern = "boom"
timeout = "0.5s"

[[step]]
kind = "wait"
absent = "rst:"
since = "trigger"
window = "0.1s"
timeout = "0.1s"
"""

    def crash_during_second_wait(args: dict[str, Any]) -> None:
        if args["pattern"] == "boom":
            fake.line_later(0.02, "boom")
        else:
            fake.device_reset()

    fake.on_wait = crash_during_second_wait
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert results_of(result.steps) == ["pass", "fail"]
    results_md = (result.run_dir / "results.md").read_text(encoding="utf-8")
    assert "Resets: 0 expected, 1 unexpected (step 2)." in results_md
    assert "absent_violated" in str(result.steps[1]["detail"])


# --- budget ------------------------------------------------------------------------------

ONE_OF_EACH = """
[[step]]
kind = "human"
prompt = "p"
confirm = "c"

[[step]]
kind = "external"
run = "true"
at = "+5s"

[[step]]
kind = "cmd"
send = "free"

[[step]]
kind = "wait"
pattern = "x"
at = "+2m"

[[step]]
kind = "reset"

[[step]]
kind = "flash"
files = [{ addr = "0x10000", file = "app.bin" }]

[[step]]
kind = "mark"
name = "m"
"""


def test_compute_budget_sums_kind_defaults_and_at_offsets(tmp_path: Path, profile: Profile) -> None:
    wide_wait = '\n[[step]]\nkind = "wait"\nabsent = "rst:"\nwindow = "90s"\ntimeout = "30s"\n'
    loaded = scenario(tmp_path, ONE_OF_EACH + wide_wait)
    console = profile.console.timeout
    # The last wait counts its 90 s window, not its 30 s timeout.
    assert compute_budget(loaded, profile) == 600 + 5 + 60 + console + 120 + 60 + 5 + 300 + 0 + 90


def test_cmd_timeout_falls_back_from_step_to_map_to_console(
    tmp_path: Path, profile: Profile
) -> None:
    commands = {**profile.commands, "slow": Command(name="slow", send="slow", timeout=30.0)}
    profile = replace(profile, commands=commands)
    body = """
[[step]]
kind = "cmd"
command = "slow"
timeout = "2s"

[[step]]
kind = "cmd"
command = "slow"

[[step]]
kind = "cmd"
command = "version"
"""
    steps = scenario(tmp_path, body).steps
    assert [step_timeout(step, profile) for step in steps] == [2.0, 30.0, profile.console.timeout]


def test_budget_line_reports_declared_and_computed_and_warns_when_short(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()
    loaded = scenario(tmp_path, 'budget = "1m"\n' + ONE_OF_EACH)
    result, out = run(fake, loaded, profile, tmp_path, dry_run=True)
    computed = compute_budget(loaded, profile)
    assert result.budget == {"declared": 60.0, "computed": computed}
    assert out.splitlines() == [
        "worst case: 1m (declared) / 19m18s (computed)",
        "warning: declared budget 1m is less than the computed worst case 19m18s",
    ]


def test_budget_line_without_a_declared_budget(tmp_path: Path, profile: Profile) -> None:
    loaded = scenario(tmp_path, '[[step]]\nkind = "mark"\nname = "m"\nat = "+30s"\n')
    _, out = run(FakeClient(), loaded, profile, tmp_path, dry_run=True)
    assert out.splitlines() == ["worst case: 30s (computed)"]


def test_dry_run_touches_neither_the_unit_nor_the_disk(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    result, _ = run(fake, scenario(tmp_path, ONE_OF_EACH), profile, tmp_path, dry_run=True)
    assert (result.result, result.steps) == ("pass", [])
    assert fake.calls == []
    assert not result.run_dir.exists()


# --- preconditions -----------------------------------------------------------------------

STEPS_AFTER_GATE = '[[step]]\nkind = "cmd"\nsend = "version"\n'


def assert_precondition_failed(result: RunResult, fake: FakeClient, message: str) -> None:
    assert result.result == "precondition_failed"
    assert result.exit_code == 1
    assert fake.ops("cmd") == []
    assert results_of(result.steps) == ["skip"]
    assert result.steps[0]["detail"] == f"not run: precondition failed: {message}"
    summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert (summary["ok"], summary["result"]) == (False, "precondition_failed")
    results_md = (result.run_dir / "results.md").read_text(encoding="utf-8")
    assert " — precondition_failed (u1, " in results_md.splitlines()[0]
    assert f"- Precondition failed: {message}" in results_md
    end = events_of(result)[-1]
    assert (end["type"], end["phase"], end["result"]) == ("run", "end", "precondition_failed")


def test_fw_version_mismatch_is_a_failed_precondition(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = "[preconditions]\nfw_version = '^9\\.'\n" + STEPS_AFTER_GATE
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert_precondition_failed(result, fake, f"fw_version {FW!r} does not match '^9\\\\.'")


def test_fw_version_without_the_identity_field(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient(identity=Identity({"mac": MAC}, True, {}))
    body = "[preconditions]\nfw_version = '^1'\n" + STEPS_AFTER_GATE
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    message = "fw_version: the unit's identity has no fw_version field"
    assert_precondition_failed(result, fake, message)


def test_env_gate_without_a_profile_gate(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = "[preconditions]\nenv_gate = true\n" + STEPS_AFTER_GATE
    result, _ = run(fake, scenario(tmp_path, body), replace(profile, env_gate=None), tmp_path)
    assert_precondition_failed(result, fake, "env_gate: profile 'esp-idf' has no [env_gate] table")
    assert fake.ops("gate") == []


def test_env_gate_failure(tmp_path: Path, profile: Profile) -> None:
    hit = {"pattern": "Guru", "line": "Guru Meditation Error", "offset": 3}
    fake = FakeClient(gate={"ok": False, "missing": ["sta ip"], "forbidden_hits": [hit]})
    body = "[preconditions]\nenv_gate = true\n" + STEPS_AFTER_GATE
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    message = "env gate: missing `sta ip`; forbidden `Guru Meditation Error` @ 3"
    assert_precondition_failed(result, fake, message)
    assert profile.env_gate is not None
    assert fake.ops("gate") == [{"since": "boot", "timeout": profile.env_gate.timeout}]


def test_preconditions_that_hold_let_the_steps_run(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = "[preconditions]\nenv_gate = true\nfw_version = '^1\\.2'\n" + STEPS_AFTER_GATE
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    assert len(fake.ops("gate")) == 1


def test_skip_gate_bypasses_the_env_gate(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient(gate={"ok": False, "missing": ["x"], "forbidden_hits": []})
    body = "[preconditions]\nenv_gate = true\n" + STEPS_AFTER_GATE
    result, out = run(fake, scenario(tmp_path, body), profile, tmp_path, skip_gate=True)
    assert result.result == "pass"
    assert fake.ops("gate") == []
    assert "env gate skipped (--skip-gate)" in out


# --- cmd ---------------------------------------------------------------------------------


def test_step_expect_overrides_the_map_reply_mode(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = """
[[step]]
kind = "cmd"
command = "version"
expect = 'Version:\\s*\\S+'
error = ["not found"]
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    sent = fake.ops("cmd")[0]
    assert (sent["line"], sent["expect"], sent["capture"], sent["error"]) == (
        "version",
        r"Version:\s*\S+",
        None,
        ["not found"],
    )
    assert (sent["source"], sent["timeout"], sent["reboots"]) == (
        "runner",
        profile.console.timeout,
        False,
    )


def test_map_entry_supplies_expect_error_timeout_and_reboots(
    tmp_path: Path, profile: Profile
) -> None:
    probe = Command(
        name="probe", send="probe {n}", expect="PASSED", error=["FAIL"], timeout=3.0, reboots=True
    )
    profile = replace(profile, commands={**profile.commands, "probe": probe})
    fake = FakeClient()
    fake.replies["probe 2"] = ["PASSED"]
    body = '[[step]]\nkind = "cmd"\ncommand = "probe"\nargs = { n = 2 }\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    sent = fake.ops("cmd")[0]
    assert (sent["line"], sent["expect"], sent["error"], sent["timeout"], sent["reboots"]) == (
        "probe 2",
        r"^\s*PASSED",
        ["FAIL"],
        3.0,
        True,
    )


def test_map_capture_without_step_overrides(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = '[[step]]\nkind = "cmd"\ncommand = "version"\n'
    run(fake, scenario(tmp_path, body), profile, tmp_path)
    sent = fake.ops("cmd")[0]
    assert (sent["expect"], sent["capture"], sent["error"]) == (None, True, [])


def test_a_cmd_the_shell_was_not_ready_for_has_no_tx_evidence(
    tmp_path: Path, profile: Profile
) -> None:
    fake = FakeClient()

    def not_ready(line: str) -> CmdResult:
        # As U4: nothing is sent and tx_offset is the next offset, where a device line lands.
        offset = len(fake.lines)
        fake.line("I (1203) wifi: some unrelated device line")
        return CmdResult(offset, "timeout", "shell not ready (state=booting)", [], None, 0.5)

    fake.on_cmd = not_ready
    body = '[[step]]\nkind = "cmd"\nsend = "version"\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "fail"
    assert result.steps[0]["detail"] == "timeout: `shell not ready (state=booting)`"


def test_cmd_error_fails_the_step_with_its_line(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = '[[step]]\nkind = "cmd"\nsend = "bogus"\nerror = ["not found"]\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    tx = fake.find("## tx bogus")
    assert result.result == "fail"
    assert result.steps[0]["detail"] == f"`## tx bogus` @ {tx.offset}; error: `command not found`"


def test_vars_fill_send_run_and_undo(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    cwd = json.dumps(str(tmp_path))
    body = f"""
[[step]]
kind = "cmd"
send = "log_level {{tag}} debug"

[[step]]
kind = "external"
run = "echo {{word}} > run.out"
undo = "echo password={{secret}} > undo.out"
cwd = {cwd}
"""
    variables = {"tag": "wifi", "word": "hello", "secret": "hunter2"}
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path, variables=variables)
    assert result.result == "pass", result.steps
    assert fake.ops("cmd")[0]["line"] == "log_level wifi debug"
    assert (tmp_path / "run.out").read_text() == "hello\n"
    assert (tmp_path / "undo.out").read_text() == "password=hunter2\n"
    # The profile's redaction covers external records; the report names unrendered templates.
    events_text = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "hunter2" not in events_text
    assert "echo password=*** > undo.out" in events_text
    assert result.steps[0]["name"] == "log_level {tag} debug"


@pytest.mark.parametrize(
    ("body", "variables", "message"),
    [
        ('[[step]]\nkind = "cmd"\nsend = "say {who}"\n', {}, "no --var for: who"),
        ('[[step]]\nkind = "cmd"\nsend = "say"\n', {"who": "x"}, "not used by any step: who"),
        ('[[step]]\nkind = "cmd"\ncommand = "nope"\n', {}, "step[1]: unknown command 'nope'"),
        ('[[step]]\nkind = "cmd"\nsend = "deep_sleep 5"\n', {}, "step[1]: denied: "),
        ('[[step]]\nkind = "external"\nrun = "echo {"\n', {}, "step[1]: unclosed '{'"),
    ],
)
def test_preflight_usage_errors_stop_before_the_unit_is_touched(
    tmp_path: Path, profile: Profile, body: str, variables: dict[str, str], message: str
) -> None:
    fake = FakeClient()
    with pytest.raises(errors.UsageError, match=re.escape(message)) as caught:
        run(fake, scenario(tmp_path, body), profile, tmp_path, variables=variables)
    assert caught.value.exit_code == 2
    assert fake.calls == []
    assert not (tmp_path / "run").exists()


# --- reset, flash, external, continue_on_fail ---------------------------------------------


def test_reset_step_fails_without_an_rst_line(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    fake.reset_answers = False
    body = '[[step]]\nkind = "reset"\nbootloader = true\ntimeout = "0.5s"\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert fake.ops("reset") == [{"bootloader": True, "wait_rst": 0.5}]
    asked = fake.find("## reset requested bootloader=true")
    assert result.result == "fail"
    assert result.steps[0]["detail"] == f"`{asked.text}` @ {asked.offset}; no `rst:` line in 0.5 s"


def test_reset_step_passes_on_its_rst_line(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    result, _ = run(fake, scenario(tmp_path, '[[step]]\nkind = "reset"\n'), profile, tmp_path)
    rst = next(line for line in fake.lines if line.text.startswith("rst:"))
    assert result.result == "pass"
    assert result.steps[0]["detail"] == f"`{rst.text}` @ {rst.offset}"
    assert fake.ops("reset") == [{"bootloader": False, "wait_rst": 5.0}]


def test_flash_step_resolves_files_and_takes_profile_defaults(
    tmp_path: Path, profile: Profile
) -> None:
    app = tmp_path / "app.bin"
    app.write_bytes(b"\xe9" + bytes(63))
    fake = FakeClient()
    files = f'[{{ addr = "0x10000", file = {json.dumps(str(app))} }}]'
    body = f'[[step]]\nkind = "flash"\nfiles = {files}\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass", result.steps
    sent = fake.ops("flash")[0]
    assert sent["files"] == [(0x10000, str(app.resolve()), "0x10000")]
    assert (sent["after"], sent["baud"]) == (profile.flash.after, profile.flash.baud)
    assert sent["flash_args"] == {"flash-mode": "keep", "flash-size": "keep", "flash-freq": "keep"}
    rst = next(line for line in fake.lines if line.text.startswith("rst:"))
    assert result.steps[0]["detail"] == f"0x10000; `{rst.text}` @ {rst.offset}"


def test_flash_step_after_overrides_the_profile(tmp_path: Path, profile: Profile) -> None:
    app = tmp_path / "app.bin"
    app.write_bytes(bytes(8))
    fake = FakeClient()
    files = f'[{{ addr = "0x10000", file = {json.dumps(str(app))} }}]'
    body = f'[[step]]\nkind = "flash"\nafter = "no-reset"\nfiles = {files}\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass"
    assert fake.ops("flash")[0]["after"] == "no-reset"


def test_a_missing_flash_input_is_a_step_error(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    missing = json.dumps(str(tmp_path / "no-build"))
    body = f'[[step]]\nkind = "flash"\nartifacts = {missing}\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert results_of(result.steps) == ["error"]
    assert "no such file or directory" in str(result.steps[0]["detail"])
    assert fake.ops("flash") == []


def test_external_timeout_kills_the_command(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = '[[step]]\nkind = "external"\nrun = "sleep 5; echo late"\ntimeout = "0.2s"\n'
    started = time.monotonic()
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert time.monotonic() - started < 1.0
    assert result.result == "fail"
    assert result.steps[0]["detail"] == "timed out after 0.2s"
    external = [e for e in events_of(result) if e["type"] == "external"]
    assert [(e["exit"], e["stdout_tail"]) for e in external] == [(None, "")]


def test_external_records_tail_and_redact_output(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    run_cmd = "for n in $(seq 1 30); do echo line $n; done; echo password=hunter2 >&2; exit 4"
    body = f'[[step]]\nkind = "external"\nrun = {json.dumps(run_cmd)}\nenv = {{ X = "1" }}\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.steps[0]["detail"] == "exit 4"
    (external,) = [e for e in events_of(result) if e["type"] == "external"]
    assert external["exit"] == 4
    assert external["stdout_tail"].splitlines() == [f"line {n}" for n in range(11, 31)]
    assert external["stderr_tail"] == "password=***"


def test_external_env_adds_to_the_environment(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    cwd = json.dumps(str(tmp_path))
    body = f"""
[[step]]
kind = "external"
run = 'echo "$BENCHCTL_EXAMPLE:$HOME" > env.out'
env = {{ BENCHCTL_EXAMPLE = "1" }}
cwd = {cwd}
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert result.result == "pass"
    assert (tmp_path / "env.out").read_text() == f"1:{os.environ['HOME']}\n"


def test_continue_on_fail_keeps_going_but_fails_the_run(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()
    body = """
[[step]]
kind = "wait"
pattern = "never"
timeout = "0.05s"
continue_on_fail = true

[[step]]
kind = "mark"
name = "still"
"""
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert results_of(result.steps) == ["fail", "pass"]
    assert result.result == "fail"
    assert str(result.steps[0]["detail"]).startswith("timeout after ")


def test_a_device_error_is_a_step_error(tmp_path: Path, profile: Profile) -> None:
    fake = FakeClient()

    def lost(args: dict[str, Any]) -> None:
        raise errors.DeviceError("port lost")

    fake.on_wait = lost
    body = '[[step]]\nkind = "wait"\npattern = "x"\ntimeout = "0.1s"\n[[step]]\nkind = "reset"\n'
    result, _ = run(fake, scenario(tmp_path, body), profile, tmp_path)
    assert results_of(result.steps) == ["error", "skip"]
    assert result.steps[0]["detail"] == "port lost"
    assert result.steps[1]["detail"] == "not run: step 1 errored"
