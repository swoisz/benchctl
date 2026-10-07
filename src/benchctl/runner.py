"""Scenario execution (design Section 5): steps against a DeviceClient, `at` timing from the
last trigger, the runner's own cursors, undo, and the run directory (Section 7.6)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any, Literal, TextIO

from benchctl import artifacts, defaults
from benchctl.durations import format_duration
from benchctl.errors import BenchctlError, UsageError
from benchctl.events import EventWriter, external_record, human_record, run_record, step_record
from benchctl.logstore import format_line
from benchctl.profile import Command, Profile, placeholders, render
from benchctl.redact import Redactor
from benchctl.report import ReportRow, at_offset, code, render_results
from benchctl.scenario import Scenario, Step
from benchctl.types import DeviceClient, Event, Identity, LogLine, Offset, Subscription

# Section 7.8's timeouts for steps that set none; a `cmd` step falls back to its map entry's,
# then to console.timeout.
DEFAULT_TIMEOUTS: dict[str, float] = {
    "human": 600.0,
    "external": 60.0,
    "wait": 60.0,
    "reset": defaults.RESET_WAIT_S,
    "flash": 300.0,
    "mark": 0.0,
}
SCENARIO_COPY = "scenario.toml"
PROFILE_COPY = "profile.toml"
SERIAL_LOG = "serial.log"
EVENTS_FILE = "events.jsonl"
RESULTS_FILE = "results.md"
SUMMARY_FILE = "summary.json"
_TX_PREFIX = "## tx "  # the meta line a sent command leaves (Section 4.7)
_PASSING_WAITS = ("match", "absent_ok", "order_ok")
_FEED_POLL_S = 0.1
_FEED_SETTLE_S = 2.0
_FEED_JOIN_S = 2.0
_OUTPUT_TAIL_LINES = 20

RunOutcome = Literal["pass", "fail", "precondition_failed"]
StepResult = Literal["pass", "fail", "skip", "error"]


@dataclass(frozen=True, slots=True)
class RunResult:
    run_dir: Path
    result: RunOutcome
    budget: dict[str, float | None]  # {"declared": ..., "computed": ...}
    steps: list[dict[str, object]]

    @property
    def exit_code(self) -> int:
        """Section 7.1: `fail` and `precondition_failed` both exit 1."""
        return 0 if self.result == "pass" else 1

    def to_json(self, unit: str) -> dict[str, object]:
        """The `run --json` result fields under the Section 7.1 envelope (`ok`, `unit`);
        `summary.json` holds this object."""
        return {
            "ok": self.result == "pass",
            "unit": unit,
            "run_dir": str(self.run_dir),
            "result": self.result,
            "budget": dict(self.budget),
            "steps": list(self.steps),
        }


def step_timeout(step: Step, profile: Profile) -> float:
    """The step's timeout, else Section 7.8's default for its kind."""
    if step.timeout is not None:
        return step.timeout
    if step.kind == "cmd":
        command = profile.commands.get(step.command) if step.command is not None else None
        if command is not None and command.timeout is not None:
            return command.timeout
        return profile.console.timeout
    return DEFAULT_TIMEOUTS[step.kind]


def step_span(step: Step, profile: Profile) -> float:
    """The longest a step can run: its timeout, or a wait's declared window when longer
    (the daemon holds a matched wait until the window ends)."""
    timeout = step_timeout(step, profile)
    if step.kind == "wait" and step.window is not None:
        return max(timeout, step.window)
    return timeout


def compute_budget(scenario: Scenario, profile: Profile) -> float:
    """Worst-case seconds: each step's span plus its `at` offset."""
    return float(sum((step.at or 0.0) + step_span(step, profile) for step in scenario.steps))


def run_scenario(
    client: DeviceClient,
    scenario: Scenario,
    profile: Profile,
    *,
    run_dir: Path,
    variables: dict[str, str],
    skip_gate: bool,
    dry_run: bool,
    prompt_out: TextIO,
) -> RunResult:
    """Run `scenario` on the unit behind `client` and write `run_dir`.

    Raises UsageError before anything runs: a `--var` missing or unused, an unknown map
    command, a denied line. `dry_run` stops after those checks and the budget line."""
    plans = _prepare(scenario, profile, variables)
    computed = compute_budget(scenario, profile)
    budget: dict[str, float | None] = {"declared": scenario.budget, "computed": computed}
    _announce_budget(scenario.budget, computed, prompt_out)
    if dry_run:
        return RunResult(run_dir, "pass", budget, [])
    with _sigterm_raises():
        return _Run(client, scenario, profile, plans, run_dir, budget, skip_gate, prompt_out).run()


