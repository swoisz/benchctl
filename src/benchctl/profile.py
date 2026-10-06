"""Profile TOML: load, validate, look up (design Section 7.3).

`Checker` and `Table` are the schema helpers `registry.py` and `scenario.py` share. Every
getter records `<file>: <toml path>: <message>` and returns a placeholder, so one pass
reports every problem in a file; `Checker.done` then raises them together. Array elements
are numbered from 1 (`deny[2].pattern`), as scenario steps are.
"""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal, cast

from benchctl import idf
from benchctl.errors import UsageError
from benchctl.redact import Redactor

PROFILE_FILE = "profile.toml"
LOCAL_DIR = ".benchctl"
# esptool 5's --before choices; --after is narrowed to the two the daemon supports (4.10).
FLASH_BEFORE = ("default-reset", "usb-reset", "no-reset", "no-reset-no-sync")
FLASH_AFTER = ("hard-reset", "no-reset")
IDENTITY_SOURCES = ("cmd", "boot")
# A profile name becomes a directory under <config>/profiles.
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

_TOP_KEYS = (
    "profile",
    "console",
    "boot",
    "identity",
    "commands",
    "deny",
    "redact",
    "flash",
    "env_gate",
)
_PROFILE_KEYS = ("name", "chip")
_CONSOLE_KEYS = ("baud", "line_ending", "prompt", "timeout", "tx_chunk", "tx_chunk_delay_ms")
_BOOT_KEYS = (
    "ready",
    "settle",
    "ready_timeout",
    "probe",
    "probe_interval",
    "reset_patterns",
    "sha_pattern",
    "expected_reset_window",
    "ms_regression",
)
_IDENTITY_FIELD_KEYS = ("name", "source", "send", "regex", "filter", "timeout")
_COMMAND_KEYS = ("description", "send", "capture", "expect", "error", "timeout", "reboots")
_DENY_KEYS = ("pattern", "reason")
_REDACT_KEYS = ("name", "pattern")
_FLASH_KEYS = ("baud", "before", "after", "flash_mode", "flash_size", "flash_freq")
_ENV_GATE_KEYS = ("timeout", "require", "forbid")


class ConfigError(UsageError):
    """Every problem found in one config file, one `<file>: <toml path>: <message>` each."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors), data={"errors": list(errors)})
        self.errors = list(errors)


class Checker:
    """Collects one file's errors."""

    def __init__(self, file: Path | str) -> None:
        self.file = str(file)
        self.errors: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{self.file}: {path}: {message}" if path else f"{self.file}: {message}")

    def done(self) -> None:
        if self.errors:
            raise ConfigError(self.errors)


