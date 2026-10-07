"""Command-line interface (design Section 7.1).

Each verb is one client call. `--json` prints one object on stdout; a verb whose result is a
failure (cmd not ok, wait timeout, run fail, validate errors) prints its result fields with
`ok: false` and an `error`, and exits with that error's code.
"""

from __future__ import annotations

import functools
import json
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import click

from benchctl import __version__, artifacts, paths
from benchctl import capture as capture_mod
from benchctl import daemon as daemon_mod
from benchctl import flash as flash_mod
from benchctl import monitor as monitor_mod
from benchctl import runner as runner_mod
from benchctl.client import ATTACH_TIMEOUT_S, Client, NotAttached, daemon_pid
from benchctl.defaults import LOG_LIMIT, REDACTED, RESET_WAIT_S, WAIT_TIMEOUT_S
from benchctl.durations import parse_duration
from benchctl.errors import (
    AssertionFailed,
    BenchctlError,
    DeviceError,
    TimeoutError_,
    UsageError,
)
from benchctl.idf import compile_reset_patterns
from benchctl.lease import read_lease
from benchctl.profile import FLASH_AFTER, Profile, load_profile
from benchctl.redact import Redactor
from benchctl.registry import Registry, Unit, list_ports, load_registry, match_port, matching_ports
from benchctl.scenario import load_scenario, validate_paths
from benchctl.types import Cursor, FlashResult, LogLine, ResetInfo, Status

HEARTBEAT_S = 10.0
DEFAULT_CAPTURE_S = 10.0
TAIL_TICK_S = 0.5
_JSON_FLAG = "--json"
_JSON_META = "benchctl.json"
_SERIAL_LOG = "serial.log"
# A serial.log line: `HH:MM:SS.mmm +SSSSS.mmm <text>` (Section 4.7).
_SERIAL_LINE = re.compile(r"(\S+) \+(\d+(?:\.\d+)?) ?(.*)")
_SLUG = re.compile(r"[^A-Za-z0-9]+")


class _Duration(click.ParamType[float]):
    name = "duration"

    def convert(
        self, value: Any, param: click.Parameter | None, ctx: click.Context | None
    ) -> float:
        if isinstance(value, float):
            return value
        try:
            return parse_duration(str(value))
        except UsageError as exc:
            self.fail(exc.message, param, ctx)


DURATION = _Duration()


# --- per-invocation state ----------------------------------------------------------------


@dataclass
class Opts:
    """The options every verb takes, and what is loaded from them on first use."""

    unit_arg: str | None
    json: bool
    profile_arg: str | None
    quiet: bool
    _registry: Registry | None = None
    _unit: Unit | None = None
    _profile: Profile | None = None
    _clients: list[Client] = field(default_factory=list)

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = load_registry(paths.config_dir())
        return self._registry

    @property
    def unit(self) -> Unit:
        if self._unit is None:
            self._unit = self.registry.unit(self.unit_arg)
        return self._unit

    @property
    def label(self) -> str | None:
        """The unit name for JSON output, without loading anything a verb did not need."""
        return self._unit.name if self._unit is not None else self.unit_arg

    def profile(self, preferred: str | None = None) -> Profile:
        """`--profile`, else `preferred` (a scenario's), else the unit's default; loaded once."""
        if self._profile is None:
            name = self.profile_arg or preferred or self.unit.profile
            self._profile = load_profile(name, Path.cwd(), paths.config_dir())
        return self._profile

    def client(
        self,
        *,
        spawn: bool,
        heartbeat: Heartbeat | None = None,
        attach_timeout: float = ATTACH_TIMEOUT_S,
        spawn_profile: str | None = None,
    ) -> Client:
        # Only a verb that loaded a profile names it in hello; resolving --profile just to
        # send it would refuse `detach` and `status` on a mismatch, the way out of one.
        loaded = None if self._profile is None else str(self._profile.path)
        client = Client(
            self.unit.name,
            spawn=spawn,
            attach_timeout=attach_timeout,
            on_progress=heartbeat,
            spawn_profile=spawn_profile or loaded or self.profile_arg,
            profile_path=loaded,
        )
        self._clients.append(client)
        return client

    def close(self) -> None:
        for client in self._clients:
            client.close()