# --- before step 1 -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    """A step's templates, rendered: the `cmd` line, or an `external` step's run and undo."""

    line: str | None = None
    command: Command | None = None
    run: str | None = None
    undo: str | None = None


@contextlib.contextmanager
def _blame(scenario: Scenario, step: Step) -> Iterator[None]:
    try:
        yield
    except UsageError as exc:
        raise UsageError(
            f"{scenario.path}: step[{step.index}]: {exc.message}", data=exc.data
        ) from exc


def _fill(template: str, variables: dict[str, str]) -> str:
    # render() rejects unused args, so each template gets only its own placeholders.
    return render(template, {name: variables[name] for name in placeholders(template)})


def _prepare(scenario: Scenario, profile: Profile, variables: dict[str, str]) -> dict[int, _Plan]:
    needed: set[str] = set()
    for step in scenario.steps:
        with _blame(scenario, step):
            for template in (step.send, step.run, step.undo):
                if template is not None:
                    needed.update(placeholders(template))
    missing = sorted(needed - variables.keys())
    if missing:
        raise UsageError(f"{scenario.path}: no --var for: {', '.join(missing)}")
    unused = sorted(variables.keys() - needed)
    if unused:
        raise UsageError(f"{scenario.path}: --var not used by any step: {', '.join(unused)}")

    plans: dict[int, _Plan] = {}
    for step in scenario.steps:
        with _blame(scenario, step):
            if step.kind == "cmd":
                command = profile.command(step.command) if step.command is not None else None
                if command is not None:
                    line = command.render(step.args)
                else:
                    line = _fill(step.send or "", variables)
                profile.check_allowed(line)
                plans[step.index] = _Plan(line=line, command=command)
            elif step.kind == "external":
                undo = None if step.undo is None else _fill(step.undo, variables)
                plans[step.index] = _Plan(run=_fill(step.run or "", variables), undo=undo)
    return plans


def _announce_budget(declared: float | None, computed: float, out: TextIO) -> None:
    if declared is None:
        print(f"worst case: {format_duration(computed)} (computed)", file=out)
        return
    print(
        f"worst case: {format_duration(declared)} (declared) / "
        f"{format_duration(computed)} (computed)",
        file=out,
    )
    if declared < computed:
        print(
            f"warning: declared budget {format_duration(declared)} is less than the "
            f"computed worst case {format_duration(computed)}",
            file=out,
        )


# --- signals -----------------------------------------------------------------------------


class _Terminated(BaseException):
    """SIGTERM, raised in the main thread so it unwinds a step the way Ctrl-C does."""


_INTERRUPTS = (KeyboardInterrupt, _Terminated)


def _interrupt_text(exc: BaseException) -> str:
    return "interrupted by SIGTERM" if isinstance(exc, _Terminated) else "interrupted by SIGINT"


@contextlib.contextmanager
def _sigterm_raises() -> Iterator[None]:
    # Python installs handlers from the main thread only; elsewhere SIGTERM keeps its default.
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: FrameType | None) -> None:
        raise _Terminated

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)


# --- time and the event feed -------------------------------------------------------------


class _DeviceClock:
    """Maps the daemon's `t` (seconds since attach) onto this process's monotonic clock.

    A streamed item stamped at `t` arrives no earlier than `t`, so the smallest
    `arrival - t` seen is the clock offset plus the shortest delivery delay: a mapped time is
    never earlier than the moment it names, so `at` never fires early.
    """

    def __init__(self, attach_epoch: float | None) -> None:
        self._offset: float | None = None
        self._attach_epoch = attach_epoch
        self._created = time.monotonic()

    def observe(self, t: float) -> None:
        offset = time.monotonic() - t
        if self._offset is None or offset < self._offset:
            self._offset = offset

    def to_monotonic(self, t: float) -> float | None:
        return None if self._offset is None else self._offset + t

    def now_t(self) -> float:
        if self._offset is not None:
            return time.monotonic() - self._offset
        if self._attach_epoch is not None:
            return time.time() - self._attach_epoch
        # Neither a streamed item nor a parsable attach time: the run's own clock is the
        # closest stand-in.
        return time.monotonic() - self._created