def _type_name(value: object) -> str:
    # Order matters: bool is an int subclass, datetime a date subclass.
    for kind, name in (
        (bool, "a boolean"),
        (int, "an integer"),
        (float, "a float"),
        (str, "a string"),
        (list, "an array"),
        (dict, "a table"),
        (datetime, "a date-time"),
        (date, "a date"),
        (time, "a time"),
    ):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class Table:
    """One TOML table under validation. `known=None` accepts any key (a map of names)."""

    def __init__(
        self,
        checker: Checker,
        data: Mapping[str, Any],
        path: str = "",
        known: Iterable[str] | None = None,
    ) -> None:
        self.checker = checker
        self.data = data
        self.path = path
        if known is not None:
            allowed = set(known)
            for key in data:
                if key not in allowed:
                    checker.error(self.at(key), "unknown key")

    def at(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def error(self, key: str | None, message: str) -> None:
        self.checker.error(self.path if key is None else self.at(key), message)

    def has(self, key: str) -> bool:
        return key in self.data

    def _wrong(self, key: str, expected: str) -> None:
        self.error(key, f"expected {expected}, got {_type_name(self.data[key])}")

    # --- scalars ---------------------------------------------------------------------

    def opt_text(self, key: str, *, nonempty: bool = False) -> str | None:
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            self._wrong(key, "a string")
            return None
        if nonempty and not value:
            self.error(key, "must not be empty")
            return None
        return value

    def text(self, key: str, default: str) -> str:
        value = self.opt_text(key)
        return default if value is None else value

    def req_text(self, key: str) -> str:
        if key not in self.data:
            self.error(key, "required")
            return ""
        return self.opt_text(key, nonempty=True) or ""

    def opt_number(self, key: str, *, positive: bool, unit: str = "a number") -> float | None:
        value = self.data.get(key)
        if value is None:
            return None
        if not _is_number(value):
            self._wrong(key, unit)
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0 or (positive and number == 0):
            self.error(key, "must be greater than 0" if positive else "must not be negative")
            return None
        return number

    def number(self, key: str, default: float, *, positive: bool = True) -> float:
        value = self.opt_number(key, positive=positive)
        return default if value is None else value

    def opt_seconds(self, key: str, *, positive: bool = True) -> float | None:
        """Profile durations: seconds as a number (duration strings are for scenarios)."""
        return self.opt_number(key, positive=positive, unit="seconds as a number")

    def seconds(self, key: str, default: float, *, positive: bool = True) -> float:
        value = self.opt_seconds(key, positive=positive)
        return default if value is None else value

    def integer(self, key: str, default: int) -> int:
        """A positive integer."""
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self._wrong(key, "an integer")
            return default
        if value <= 0:
            self.error(key, "must be greater than 0")
            return default
        return value

    def opt_flag(self, key: str) -> bool | None:
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, bool):
            self._wrong(key, "true or false")
            return None
        return value

    def flag(self, key: str, default: bool) -> bool:
        value = self.opt_flag(key)
        return default if value is None else value

    def choice(self, key: str, choices: tuple[str, ...], default: str) -> str:
        value = self.opt_text(key)
        if value is None:
            return default
        if value not in choices:
            self.error(key, f"must be one of {', '.join(choices)}; got {value!r}")
            return default
        return value

    # --- regexes ---------------------------------------------------------------------

    def _compile(self, path: str, pattern: str) -> re.Pattern[str] | None:
        try:
            return re.compile(pattern)
        except re.error as exc:
            self.checker.error(path, f"invalid regex {pattern!r}: {exc}")
            return None

    def opt_regex(self, key: str) -> re.Pattern[str] | None:
        pattern = self.opt_text(key, nonempty=True)
        return None if pattern is None else self._compile(self.at(key), pattern)

    def req_regex(self, key: str) -> re.Pattern[str] | None:
        if key not in self.data:
            self.error(key, "required")
            return None
        return self.opt_regex(key)

    def regex_list(self, key: str) -> list[re.Pattern[str]]:
        compiled: list[re.Pattern[str]] = []
        for index, pattern in enumerate(self.text_list(key), 1):
            path = f"{self.at(key)}[{index}]"
            if not pattern:
                self.checker.error(path, "must not be empty: it would match every line")
                continue
            regex = self._compile(path, pattern)
            if regex is not None:
                compiled.append(regex)
        return compiled

    # --- collections -----------------------------------------------------------------

    def opt_text_list(self, key: str) -> list[str] | None:
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self._wrong(key, "an array of strings")
            return None
        return list(cast(list[str], value))

    def text_list(self, key: str) -> list[str]:
        return self.opt_text_list(key) or []

    def text_map(self, key: str, *, scalars: bool = False) -> dict[str, str]:
        """A table of strings. `scalars` also takes integers and floats, as their text."""
        sub = self.table(key, None)
        result: dict[str, str] = {}
        for name, value in sub.data.items():
            if isinstance(value, str):
                result[name] = value
            elif scalars and _is_number(value):
                result[name] = str(value)
            else:
                sub._wrong(name, "a string")
        return result

    def opt_table(self, key: str, known: Iterable[str] | None) -> Table | None:
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            self._wrong(key, "a table")
            return None
        return Table(self.checker, value, self.at(key), known)

    def table(self, key: str, known: Iterable[str] | None, *, required: bool = False) -> Table:
        """The sub-table, or an empty one when absent or mistyped (the error is recorded)."""
        if required and key not in self.data:
            self.error(key, "required")
        sub = self.opt_table(key, known)
        return Table(self.checker, {}, self.at(key)) if sub is None else sub

    def tables(self, key: str, known: Iterable[str] | None) -> list[Table]:
        """An array of tables (`[[key]]`), each path-qualified as `key[n]`."""
        value = self.data.get(key)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            self._wrong(key, "an array of tables")
            return []
        return [
            Table(self.checker, item, f"{self.at(key)}[{index}]", known)
            for index, item in enumerate(value, 1)
        ]