class Heartbeat:
    """Prints `benchctl: waiting 20/600 s for … (offset now 850)` to stderr every
    HEARTBEAT_S while a request blocks; fed by the client's progress hook."""

    def __init__(self, opts: Opts, what: str, total: float | None = None) -> None:
        self._quiet = opts.quiet
        self._what = what
        self._total = total
        self._interval = HEARTBEAT_S
        self._start = time.monotonic()
        self._next = self._interval
        self._offset: Any = None

    def __call__(self, progress: dict[str, Any] | None) -> None:
        if progress is not None:
            self._offset = progress.get("offset", self._offset)
        elapsed = time.monotonic() - self._start
        if self._quiet or elapsed < self._next:
            return
        while self._next <= elapsed:
            self._next += self._interval
        total = "" if self._total is None else f"/{self._total:g}"
        now = "" if self._offset is None else f" (offset now {self._offset})"
        click.echo(f"benchctl: waiting {elapsed:.0f}{total} s {self._what}{now}", err=True)


# --- output ------------------------------------------------------------------------------


def _finish(
    opts: Opts, fields: dict[str, Any], text: str | None, error: BenchctlError | None = None
) -> int:
    if opts.json:
        payload: dict[str, Any] = {"ok": error is None, "unit": opts.label, **fields}
        if error is not None:
            payload["error"] = error.to_dict()
        click.echo(json.dumps(payload))
    else:
        if text:
            click.echo(text)
        if error is not None:
            click.echo(f"benchctl: {error.message}", err=True)
    return 0 if error is None else error.exit_code


def _print_error(error: BenchctlError, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps({"ok": False, "error": error.to_dict()}))
    else:
        click.echo(f"benchctl: {error.message}", err=True)


def _line_fields(line: LogLine) -> dict[str, Any]:
    return {"offset": line.offset, "t": line.t, "ts": line.ts, "text": line.text}


def _line_text(line: LogLine) -> str:
    return f"{line.offset} +{line.t:.3f} {line.text}"


def _reset_text(reset: ResetInfo) -> str:
    code = "?" if reset.code is None else f"{reset.code:#x}"
    kind = f"expected ({reset.cause})" if reset.expected else "unexpected"
    return f"{reset.offset} rst:{code} ({reset.reason}) {kind}"


def _status_fields(status: Status, profile: dict[str, Any] | None) -> dict[str, Any]:
    identity = status.identity
    return {
        "daemon": {
            "pid": status.pid,
            "since": status.attached_at,
            "port": status.port,
            "connected": status.connected,
            "baud": status.baud,
        },
        "profile": profile,
        "lease": status.lease or None,
        "identity": None
        if identity is None
        else {
            "fields": identity.fields,
            "verified": identity.verified,
            "mismatches": {name: list(pair) for name, pair in identity.mismatches.items()},
        },
        "last_reset": None if status.last_reset is None else asdict(status.last_reset),
        "offset": status.offset,
        "gate": {
            "state": status.gate.state,
            "since": status.gate.since,
            "method": status.gate.method,
        },
        "session_dir": status.session_dir,
    }


def _status_text(status: Status, profile: dict[str, Any] | None) -> str:
    link = "connected" if status.connected else "DISCONNECTED"
    lines = [
        f"{status.unit}: attached since {status.attached_at} (pid {status.pid}) "
        f"on {status.port or '?'}, {link}, {status.baud} baud",
    ]
    if profile is not None:
        lines.append(f"profile: {profile.get('name')} ({profile.get('path')})")
    lines.append(_lease_text(status.lease or None))
    if status.identity is not None:
        values = " ".join(f"{k}={v}" for k, v in status.identity.fields.items())
        verdict = "verified" if status.identity.verified else "UNVERIFIED"
        lines.append(f"identity: {values} ({verdict})")
    if status.last_reset is not None:
        lines.append(f"last reset: {_reset_text(status.last_reset)}")
    lines.append(f"offset {status.offset}, gate {status.gate.state}")
    lines.append(f"session: {status.session_dir}")
    return "\n".join(lines)