def _attach_epoch(attached_at: str) -> float | None:
    try:
        return datetime.fromisoformat(attached_at).timestamp()
    except ValueError:
        return None


class _Feed:
    """Drains the run's tail subscription on its own thread, so a long blocking step cannot
    overflow the bounded queue, and passes daemon events through to the run's file."""

    def __init__(
        self, sub: Subscription, writer: EventWriter, clock: _DeviceClock, first: Offset
    ) -> None:
        self.resets: list[Event] = []
        self.error: str | None = None
        self._sub = sub
        self._writer = writer
        self._clock = clock
        self._first = first
        self._next = first  # offset after the last streamed line
        self._ended = False
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, name="benchctl-run-feed", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _drain(self) -> None:
        try:
            while not self._stop.is_set():
                item = self._sub.get(_FEED_POLL_S)
                if item is None:
                    continue
                if isinstance(item, LogLine):
                    self._clock.observe(item.t)
                    with self._cond:
                        self._next = max(self._next, item.offset + 1)
                        self._cond.notify_all()
                else:
                    self._event(item)
        except BenchctlError as exc:
            self.error = exc.message
        except (OSError, ValueError) as exc:
            # ValueError: the writer was closed under a feed that outlived stop()'s join.
            self.error = str(exc)
        finally:
            with self._cond:
                self._ended = True
                self._cond.notify_all()

    def _event(self, event: Event) -> None:
        t = event.get("t")
        if isinstance(t, int | float):
            self._clock.observe(float(t))
        offset = event.get("offset")
        # A replayed feed can reach back before the run; the run's file covers its own span.
        if isinstance(offset, int) and offset < self._first:
            return
        record = self._writer.passthrough(event)
        if record.get("type") == "reset":
            self.resets.append(record)

    def settle(self, end: Offset | None) -> None:
        """Wait, bounded, until every line before `end` has streamed, so the events that ride
        with them are written before the file closes."""
        if end is None:
            return
        deadline = time.monotonic() + _FEED_SETTLE_S
        with self._cond:
            while self._next < end and not self._ended:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(remaining)

    def stop(self) -> None:
        self._stop.set()
        self._sub.close()
        self._thread.join(_FEED_JOIN_S)


# --- external commands -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ShellResult:
    exit: int | None  # None: killed at its timeout, or interrupted
    stdout: str
    stderr: str
    duration: float


def _kill_group(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _shell(command: str, cwd: str | None, env: dict[str, str], timeout: float) -> _ShellResult:
    """Run `command` through the shell in its own process group, so a timeout or an
    interrupt kills everything it started, not only the shell. Raises OSError (bad cwd)."""
    started = time.monotonic()
    with subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env={**os.environ, **env},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
            exit_code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out, err = proc.communicate()
            exit_code = None
        except BaseException:
            _kill_group(proc)
            raise
    return _ShellResult(exit_code, out, err, time.monotonic() - started)


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-_OUTPUT_TAIL_LINES:])


def _exit_text(shell: _ShellResult, timeout: float) -> str:
    if shell.exit is None:
        return f"timed out after {format_duration(timeout)}"
    return f"exit {shell.exit}"


# --- the run -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Ctx:
    start: Offset  # this step's start: the `step` cursor
    prev: Offset  # the previous step's start: the `prev` cursor
    timeout: float


@dataclass(frozen=True, slots=True)
class _Outcome:
    result: Literal["pass", "fail", "error"]
    detail: str  # the evidence: results.md's column and the step's `detail`
    match_t: float | None = None  # a trigger's matched line, device `t`
    match_offset: Offset | None = None
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class _Undo:
    step: int
    command: str
    cwd: str | None
    env: dict[str, str]
    timeout: float