def loads_toml(text: str, file: Path | str) -> dict[str, Any]:
    """Parse TOML text; a syntax error is a ConfigError naming `file`."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{file}: {exc}"]) from exc


def load_toml(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file; unreadable, non-UTF-8 or malformed is a ConfigError."""
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ConfigError([f"{path}: {exc.strerror or exc}"]) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError([f"{path}: not UTF-8: {exc}"]) from exc
    return loads_toml(text, path)


# --- templates ---------------------------------------------------------------------------


def placeholders(template: str) -> list[str]:
    """`{name}` placeholders in order of appearance. Raises UsageError on an unclosed `{`."""
    names: list[str] = []
    rest = template
    while (start := rest.find("{")) >= 0:
        end = rest.find("}", start + 1)
        if end < 0:
            raise UsageError(f"unclosed '{{' in template: {template}")
        names.append(rest[start + 1 : end])
        rest = rest[end + 1 :]
    return names


def render(template: str, args: dict[str, str]) -> str:
    """Fill `{name}` placeholders from `args` (hail's render). A missing or unused argument is
    a UsageError; literal braces have no escape, as device commands do not use them."""
    out: list[str] = []
    used: set[str] = set()
    rest = template
    while (start := rest.find("{")) >= 0:
        out.append(rest[:start])
        end = rest.find("}", start + 1)
        if end < 0:
            raise UsageError(f"unclosed '{{' in template: {template}")
        key = rest[start + 1 : end]
        if key not in args:
            raise UsageError(f"missing argument '{key}=' for: {template}")
        out.append(args[key])
        used.add(key)
        rest = rest[end + 1 :]
    out.append(rest)
    unused = sorted(set(args) - used)
    if unused:
        raise UsageError(f"unknown argument(s): {', '.join(unused)}")
    return "".join(out)


# --- the profile -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    baud: int = 115200
    line_ending: str = "\r\n"
    prompt: str | None = None
    # 7.3 gives no default for the reply timeout; hail's is used.
    timeout: float = 5.0
    tx_chunk: int = 64
    tx_chunk_delay_ms: float = 10.0


@dataclass(frozen=True, slots=True)
class BootConfig:
    ready: str | None = None  # resolved at load: the profile's, else re.escape(console.prompt)
    settle: float = 2.5
    ready_timeout: float = 20.0
    probe: str = ""
    probe_interval: float = 1.0
    reset_patterns: list[str] = field(default_factory=list)  # tried after idf's default
    sha_pattern: str = idf.DEFAULT_SHA_PATTERN
    expected_reset_window: float = 10.0
    ms_regression: int = idf.DEFAULT_MS_REGRESSION


@dataclass(frozen=True, slots=True)
class IdentityField:
    name: str
    source: Literal["cmd", "boot"]
    regex: str  # group 1 is the value
    send: str | None = None
    filter: str | None = None
    timeout: float | None = None


@dataclass(frozen=True, slots=True)
class Command:
    """One `[commands.<name>]` entry: hail's command map plus `timeout` and `reboots`."""

    name: str
    send: str
    description: str | None = None
    expect: str | None = None  # success: a reply line starting with this
    error: list[str] = field(default_factory=list)  # failure: a reply line containing one
    capture: bool = False
    timeout: float | None = None
    reboots: bool = False

    def render(self, args: dict[str, str]) -> str:
        return render(self.send, args)

    @property
    def expect_regex(self) -> str | None:
        """`expect` as the regex `Session.cmd` takes, keeping hail's starts-with semantics."""
        return None if self.expect is None else r"^\s*" + re.escape(self.expect)

    @property
    def error_regexes(self) -> list[str]:
        """`error` as regexes, keeping hail's contains semantics."""
        return [re.escape(marker) for marker in self.error]