def _lease_text(lease: dict[str, Any] | None) -> str:
    if not lease:
        return "lease: free"
    return f"lease: {lease['owner']} since {lease['since']} (last seen {lease['last_seen']})"


# --- argument helpers --------------------------------------------------------------------


def _pairs(items: Iterable[str], option: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise UsageError(f"{option} {item!r}: expected key=value")
        if key in pairs:
            raise UsageError(f"{option} {key} given twice")
        pairs[key] = value
    return pairs


def _check_regex(option: str, pattern: str | None) -> None:
    if pattern is None:
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        raise UsageError(f"{option}: invalid regex {pattern!r}: {exc}") from None


def _cursor(option: str, text: str) -> int | str:
    """A cursor for the wire: offsets as ints, everything else for the daemon to resolve."""
    try:
        cursor = Cursor.parse(text)
    except UsageError as exc:
        raise UsageError(f"{option}: {exc.message}") from None
    return cursor.offset if cursor.kind == "offset" and cursor.offset is not None else text


def _split(text: str | None) -> list[str] | None:
    return None if text is None else [item for item in text.split(",") if item]


# --- the group -----------------------------------------------------------------------------


class _Cli(click.Group):
    """Renders BenchctlError (and click usage errors under `--json`) as Section 7.1 output,
    and turns a verb's returned status into the exit code."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta[_JSON_META] = _JSON_FLAG in args
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> Any:
        as_json = bool(ctx.meta.get(_JSON_META))
        try:
            status = super().invoke(ctx)
        except click.ClickException as exc:
            if not as_json:
                raise
            _print_error(UsageError(exc.format_message()), as_json=True)
            ctx.exit(exc.exit_code)
        except BenchctlError as exc:
            _print_error(exc, as_json)
            ctx.exit(exc.exit_code)
        if isinstance(status, int) and status:
            ctx.exit(status)
        return status


@click.group(cls=_Cli, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="benchctl")
def main() -> None:
    """Bench harness for ESP-IDF units: one daemon per unit holds the port; every verb is a
    short client call. See SKILL.md for the workflow."""


def _verb(*, json_flag: bool = True) -> Callable[[Callable[..., int]], Callable[..., int]]:
    """Add the options every verb takes and hand the verb an `Opts` first."""

    def decorate(func: Callable[..., int]) -> Callable[..., int]:
        @functools.wraps(func)
        def wrapper(
            *args: Any,
            unit: str | None,
            profile: str | None,
            quiet: bool,
            as_json: bool = False,
            **kwargs: Any,
        ) -> int:
            opts = Opts(unit, as_json, profile, quiet)
            try:
                return func(opts, *args, **kwargs)
            finally:
                opts.close()

        command: Callable[..., int] = wrapper
        if json_flag:
            command = click.option(
                "--json", "as_json", is_flag=True, help="Print one JSON object on stdout."
            )(command)
        command = click.option("--quiet", is_flag=True, help="No stderr heartbeat.")(command)
        command = click.option(
            "--profile", envvar="BENCHCTL_PROFILE", help="Profile name, dir or profile.toml."
        )(command)
        return click.option(
            "--unit", envvar="BENCHCTL_UNIT", help="Unit name (default: the only one)."
        )(command)

    return decorate


# --- verbs that never attach -------------------------------------------------------------


@main.command()
@_verb()
def units(opts: Opts) -> int:
    """List registered units, their port match and daemon."""
    ports = list_ports()
    selected = [opts.unit] if opts.unit_arg else list(opts.registry.units.values())
    rows: list[dict[str, Any]] = []
    for unit in selected:
        found = matching_ports(unit.match, ports)
        running = daemon_pid(unit.name) is not None
        lease = read_lease(paths.lease_path(unit.name)) if running else None
        rows.append(
            {
                "unit": unit.name,
                "profile": unit.profile,
                "port": found[0].device if len(found) == 1 else None,
                "candidates": len(found),
                "daemon": running,
                "lease": None
                if lease is None
                else {key: lease.get(key) for key in ("owner", "since", "last_seen")},
            }
        )
    text = "\n".join(
        f"{row['unit']}  profile={row['profile']}  port={row['port']}  "
        f"candidates={row['candidates']}  daemon={'yes' if row['daemon'] else 'no'}  "
        f"lease={row['lease']['owner'] if row['lease'] else '-'}"
        for row in rows
    )
    return _finish(opts, {"units": rows}, text or "no units registered")


@main.command()
@_verb()
def status(opts: Opts) -> int:
    """Daemon, lease, identity, last reset and gate; `daemon: null` when not attached."""
    client = opts.client(spawn=False)
    try:
        client.connect()
    except NotAttached:
        fields: dict[str, Any] = dict.fromkeys(
            (
                "daemon",
                "profile",
                "lease",
                "identity",
                "last_reset",
                "offset",
                "gate",
                "session_dir",
            )
        )
        return _finish(opts, fields, f"{opts.unit.name}: not attached")
    result = client.status()
    return _finish(
        opts, _status_fields(result, client.profile), _status_text(result, client.profile)
    )


@main.command()
@_verb()
def detach(opts: Opts) -> int:
    """Release the lease and stop the daemon."""
    client = opts.client(spawn=False)
    try:
        client.connect()
    except NotAttached:
        return _finish(opts, {"detached": False}, "not attached")
    client.detach()
    return _finish(opts, {"detached": True}, f"{opts.unit.name}: detached")


@main.command()
@_verb()
@click.argument("targets", nargs=-1, required=True, type=click.Path(path_type=Path))
def validate(opts: Opts, targets: tuple[Path, ...]) -> int:
    """Check profiles, units.toml and scenarios without hardware."""
    rows = validate_paths(list(targets))
    bad = [row for row in rows if not row["ok"]]
    lines: list[str] = []
    for row in rows:
        lines.append(f"{'ok  ' if row['ok'] else 'FAIL'} {row['kind']} {row['path']}")
        errors = row["errors"]
        if isinstance(errors, list):
            lines += [f"     {message}" for message in errors]
    error = UsageError(f"{len(bad)} of {len(rows)} file(s) invalid") if bad else None
    return _finish(opts, {"files": rows}, "\n".join(lines), error)


@main.command()
@_verb()
@click.option("--take", is_flag=True, help="Take the lease from its holder.")
@click.option("--release", is_flag=True, help="Give the lease up.")
def lease(opts: Opts, take: bool, release: bool) -> int:
    """Show, take or release the unit's lease."""
    if take and release:
        raise UsageError("give --take or --release, not both")
    client = opts.client(spawn=False)
    try:
        hello = client.connect()
    except NotAttached:
        return _finish(opts, {"lease": None}, "not attached")
    if take:
        current = client.lease("take")["lease"]
    elif release:
        current = client.lease("release")["lease"]
    else:
        current = hello.get("lease")
    return _finish(opts, {"lease": current}, _lease_text(current))


@main.command()
@_verb()
@click.option("--since", default="attach", show_default=True, help="Cursor.")
@click.option("--until", help="Cursor (exclusive).")
@click.option("--grep", help="Regex the lines must contain.")
@click.option("--limit", type=click.IntRange(min=1), default=LOG_LIMIT, show_default=True)
@click.option("--tail", "follow", is_flag=True, help="Follow new lines (text mode only).")
def log(
    opts: Opts, since: str, until: str | None, grep: str | None, limit: int, follow: bool
) -> int:
    """Stored lines `<offset> +<t> <text>`; without a daemon, the newest serial.log."""
    if follow and opts.json:
        raise UsageError("--tail is text mode only")
    since_c = _cursor("--since", since)
    until_c = None if until is None else _cursor("--until", until)
    _check_regex("--grep", grep)
    client = opts.client(spawn=False)
    try:
        client.connect()
    except NotAttached:
        if follow:
            raise UsageError(
                f"--tail needs a running daemon; {opts.unit.name} is not attached"
            ) from None
        return _log_from_disk(opts, since_c, until_c, grep, limit)
    if follow:
        return _follow(client, since_c)
    lines, nxt = client.log(since_c, until_c, grep, limit)
    return _finish(
        opts,
        {"lines": [_line_fields(line) for line in lines], "next": nxt},
        "\n".join(_line_text(line) for line in lines),
    )


def _follow(client: Client, since: int | str) -> int:
    feed = client.tail(since, events=False)
    try:
        while True:
            item = feed.get(TAIL_TICK_S)
            if isinstance(item, LogLine):
                click.echo(_line_text(item))
    except KeyboardInterrupt:
        return 0
    finally:
        feed.close()


def _read_serial_log(path: Path) -> list[LogLine]:
    lines: list[LogLine] = []
    for offset, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        match = _SERIAL_LINE.fullmatch(raw)
        ts, t, text = (match[1], float(match[2]), match[3]) if match else ("", 0.0, raw)
        lines.append(LogLine(offset, t, ts, text, text.startswith("## ")))
    return lines


def _offline_cursor(cursor: int | str, lines: list[LogLine]) -> int:
    if isinstance(cursor, int):
        return cursor
    if cursor == "attach":
        return 0
    if cursor == "boot":
        patterns = compile_reset_patterns()
        resets = [line.offset for line in lines if any(p.search(line.text) for p in patterns)]
        return resets[-1] if resets else 0
    raise UsageError(f"mark {cursor!r}: marks live in the daemon and died with it")


def _log_from_disk(
    opts: Opts, since: int | str, until: int | str | None, grep: str | None, limit: int
) -> int:
    logs = sorted(paths.sessions_dir(opts.unit.name).glob(f"*/{_SERIAL_LOG}"))
    if not logs:
        raise DeviceError(f"{opts.unit.name} is not attached and has no session logs")
    source = logs[-1]
    click.echo(f"benchctl: {opts.unit.name} is not attached; reading {source}", err=True)
    every = _read_serial_log(source)
    start = _offline_cursor(since, every)
    end = len(every) if until is None else _offline_cursor(until, every)
    lines = [line for line in every[start:end] if grep is None or re.search(grep, line.text)]
    hit_limit = len(lines) > limit
    lines = lines[:limit]
    nxt = lines[-1].offset + 1 if hit_limit else max(start, min(end, len(every)))
    fields = {
        "lines": [_line_fields(line) for line in lines],
        "next": nxt,
        "file": str(source),
    }
    return _finish(opts, fields, "\n".join(_line_text(line) for line in lines))


# --- verbs that attach -------------------------------------------------------------------


@main.command()
@_verb()
@click.option("--take", is_flag=True, help="Take the lease from its holder.")
@click.option("--no-verify", is_flag=True, help="Accept an identity mismatch.")
@click.option("--timeout", type=DURATION, default=ATTACH_TIMEOUT_S, help="Daemon start bound.")
def attach(opts: Opts, take: bool, no_verify: bool, timeout: float) -> int:
    """Start the unit's daemon (resets the unit once), verify identity, print status."""
    client = opts.client(
        spawn=True, attach_timeout=timeout, heartbeat=Heartbeat(opts, "for identity")
    )
    client.connect()
    if take:
        client.lease("take")
    client.identity(False, override=no_verify)
    result = client.status()
    return _finish(
        opts, _status_fields(result, client.profile), _status_text(result, client.profile)
    )


@main.command()
@_verb()
@click.argument("name", required=False)
def mark(opts: Opts, name: str | None) -> int:
    """Name the current offset (or print it); wait --since NAME starts there."""
    # Checked here too: the daemon would refuse it, but only after auto-attach reset the unit.
    if name is not None and Cursor.parse(name).kind != "mark":
        raise UsageError(f"mark name {name!r} is a cursor keyword or offset")
    offset = opts.client(spawn=True).mark(name)
    return _finish(opts, {"offset": offset, "name": name}, str(offset))


@main.command()
@_verb()
@click.argument("line", required=False)
@click.option("--name", "map_name", help="Command map entry from the profile.")
@click.option("--arg", "map_args", multiple=True, help="k=v for the map entry's {k}.")
@click.option("--expect", help="Regex: success when a reply line matches.")
@click.option("--error", "errors", multiple=True, help="Regex: failure when a line matches.")
@click.option("--capture", "capture_flag", is_flag=True, help="Capture the reply to the prompt.")
@click.option("--no-wait", is_flag=True, help="Send and return.")
@click.option("--reboots", is_flag=True, help="The command resets the unit.")
@click.option("--secret", is_flag=True, help="Log the sent line as <redacted>.")
@click.option("--force", is_flag=True, help="Skip the shell-ready gate.")
@click.option("--timeout", type=DURATION, help="Reply timeout (default console.timeout).")
def cmd(
    opts: Opts,
    line: str | None,
    map_name: str | None,
    map_args: tuple[str, ...],
    expect: str | None,
    errors: tuple[str, ...],
    capture_flag: bool,
    no_wait: bool,
    reboots: bool,
    secret: bool,
    force: bool,
    timeout: float | None,
) -> int:
    """Send LINE (or a map entry) to the shell and report the reply."""
    if (line is None) == (map_name is None):
        raise UsageError("give LINE or --name, not both")
    if map_args and map_name is None:
        raise UsageError("--arg needs --name")
    if sum((expect is not None, capture_flag, no_wait)) > 1:
        raise UsageError("--expect, --capture and --no-wait are exclusive")
    profile = opts.profile()
    entry = None if map_name is None else profile.command(map_name)
    if entry is not None:
        line = entry.render(_pairs(map_args, "--arg"))
    if line is None:  # excluded by the checks above; narrows the type
        raise UsageError("give LINE or --name")
    profile.check_allowed(line)
    explicit = expect is not None or capture_flag or no_wait
    if entry is not None and not explicit:
        expect = entry.expect_regex
    error = list(errors) or ([] if entry is None else entry.error_regexes)
    for pattern in (expect, *error):
        _check_regex("--expect/--error", pattern)
    capture: bool | None = None  # None: capture to the prompt when the profile has one
    if no_wait or expect is not None:
        capture = False
    elif capture_flag or (entry is not None and entry.capture):
        capture = True
    if timeout is None:
        timeout = (entry.timeout if entry else None) or profile.console.timeout
    shown = REDACTED if secret else repr(line)
    client = opts.client(
        spawn=True, heartbeat=Heartbeat(opts, f"for the reply to {shown}", timeout)
    )
    result = client.cmd(
        line,
        expect=expect,
        error=error,
        capture=capture,
        timeout=timeout,
        reboots=reboots or bool(entry and entry.reboots),
        secret=secret,
        force=force,
        source="cmd",
    )
    fields = {
        "offset": result.tx_offset,
        "status": result.status,
        "line": result.line,
        "groups": result.groups,
        "captured": result.captured,
        "duration": result.duration,
    }
    failure: BenchctlError | None = None
    if result.status == "error":
        failure = AssertionFailed(f"cmd error: {result.line}")
    elif result.status == "timeout":
        # A refusal (shell not ready, device busy) names itself in `line`; nothing was sent.
        if result.line is not None:
            failure = TimeoutError_(f"cmd timeout: {result.line}")
        else:
            failure = TimeoutError_(f"cmd timeout after {timeout:g} s: {shown}")
    text = result.captured if result.captured is not None else result.line
    return _finish(opts, fields, text, failure)


@main.command()
@_verb()
@click.argument("pattern", required=False)
@click.option("--since", required=True, help="Cursor: attach, boot, an offset, or a mark.")
@click.option("--timeout", type=DURATION, default=WAIT_TIMEOUT_S, show_default=True)
@click.option("--absent", help="Regex that must not appear (before PATTERN, or in --window).")
@click.option("--window", type=DURATION, help="Absence window (default: --timeout).")
@click.option("--order", help="Comma-separated regexes that must match in this order.")
def wait(
    opts: Opts,
    pattern: str | None,
    since: str,
    timeout: float,
    absent: str | None,
    window: float | None,
    order: str | None,
) -> int:
    """Block until PATTERN appears in the log after --since."""
    order_list = _split(order)
    if pattern is None and absent is None and not order_list:
        raise UsageError("give PATTERN, --absent or --order")
    if pattern is not None and order_list:
        raise UsageError("PATTERN and --order are exclusive")
    for option, value in (("PATTERN", pattern), ("--absent", absent)):
        _check_regex(option, value)
    for item in order_list or []:
        _check_regex("--order", item)
    since_c = _cursor("--since", since)
    what = f"for /{pattern or absent or order}/ since {since}"
    client = opts.client(spawn=True, heartbeat=Heartbeat(opts, what, timeout))
    result = client.wait(pattern, since_c, timeout, absent=absent, window=window, order=order_list)
    fields = asdict(result)
    failure: BenchctlError | None = None
    if result.result == "timeout":
        failure = TimeoutError_(f"wait timed out after {timeout:g} s {what}")
    elif result.result.endswith("_violated"):
        where = "" if result.offset is None else f" at {result.offset}: {result.line}"
        failure = AssertionFailed(f"wait {result.result}{where}")
    if result.line is not None:
        text = f"{result.offset} {result.line}"
    else:
        text = result.result + (f" {result.order}" if result.order else "")
    return _finish(opts, fields, text, failure)


@main.command()
@_verb()
@click.option("--bootloader", is_flag=True, help="Reset into the ROM download mode.")
@click.option("--timeout", type=DURATION, default=RESET_WAIT_S, show_default=True)
def reset(opts: Opts, bootloader: bool, timeout: float) -> int:
    """Reset the unit and report the rst: line."""
    offset, info = opts.client(spawn=True).reset(bootloader, timeout)
    fields = {"offset": offset, "reset": None if info is None else asdict(info)}
    text = _reset_text(info) if info else f"{offset} no rst: line within {timeout:g} s"
    return _finish(opts, fields, text)


@main.command()
@_verb()
@click.option("--reprobe", is_flag=True, help="Probe the identity fields again.")
def identity(opts: Opts, reprobe: bool) -> int:
    """Identity fields and whether they match units.toml's expect."""
    result = opts.client(spawn=True, heartbeat=Heartbeat(opts, "for identity")).identity(reprobe)
    fields = {
        "fields": result.fields,
        "verified": result.verified,
        "mismatches": {name: list(pair) for name, pair in result.mismatches.items()},
    }
    text = "\n".join(f"{k}={v}" for k, v in result.fields.items())
    return _finish(opts, fields, text + f"\n{'verified' if result.verified else 'UNVERIFIED'}")


@main.command()
@_verb()
@click.argument("specs", nargs=-1, required=True)
@click.option("--only", help="Comma-separated images: app,bootloader,partition-table,otadata.")
@click.option("--after", type=click.Choice(FLASH_AFTER), help="Default: the profile's.")
@click.option("--baud", type=click.IntRange(min=1), help="Default: the profile's.")
def flash(
    opts: Opts, specs: tuple[str, ...], only: str | None, after: str | None, baud: int | None
) -> int:
    """Flash a build dir, flasher_args.json or addr=file pairs (direct mode without a daemon)."""
    profile = opts.profile()
    chip = profile.flash.chip
    if chip is None:
        raise UsageError(f"profile {profile.name} sets no [profile] chip; flash needs it")
    files, flash_args = artifacts.resolve(list(specs), _split(only), profile.flash.overrides)
    after = after or profile.flash.after
    baud = baud or profile.flash.baud
    client = opts.client(spawn=False, heartbeat=Heartbeat(opts, "for flash"))
    result: FlashResult
    try:
        client.connect()
    except NotAttached:
        port = match_port(opts.unit.match, list_ports())
        try:
            result = flash_mod.flash_direct(
                port,
                files,
                chip=chip,
                before=profile.flash.before,
                after=after,
                baud=baud,
                flash_args=flash_args,
            )
        except DeviceError as exc:
            # A daemon that started after our connect attempt holds the port.
            if daemon_pid(opts.unit.name) is None:
                raise
            raise DeviceError(f"port busy: unit {opts.unit.name} daemon is running") from exc
    else:
        result = client.flash(files, after=after, baud=baud, flash_args=flash_args)
    text = f"flashed {', '.join(f.name for f in result.files)} at {result.baud} baud"
    text += f" in {result.duration:.1f} s"
    if result.app is not None:
        text += f"\napp {result.app.project} {result.app.version} elf {result.app.elf_sha256}"
    if result.reset is not None:
        text += f"\nreset: {_reset_text(result.reset)}"
    return _finish(opts, asdict(result), text)


@main.command()
@_verb()
@click.option("--reset", "do_reset", is_flag=True, help="Reset, then capture (required in v1).")
@click.option("--seconds", type=DURATION, default=DEFAULT_CAPTURE_S, show_default=True)
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), help="Also write here.")
def capture(opts: Opts, do_reset: bool, seconds: float, out: Path | None) -> int:
    """Reset the unit and collect --seconds of boot output (direct mode without a daemon)."""
    if not do_reset:
        raise UsageError("capture needs --reset in v1")
    client = opts.client(spawn=False, heartbeat=Heartbeat(opts, "for capture", seconds))
    reset_info: ResetInfo | None = None
    try:
        client.connect()
    except NotAttached:
        profile = opts.profile()
        port = match_port(opts.unit.match, list_ports())
        raw = capture_mod.capture_direct(port, profile.console.baud, seconds)
        # No daemon reader in direct mode, so this is where redaction happens (Section 4.8).
        redactor = Redactor([rule.pattern for rule in profile.redact])
        lines = [
            LogLine(line.offset, line.t, line.ts, redactor.apply(line.text), line.meta)
            for line in raw
        ]
    else:
        lines, reset_info = client.capture(seconds)
    text = "\n".join(_line_text(line) for line in lines)
    if out is not None:
        out.write_text(text + "\n" if text else "", encoding="utf-8")
    fields = {
        "lines": [_line_fields(line) for line in lines],
        "reset": None if reset_info is None else asdict(reset_info),
    }
    return _finish(opts, fields, text if out is None else f"{len(lines)} lines -> {out}")