def _label(step: Step) -> str:
    """The step's name, else one built from its unrendered templates, so a `--var` value
    never reaches the report."""
    if step.name:
        return step.name
    if step.kind == "human":
        return step.prompt or "human"
    if step.kind == "external":
        return step.run or "external"
    if step.kind == "cmd":
        return step.send or step.command or "cmd"
    if step.kind == "wait":
        if step.pattern is not None:
            return step.pattern
        if step.order:
            return " → ".join(step.order)
        return f"no {step.absent}"
    if step.kind == "reset":
        return "reset to bootloader" if step.bootloader else "reset"
    if step.kind == "flash":
        return " ".join(step.flash_specs)
    return step.kind


def _gate_text(verdict: dict[str, Any]) -> str:
    parts: list[str] = []
    missing = verdict.get("missing")
    if isinstance(missing, list) and missing:
        parts.append("missing " + ", ".join(code(str(pattern)) for pattern in missing))
    hits = verdict.get("forbidden_hits")
    if isinstance(hits, list) and hits:
        found = [
            at_offset(str(hit.get("line")), hit.get("offset"))
            for hit in hits
            if isinstance(hit, dict)
        ]
        parts.append("forbidden " + ", ".join(found))
    return "; ".join(parts) or "not ok"


class _Run:
    """One execution: the step loop and everything it writes to the run dir."""

    def __init__(
        self,
        client: DeviceClient,
        scenario: Scenario,
        profile: Profile,
        plans: dict[int, _Plan],
        run_dir: Path,
        budget: dict[str, float | None],
        skip_gate: bool,
        out: TextIO,
    ) -> None:
        self.client = client
        self.scenario = scenario
        self.profile = profile
        self.plans = plans
        self.run_dir = run_dir
        self.budget = budget
        self.skip_gate = skip_gate
        self.out = out
        self.redactor = Redactor(rule.pattern for rule in profile.redact)
        self.unit = ""
        self.fw: str | None = None
        self.dev: str | None = None
        self.first: Offset = 0  # the run's first offset
        self.prev_start: Offset = 0
        self.start_mono = time.monotonic()  # `at` before any trigger counts from here
        self.trigger_mono: float | None = None
        self.trigger_offset: Offset | None = None
        self.failed = False
        self.starts: list[tuple[int, Offset]] = []  # (step index, start offset)
        self.undo: list[_Undo] = []
        self.rows: list[ReportRow] = []
        self.steps: list[dict[str, object]] = []
        self.notes: list[str] = []
        self.clock = _DeviceClock(None)
        self.writer: EventWriter | None = None
        self.handlers: dict[str, Callable[[Step, _Ctx], _Outcome]] = {
            "human": self._human,
            "external": self._external,
            "cmd": self._cmd,
            "wait": self._wait,
            "reset": self._reset,
            "flash": self._flash,
            "mark": self._mark,
        }

    # --- lifecycle ---------------------------------------------------------------------

    def run(self) -> RunResult:
        started = datetime.now()
        status = self.client.status()
        self.unit = status.unit
        # `status` is read-only, so on a run that attached it carries no identity yet: probe
        # here, before the first record, rather than lazily inside the first `mark`.
        self._learn(self.client.identity())
        self.first = self.prev_start = self.client.mark(None)
        # Only now: a unit that fails those three calls leaves no half-written run dir.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.scenario.path, self.run_dir / SCENARIO_COPY)
        shutil.copyfile(self.profile.path, self.run_dir / PROFILE_COPY)
        self.clock = _DeviceClock(_attach_epoch(status.attached_at))
        writer = EventWriter(
            self.run_dir / EVENTS_FILE,
            unit=self.unit,
            fw=self.fw,
            dev=self.dev,
            now_t=self.clock.now_t,
        )
        self.writer = writer
        try:
            result, feed = self._observed(writer)
        finally:
            writer.close()
        if feed.error is not None:
            self.notes.append(f"Event stream ended early: {feed.error}")
        return self._finish(result, started, feed.resets)

    def _observed(self, writer: EventWriter) -> tuple[RunOutcome, _Feed]:
        """The run proper, with the daemon's event stream flowing into the run's file, then
        serial.log; an interrupt in either fails the run before its `run` end record."""
        feed = _Feed(self.client.tail(self.first, events=True), writer, self.clock, self.first)
        feed.start()
        end: Offset | None = None
        try:
            writer.write(self._run_record("start", None))
            result = self._phases()
            try:
                end = self._offset_now()
                feed.settle(end)
            except _INTERRUPTS as exc:
                self.notes.append(
                    f"Events after the last step may be missing: {_interrupt_text(exc)}"
                )
                result = "fail" if result == "pass" else result
        finally:
            feed.stop()
        if not self._write_serial_log(end) and result == "pass":
            result = "fail"
        writer.write(self._run_record("end", result))
        return result, feed

    def _phases(self) -> RunOutcome:
        """Preconditions, steps and undo. An interrupt anywhere in them fails the run and
        skips what is left, and the undos still run."""
        try:
            problem = self._preconditions()
            if problem is not None:
                self._say(f"precondition failed: {problem}")
                self.notes.append(f"Precondition failed: {problem}")
                self._skip_rest(f"not run: precondition failed: {problem}")
                return "precondition_failed"
            self.start_mono = time.monotonic()
            self._steps()
        except _INTERRUPTS as exc:
            self.failed = True
            self._skip_rest(f"not run: {_interrupt_text(exc)}")
        finally:
            self._undo_all()
        return "fail" if self.failed else "pass"

    def _steps(self) -> None:
        stopped: str | None = None
        for step in self.scenario.steps:
            if stopped is not None:
                self._record(step, "skip", 0.0, None, f"not run: {stopped}")
                continue
            outcome = self._attempt(step)
            if outcome.result == "pass":
                continue
            self.failed = True
            if outcome.interrupted:
                stopped = outcome.detail
            elif not step.continue_on_fail:
                verb = "failed" if outcome.result == "fail" else "errored"
                stopped = f"step {step.index} {verb}"

    def _skip_rest(self, detail: str) -> None:
        recorded = {step["index"] for step in self.steps}
        for step in self.scenario.steps:
            if step.index not in recorded:
                self._record(step, "skip", 0.0, None, detail)

    def _finish(self, result: RunOutcome, started: datetime, resets: list[Event]) -> RunResult:
        expected = [self._step_of(r.get("offset")) for r in resets if r.get("expected")]
        unexpected = [self._step_of(r.get("offset")) for r in resets if not r.get("expected")]
        results = render_results(
            scenario=self.scenario.name,
            result=result,
            unit=self.unit,
            fw=self.fw,
            date=started.strftime("%Y-%m-%d"),
            rows=self.rows,
            expected_resets=expected,
            unexpected_resets=unexpected,
            notes=self.notes,
            run_dir=self.run_dir,
        )
        (self.run_dir / RESULTS_FILE).write_text(results, encoding="utf-8")
        run = RunResult(self.run_dir, result, self.budget, self.steps)
        summary = json.dumps(run.to_json(self.unit), indent=2, ensure_ascii=False)
        (self.run_dir / SUMMARY_FILE).write_text(summary + "\n", encoding="utf-8")
        return run

    def _write_serial_log(self, end: Offset | None) -> bool:
        """The run's slice of the session log; False when an interrupt cut it short."""
        with (self.run_dir / SERIAL_LOG).open("w", encoding="utf-8") as out:
            since = self.first
            while end is None or since < end:
                try:
                    lines, after = self.client.log(
                        since=since, until=None, grep=None, limit=defaults.LOG_LIMIT
                    )
                except BenchctlError as exc:
                    self.notes.append(f"serial.log is incomplete: {exc.message}")
                    return True
                except _INTERRUPTS as exc:
                    self.notes.append(f"serial.log is incomplete: {_interrupt_text(exc)}")
                    return False
                for line in lines:
                    if end is None or line.offset < end:
                        out.write(format_line(line) + "\n")
                if not lines or after <= since:
                    break
                since = after
        return True

    # --- preconditions -----------------------------------------------------------------

    def _preconditions(self) -> str | None:
        """Why the run cannot start, or None. An interrupt propagates: it fails the run."""
        try:
            return self._check_preconditions()
        except BenchctlError as exc:
            return exc.message

    def _check_preconditions(self) -> str | None:
        pre = self.scenario.preconditions
        if pre.fw_version is not None:
            identity = self.client.identity()
            self._learn(identity)
            fw = identity.fields.get("fw_version")
            if fw is None:
                return "fw_version: the unit's identity has no fw_version field"
            if re.search(pre.fw_version, fw) is None:
                return f"fw_version {fw!r} does not match {pre.fw_version!r}"
        if pre.env_gate:
            gate = self.profile.env_gate
            if self.skip_gate:
                self._say("env gate skipped (--skip-gate)")
            elif gate is None:
                return f"env_gate: profile {self.profile.name!r} has no [env_gate] table"
            else:
                verdict = self.client.gate("boot", gate.timeout)
                if not verdict.get("ok"):
                    return f"env gate: {_gate_text(verdict)}"
        return None

    # --- one step ----------------------------------------------------------------------

    def _attempt(self, step: Step) -> _Outcome:
        started = time.monotonic()
        try:
            self._sleep_until(step)
            started = time.monotonic()
            start = self.client.mark(None)
            ctx = _Ctx(start=start, prev=self.prev_start, timeout=step_timeout(step, self.profile))
            self.prev_start = start
            self.starts.append((step.index, start))
            if step.expect_reset:
                # Armed for the whole step and a reset window past it: a human or external
                # step may cause the reset long after it starts, or just after it ends.
                span = step_span(step, self.profile)
                window = span + self.profile.boot.expected_reset_window
                self.client.arm_reset(f"step:{step.index}", window)
            outcome = self.handlers[step.kind](step, ctx)
        except _INTERRUPTS as exc:
            outcome = _Outcome("error", _interrupt_text(exc), interrupted=True)
        except BenchctlError as exc:
            outcome = _Outcome("error", exc.message)
        except OSError as exc:
            outcome = _Outcome("error", str(exc))
        ended = time.monotonic()
        if step.trigger and outcome.result == "pass" and outcome.match_offset is not None:
            self.trigger_offset = outcome.match_offset
            mapped = None if outcome.match_t is None else self.clock.to_monotonic(outcome.match_t)
            # The step saw the line by `ended` at the latest; the mapping only pulls earlier.
            self.trigger_mono = ended if mapped is None else min(ended, mapped)
        self._record(step, outcome.result, ended - started, ended - self.start_mono, outcome.detail)
        return outcome

    def _sleep_until(self, step: Step) -> None:
        if step.at is None:
            return
        anchor = self.start_mono if self.trigger_mono is None else self.trigger_mono
        delay = anchor + step.at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _since(self, since: str, ctx: _Ctx) -> Offset | str:
        """Runner cursors become offsets here; `boot`, `attach` and marks are the daemon's."""
        if since == "prev":
            return ctx.prev
        if since == "step":
            return ctx.start
        if since == "trigger":
            return self.first if self.trigger_offset is None else self.trigger_offset
        return since

    def _human(self, step: Step, ctx: _Ctx) -> _Outcome:
        confirm = step.confirm or ""
        self._say(
            f"\a[step {step.index}] {step.prompt}\n"
            f"  (waiting up to {format_duration(ctx.timeout)} for /{confirm}/)"
        )
        found = self.client.wait(
            confirm, ctx.start, ctx.timeout, absent=None, window=None, order=None
        )
        confirmed = found.result == "match"
        self._write(
            human_record(
                prompt=step.prompt or "",
                result="confirmed" if confirmed else "timeout",
                line=found.line,
            ),
            offset=found.offset,
        )
        if not confirmed:
            return _Outcome("fail", f"not confirmed within {format_duration(ctx.timeout)}")
        self._say(f"[step {step.index}] confirmed: {found.line}")
        return _Outcome(
            "pass",
            at_offset(found.line, found.offset),
            match_t=found.t,
            match_offset=found.offset,
        )

    def _wait(self, step: Step, ctx: _Ctx) -> _Outcome:
        # An undeclared window stays None: a window holds a matched wait open until it ends.
        found = self.client.wait(
            step.pattern,
            self._since(step.since, ctx),
            ctx.timeout,
            absent=step.absent,
            window=step.window,
            order=step.order,
        )
        parts: list[str] = []
        if found.line is not None or found.offset is not None:
            parts.append(at_offset(found.line, found.offset))
        if found.result not in _PASSING_WAITS:
            parts.append(f"{found.result} after {found.duration:.1f} s")
            return _Outcome("fail", "; ".join(parts))
        if step.absent is not None:
            absent = code(step.absent)
            if step.window is not None:
                parts.append(f"no {absent} in {step.window:g} s")
            elif step.pattern is not None or step.order:
                parts.append(f"no {absent} before it")
            else:
                parts.append(f"no {absent} in {ctx.timeout:g} s")
        return _Outcome("pass", "; ".join(parts), match_t=found.t, match_offset=found.offset)

    def _cmd(self, step: Step, ctx: _Ctx) -> _Outcome:
        plan = self.plans[step.index]
        command = plan.command
        # The reply mode overrides as one unit: a step's `expect` must not inherit the map's
        # `capture = true`, which contradicts it.
        expect: str | None = None
        capture: bool | None = None
        if step.expect is not None or step.capture is not None:
            expect, capture = step.expect, step.capture
        elif command is not None:
            expect, capture = command.expect_regex, (True if command.capture else None)
        error = step.error or ([] if command is None else command.error_regexes)
        reply = self.client.cmd(
            plan.line or "",
            expect=expect,
            error=error,
            capture=capture,
            timeout=ctx.timeout,
            # The session's own `cmd:` arm would replace the step's `step:N` arm.
            reboots=command is not None and command.reboots and not step.expect_reset,
            secret=step.secret,
            force=False,
            source="runner",
        )
        parts: list[str] = []
        tx = self._stored(reply.tx_offset)
        # A shell that is not ready gets nothing sent, and tx_offset is then just the next
        # offset: whatever device line lands there is not this step's evidence.
        if tx is not None and tx.meta and tx.text.startswith(_TX_PREFIX):
            parts.append(at_offset(tx.text, tx.offset))
        if reply.status == "ok":
            return _Outcome("pass", "; ".join(parts))
        parts.append(reply.status if reply.line is None else f"{reply.status}: {code(reply.line)}")
        return _Outcome("fail", "; ".join(parts))

    def _external(self, step: Step, ctx: _Ctx) -> _Outcome:
        plan = self.plans[step.index]
        shell = self._shell("run", plan.run or "", step.cwd, step.env, ctx.timeout)
        if shell.exit != 0:
            return _Outcome("fail", _exit_text(shell, ctx.timeout))
        if plan.undo is not None:
            self.undo.append(_Undo(step.index, plan.undo, step.cwd, step.env, ctx.timeout))
        return _Outcome("pass", "exit 0")

    def _reset(self, step: Step, ctx: _Ctx) -> _Outcome:
        requested, info = self.client.reset(step.bootloader, wait_rst=ctx.timeout)
        if info is None:
            asked = at_offset(self._line_at(requested), requested)
            return _Outcome("fail", f"{asked}; no {code('rst:')} line in {ctx.timeout:g} s")
        return _Outcome("pass", at_offset(self._line_at(info.offset), info.offset))

    def _flash(self, step: Step, ctx: _Ctx) -> _Outcome:
        files, flash_args = artifacts.resolve(
            step.flash_specs, step.only, self.profile.flash.overrides
        )
        flashed = self.client.flash(
            files,
            after=step.after or self.profile.flash.after,
            baud=self.profile.flash.baud,
            flash_args=flash_args,
        )
        parts = [", ".join(file.name for file in flashed.files)]
        if flashed.app is not None:
            parts.append(f"app {flashed.app.version}")
        if flashed.reset is not None:
            parts.append(at_offset(self._line_at(flashed.reset.offset), flashed.reset.offset))
        return _Outcome("pass", "; ".join(parts))

    def _mark(self, step: Step, ctx: _Ctx) -> _Outcome:
        offset = self.client.mark(step.name)
        return _Outcome("pass", at_offset(self._line_at(offset), offset))

    # --- undo --------------------------------------------------------------------------

    def _undo_all(self) -> None:
        while self.undo:
            entry = self.undo.pop()
            # A further Ctrl-C fails only the undo it lands in; the rest still run.
            try:
                shell = self._shell("undo", entry.command, entry.cwd, entry.env, entry.timeout)
                if shell.exit != 0:
                    self._undo_failed(entry, _exit_text(shell, entry.timeout))
            except _INTERRUPTS as exc:
                self._undo_failed(entry, _interrupt_text(exc))
            except OSError as exc:
                self._undo_failed(entry, str(exc))

    def _undo_failed(self, entry: _Undo, reason: str) -> None:
        self.failed = True
        command = code(self.redactor.apply(entry.command))
        self.notes.append(f"Undo of step {entry.step} failed ({reason}): {command}")

    # --- helpers -----------------------------------------------------------------------

    def _shell(
        self, phase: str, command: str, cwd: str | None, env: dict[str, str], timeout: float
    ) -> _ShellResult:
        """Run an external command and record it, interrupted or not."""
        started = time.monotonic()
        try:
            result = _shell(command, cwd, env, timeout)
        except _INTERRUPTS:
            self._write_external(phase, command, _ShellResult(None, "", "", 0.0), started)
            raise
        self._write_external(phase, command, result, started)
        return result

    def _write_external(
        self, phase: str, command: str, result: _ShellResult, started: float
    ) -> None:
        # External output never passed the daemon's redactor, so the profile's patterns are
        # applied here; a rendered command can carry a --var secret.
        self._write(
            external_record(
                phase=phase,
                cmd=self.redactor.apply(command),
                exit=result.exit,
                duration=time.monotonic() - started,
                stdout_tail=self.redactor.apply(_tail(result.stdout)),
                stderr_tail=self.redactor.apply(_tail(result.stderr)),
            )
        )

    def _stored(self, offset: Offset) -> LogLine | None:
        """The stored, redacted line at `offset`, for evidence; None when unavailable."""
        try:
            lines, _ = self.client.log(since=offset, until=None, grep=None, limit=1)
        except BenchctlError:
            return None
        return lines[0] if lines and lines[0].offset == offset else None

    def _line_at(self, offset: Offset) -> str | None:
        stored = self._stored(offset)
        return None if stored is None else stored.text

    def _offset_now(self) -> Offset | None:
        try:
            return self.client.mark(None)
        except BenchctlError:
            return None

    def _step_of(self, offset: object) -> int | None:
        """The step a log offset fell in: the last one started at or before it."""
        if not isinstance(offset, int):
            return None
        owner: int | None = None
        for index, start in self.starts:
            if start <= offset:
                owner = index
        return owner

    def _learn(self, identity: Identity | None) -> None:
        if identity is None:
            return
        fields = identity.fields
        # units.toml's id_field is the daemon's to apply; its events retag the file once
        # they flow, so mac-then-cert_cn (Section 4.12's default) only covers the start.
        self.fw = fields.get("fw_version") or self.fw
        self.dev = fields.get("mac") or fields.get("cert_cn") or self.dev
        if self.writer is not None:
            self.writer.tag(self.fw, self.dev)

    def _record(
        self, step: Step, result: StepResult, duration: float, t: float | None, detail: str
    ) -> None:
        name = _label(step)
        self.steps.append(
            {
                "index": step.index,
                "name": name,
                "kind": step.kind,
                "result": result,
                "duration": round(duration, 3),
                "detail": detail,
            }
        )
        self.rows.append(ReportRow(step.index, name, step.kind, result, t, detail))
        self._write(
            step_record(
                index=step.index,
                name=name,
                kind=step.kind,
                result=result,
                duration=duration,
                detail=detail,
            )
        )
        if result != "skip":
            total = len(self.scenario.steps)
            self._say(f"[{step.index}/{total}] {result} {step.kind}: {name} ({duration:.1f}s)")

    def _run_record(self, phase: str, result: RunOutcome | None) -> Event:
        return run_record(
            phase=phase,
            scenario=self.scenario.name,
            run_dir=str(self.run_dir),
            result=result,
            budget=self.budget,
        )

    def _write(self, record: Event, *, offset: Offset | None = None) -> None:
        if self.writer is not None:
            self.writer.write(record, offset=offset)

    def _say(self, text: str) -> None:
        print(text, file=self.out, flush=True)