@dataclass(frozen=True, slots=True)
class DenyRule:
    pattern: str
    reason: str


@dataclass(frozen=True, slots=True)
class RedactRule:
    name: str
    pattern: str


@dataclass(frozen=True, slots=True)
class FlashConfig:
    chip: str | None = None  # from [profile].chip
    baud: int = 460800
    before: str = "default-reset"
    after: str = "hard-reset"
    flash_mode: str | None = None
    flash_size: str | None = None
    flash_freq: str | None = None

    @property
    def overrides(self) -> dict[str, str]:
        """The flash_* settings this profile sets, keyed by esptool's hyphen names."""
        values = {
            "flash-mode": self.flash_mode,
            "flash-size": self.flash_size,
            "flash-freq": self.flash_freq,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class EnvGate:
    timeout: float = 60.0
    require: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    path: Path
    console: ConsoleConfig = field(default_factory=ConsoleConfig)
    boot: BootConfig = field(default_factory=BootConfig)
    identity: list[IdentityField] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    deny: list[DenyRule] = field(default_factory=list)
    redact: list[RedactRule] = field(default_factory=list)
    flash: FlashConfig = field(default_factory=FlashConfig)
    env_gate: EnvGate | None = None

    def command(self, name: str) -> Command:
        try:
            return self.commands[name]
        except KeyError:
            known = ", ".join(sorted(self.commands)) or "none"
            raise UsageError(
                f"unknown command {name!r} in profile {self.name} (has: {known})"
            ) from None

    def check_allowed(self, line: str) -> None:
        """Raise UsageError `denied: <reason>` when a deny pattern matches the rendered line."""
        for rule in self.deny:
            if re.search(rule.pattern, line):
                raise UsageError(f"denied: {rule.reason}", data={"pattern": rule.pattern})


def _console(root: Table) -> ConsoleConfig:
    t = root.table("console", _CONSOLE_KEYS)
    d = ConsoleConfig()
    return ConsoleConfig(
        baud=t.integer("baud", d.baud),
        line_ending=t.text("line_ending", d.line_ending),
        prompt=t.opt_text("prompt", nonempty=True),
        timeout=t.seconds("timeout", d.timeout),
        tx_chunk=t.integer("tx_chunk", d.tx_chunk),
        tx_chunk_delay_ms=t.number("tx_chunk_delay_ms", d.tx_chunk_delay_ms, positive=False),
    )


def _boot(root: Table, prompt: str | None) -> BootConfig:
    t = root.table("boot", _BOOT_KEYS)
    d = BootConfig()
    ready = t.opt_regex("ready")
    # idf.py's compilers are the ones the detector runs, so validate with them.
    reset_patterns = t.text_list("reset_patterns")
    for index, pattern in enumerate(reset_patterns, 1):
        try:
            idf.compile_reset_patterns([pattern])
        except UsageError as exc:
            t.checker.error(f"{t.at('reset_patterns')}[{index}]", exc.message)
    sha_pattern = t.text("sha_pattern", d.sha_pattern)
    try:
        idf.compile_sha_pattern(sha_pattern)
    except UsageError as exc:
        t.error("sha_pattern", exc.message)
    default_ready = None if prompt is None else re.escape(prompt)
    return BootConfig(
        ready=default_ready if ready is None else ready.pattern,
        settle=t.seconds("settle", d.settle, positive=False),
        ready_timeout=t.seconds("ready_timeout", d.ready_timeout),
        probe=t.text("probe", d.probe),
        probe_interval=t.seconds("probe_interval", d.probe_interval),
        reset_patterns=reset_patterns,
        sha_pattern=sha_pattern,
        expected_reset_window=t.seconds("expected_reset_window", d.expected_reset_window),
        ms_regression=t.integer("ms_regression", d.ms_regression),
    )


def _identity(root: Table) -> list[IdentityField]:
    fields: list[IdentityField] = []
    seen: set[str] = set()
    for t in root.table("identity", ("field",)).tables("field", _IDENTITY_FIELD_KEYS):
        name = t.req_text("name")
        if name in seen:
            t.error("name", f"duplicate identity field {name!r}")
        seen.add(name)
        source = t.opt_text("source")
        if source is None:
            t.error("source", f"required: one of {', '.join(IDENTITY_SOURCES)}")
        elif source not in IDENTITY_SOURCES:
            t.error("source", f"must be one of {', '.join(IDENTITY_SOURCES)}; got {source!r}")
        regex = t.req_regex("regex")
        if regex is not None and regex.groups < 1:
            t.error("regex", "needs a capture group: group 1 is the field's value")
        send = t.opt_text("send", nonempty=True)
        filter_ = t.opt_text("filter", nonempty=True)
        if source == "cmd" and not t.has("send"):
            t.error("send", 'required when source = "cmd"')
        if source == "boot":
            for key in ("send", "filter"):
                if t.has(key):
                    t.error(key, 'only applies to source = "cmd"')
        fields.append(
            IdentityField(
                name=name,
                source="boot" if source == "boot" else "cmd",
                regex="" if regex is None else regex.pattern,
                send=send,
                filter=filter_,
                timeout=t.opt_seconds("timeout"),
            )
        )
    return fields


def _commands(root: Table, prompt: str | None) -> dict[str, Command]:
    commands: dict[str, Command] = {}
    table = root.table("commands", None)
    for name in table.data:
        t = table.table(name, _COMMAND_KEYS)
        send = t.req_text("send")
        try:
            placeholders(send)
        except UsageError as exc:
            t.error("send", exc.message)
        expect = t.opt_text("expect")
        capture = t.flag("capture", False)
        if capture and expect is not None:
            t.error(None, "capture and expect are exclusive; use one")
        if expect == "":
            t.error("expect", "must not be empty")
        if capture and prompt is None:
            t.error("capture", "needs console.prompt to know where the reply ends")
        markers = t.text_list("error")
        for index, marker in enumerate(markers, 1):
            if not marker:
                t.checker.error(f"{t.at('error')}[{index}]", "must not be empty: every line has it")
        commands[name] = Command(
            name=name,
            send=send,
            description=t.opt_text("description"),
            expect=expect or None,
            error=markers,
            capture=capture,
            timeout=t.opt_seconds("timeout"),
            reboots=t.flag("reboots", False),
        )
    return commands


def _deny(root: Table) -> list[DenyRule]:
    rules: list[DenyRule] = []
    for t in root.tables("deny", _DENY_KEYS):
        regex = t.req_regex("pattern")
        reason = t.req_text("reason")
        rules.append(DenyRule(pattern="" if regex is None else regex.pattern, reason=reason))
    return rules


def _redact(root: Table) -> list[RedactRule]:
    rules: list[RedactRule] = []
    for t in root.tables("redact", _REDACT_KEYS):
        name = t.req_text("name")
        if not t.has("pattern"):
            t.error("pattern", "required")
        pattern = t.opt_text("pattern", nonempty=True) or ""
        if pattern:
            try:
                Redactor([pattern])
            except UsageError as exc:
                t.error("pattern", exc.message)
        rules.append(RedactRule(name=name, pattern=pattern))
    return rules


def _flash(root: Table, chip: str | None) -> FlashConfig:
    t = root.table("flash", _FLASH_KEYS)
    d = FlashConfig()
    return FlashConfig(
        chip=chip,
        baud=t.integer("baud", d.baud),
        before=t.choice("before", FLASH_BEFORE, d.before),
        after=t.choice("after", FLASH_AFTER, d.after),
        flash_mode=t.opt_text("flash_mode", nonempty=True),
        flash_size=t.opt_text("flash_size", nonempty=True),
        flash_freq=t.opt_text("flash_freq", nonempty=True),
    )


def _env_gate(root: Table) -> EnvGate | None:
    t = root.opt_table("env_gate", _ENV_GATE_KEYS)
    if t is None:
        return None
    d = EnvGate()
    return EnvGate(
        timeout=t.seconds("timeout", d.timeout),
        require=[regex.pattern for regex in t.regex_list("require")],
        forbid=[regex.pattern for regex in t.regex_list("forbid")],
    )


def build_profile(data: Mapping[str, Any], path: Path) -> Profile:
    """Validate parsed TOML into a Profile. Raises ConfigError listing every problem."""
    checker = Checker(path)
    root = Table(checker, data, "", _TOP_KEYS)
    meta = root.table("profile", _PROFILE_KEYS, required=True)
    console = _console(root)
    profile = Profile(
        name=meta.req_text("name"),
        path=path,
        console=console,
        boot=_boot(root, console.prompt),
        identity=_identity(root),
        commands=_commands(root, console.prompt),
        deny=_deny(root),
        redact=_redact(root),
        flash=_flash(root, meta.opt_text("chip", nonempty=True)),
        env_gate=_env_gate(root),
    )
    checker.done()
    return profile


def parse_profile(text: str, path: Path) -> Profile:
    """A Profile from TOML text; `path` names it in errors and in `Profile.path`."""
    return build_profile(loads_toml(text, path), path)


def read_profile(path: Path) -> Profile:
    """The profile in `path` (a `profile.toml`). Raises ConfigError."""
    return build_profile(load_toml(path), path)


def _explicit_path(text: str, cwd: Path) -> Path | None:
    """`text` as a profile file or directory, or None when it reads as a profile name."""
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if candidate.is_file():
        return candidate
    if (candidate / PROFILE_FILE).is_file():
        return candidate / PROFILE_FILE
    looks_like_path = "/" in text or text.startswith((".", "~")) or text.endswith(".toml")
    if looks_like_path:
        raise UsageError(f"no profile at {candidate}")
    return None


def _local_name(path: Path) -> str | None:
    try:
        meta = load_toml(path).get("profile")
    except ConfigError:
        return None
    name = meta.get("name") if isinstance(meta, dict) else None
    return name if isinstance(name, str) else None


def _local_profile(name: str | None, cwd: Path) -> Profile | None:
    # A project's .benchctl/ answers for its own profile name only, so a repo checkout (even
    # a broken one) does not shadow another profile; with no name asked for it is the answer.
    for directory in (cwd, *cwd.parents):
        path = directory / LOCAL_DIR / PROFILE_FILE
        if path.is_file() and (name is None or _local_name(path) == name):
            return read_profile(path)
    return None


def load_profile(name_or_path: str | None, cwd: Path, config_dir: Path) -> Profile:
    """Resolve and load a profile (Section 7.3 lookup order): a path to a profile dir or
    file, then `.benchctl/profile.toml` up from `cwd`, then `<config>/profiles/<name>/`.
    Raises UsageError (ConfigError when the file is invalid)."""
    if name_or_path is not None:
        explicit = _explicit_path(name_or_path, cwd)
        if explicit is not None:
            return read_profile(explicit)
    local = _local_profile(name_or_path, cwd.resolve())
    if local is not None:
        return local
    if name_or_path is None:
        raise UsageError(
            "no profile: pass --profile, give the unit a profile in units.toml, "
            f"or add {LOCAL_DIR}/{PROFILE_FILE} to the project"
        )
    if not _PROFILE_NAME.fullmatch(name_or_path):
        raise UsageError(f"invalid profile name {name_or_path!r}")
    path = config_dir / "profiles" / name_or_path / PROFILE_FILE
    if not path.is_file():
        raise UsageError(
            f"profile {name_or_path!r} not found: no {LOCAL_DIR}/{PROFILE_FILE} for it "
            f"above {cwd}, and no {path}"
        )
    return read_profile(path)