@main.command()
@_verb(json_flag=False)
def monitor(opts: Opts) -> int:
    """Interactive: live lines; typed lines are sent; !reset resets; Ctrl-] exits."""
    profile = opts.profile()
    client = opts.client(spawn=True)
    since = int(client.connect()["offset"])
    return monitor_mod.run_monitor(client, profile, sys.stdin, since=since)


@main.command()
@_verb()
@click.argument("scenario_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--run-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Validate and print the budget only.")
@click.option("--skip-gate", is_flag=True, help="Skip the env-gate precondition.")
@click.option("--var", "variables", multiple=True, help="k=v for {k} in send/run.")
def run(
    opts: Opts,
    scenario_path: Path,
    run_dir: Path | None,
    dry_run: bool,
    skip_gate: bool,
    variables: tuple[str, ...],
) -> int:
    """Run a scenario; results land in a run dir."""
    scenario = load_scenario(scenario_path)
    profile = opts.profile(scenario.profile)
    unit = opts.unit
    if run_dir is None:
        slug = _SLUG.sub("-", scenario.name).strip("-").lower() or "scenario"
        run_dir = paths.runs_dir(unit.name) / f"{paths.timestamp()}-{slug}"
    # Connects on the first op, so a dry run never attaches (attaching resets the unit).
    client = opts.client(spawn=True, spawn_profile=str(profile.path))
    result = runner_mod.run_scenario(
        client,
        scenario,
        profile,
        run_dir=run_dir,
        variables=_pairs(variables, "--var"),
        skip_gate=skip_gate,
        dry_run=dry_run,
        prompt_out=sys.stderr,
    )
    envelope = ("ok", "unit")
    fields = {k: v for k, v in result.to_json(unit.name).items() if k not in envelope}
    lines = [
        f"{step.get('index')} {step.get('name') or step.get('kind')}: {step.get('result')}"
        for step in result.steps
    ]
    lines.append(f"{scenario.name}: {result.result} ({result.run_dir})")
    failure = AssertionFailed(f"scenario {result.result}") if result.exit_code else None
    return _finish(opts, fields, "\n".join(lines), failure)


@main.command(hidden=True)
@click.option("--unit", required=True)
@click.option("--profile", envvar="BENCHCTL_PROFILE")
def daemon(unit: str, profile: str | None) -> int:
    """Run the unit's daemon in the foreground (spawned by attach)."""
    return daemon_mod.main(unit, profile)
