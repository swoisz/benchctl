"""Scenario TOML: load and validate, schema only (design Section 7.8).

The runner trusts what this returns: durations are float seconds, `at` is the offset after
the last trigger, and every `since` names a keyword or a mark set by an earlier step.
Per-kind timeout defaults are the runner's, so `Step.timeout` stays None when unset.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from benchctl.artifacts import IMAGE_NAMES
from benchctl.durations import parse_duration
from benchctl.errors import UsageError
from benchctl.profile import (
    FLASH_AFTER,
    PROFILE_FILE,
    Checker,
    ConfigError,
    Table,
    load_toml,
    read_profile,
)
from benchctl.registry import UNITS_FILE, read_registry
from benchctl.types import Cursor

StepKind = Literal["human", "external", "cmd", "wait", "reset", "flash", "mark"]
STEP_KINDS: tuple[StepKind, ...] = ("human", "external", "cmd", "wait", "reset", "flash", "mark")
SINCE_KEYWORDS = ("prev", "step", "trigger", "boot", "attach")
SCENARIOS_DIR = "scenarios"

_TOP_KEYS = ("name", "profile", "budget", "preconditions", "step")
_PRECONDITION_KEYS = ("env_gate", "fw_version")
_COMMON_KEYS = ("name", "kind", "at", "timeout", "unit", "expect_reset", "continue_on_fail")
_KIND_KEYS: dict[str, tuple[str, ...]] = {
    "human": ("prompt", "confirm", "trigger"),
    "external": ("run", "undo", "cwd", "env"),
    "cmd": ("send", "command", "args", "expect", "error", "capture", "secret"),
    "wait": ("pattern", "since", "absent", "window", "order", "trigger"),
    "reset": ("bootloader",),
    "flash": ("artifacts", "files", "only", "after"),
    "mark": (),
}
_FLASH_FILE_KEYS = ("addr", "file")
# Steps whose match moves the trigger by default (Section 5).
_TRIGGER_KINDS = ("human", "wait")


@dataclass(frozen=True, slots=True)
class Preconditions:
    env_gate: bool = False
    fw_version: str | None = None  # regex on identity fw_version


@dataclass(frozen=True, slots=True)
class Step:
    """One `[[step]]`. Fields that do not apply to `kind` keep their defaults."""

    index: int  # from 1, as results.md numbers steps
    kind: StepKind
    name: str | None = None  # for `mark`, the mark's name
    at: float | None = None  # seconds after the last trigger match
    timeout: float | None = None
    expect_reset: bool = False
    continue_on_fail: bool = False
    trigger: bool = False
    unit: str | None = None  # reserved; unused in v1
    # human
    prompt: str | None = None
    confirm: str | None = None
    # external
    run: str | None = None
    undo: str | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # cmd
    send: str | None = None
    command: str | None = None
    args: dict[str, str] = field(default_factory=dict)
    expect: str | None = None
    error: list[str] = field(default_factory=list)
    capture: bool | None = None
    secret: bool = False
    # wait
    pattern: str | None = None
    since: str = "prev"
    absent: str | None = None
    window: float | None = None
    order: list[str] | None = None
    # reset
    bootloader: bool = False
    # flash: exactly one of `artifacts` (paths as written) and `files` (addr, path)
    artifacts: list[str] | None = None
    files: list[tuple[int, str]] | None = None
    only: list[str] | None = None
    after: str | None = None

    @property
    def flash_specs(self) -> list[str]:
        """The `flash` step's inputs in `artifacts.resolve` form."""
        if self.files is not None:
            return [f"{addr:#x}={path}" for addr, path in self.files]
        return list(self.artifacts or [])


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    path: Path
    steps: list[Step]
    profile: str | None = None
    budget: float | None = None  # declared worst case, seconds
    preconditions: Preconditions = field(default_factory=Preconditions)


def _duration(t: Table, key: str) -> float | None:
    value = t.data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        t.error(key, f'expected a duration such as "50s", got {value!r}')
        return None
    try:
        seconds = parse_duration(value)
    except UsageError as exc:
        t.error(key, exc.message)
        return None
    if not math.isfinite(seconds):
        t.error(key, f"invalid duration {value!r}")
        return None
    return seconds


def _at(t: Table) -> float | None:
    value = t.data.get("at")
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("+"):
        try:
            return parse_duration(value[1:])
        except UsageError:
            pass
    t.error("at", f'expected "+<duration>" after the last trigger, e.g. "+5s"; got {value!r}')
    return None


def _pattern(t: Table, key: str) -> str | None:
    regex = t.opt_regex(key)
    return None if regex is None else regex.pattern


def _flash_files(t: Table) -> list[tuple[int, str]] | None:
    if not t.has("files"):
        return None
    if t.data["files"] == []:
        t.error("files", "must list at least one { addr, file }")
    files: list[tuple[int, str]] = []
    for entry in t.tables("files", _FLASH_FILE_KEYS):
        addr = entry.data.get("addr")
        path = entry.req_text("file")
        number: int | None = None
        if isinstance(addr, int) and not isinstance(addr, bool):
            number = addr
        elif isinstance(addr, str):
            try:
                number = int(addr, 0)
            except ValueError:
                number = None
        if addr is None:
            entry.error("addr", "required")
        elif number is None or number < 0:
            entry.error("addr", f'expected a flash address such as "0x10000", got {addr!r}')
        else:
            files.append((number, path))
    return files


def _flash_artifacts(t: Table) -> list[str] | None:
    value = t.data.get("artifacts")
    if value is None:
        return None
    items: list[str] = []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = list(cast(list[str], value))
    if not items or not all(items):
        t.error("artifacts", "expected a build dir, flasher_args.json, or a list of them")
        return None
    return items


def _is_mark_name(name: str) -> bool:
    if name in SINCE_KEYWORDS:
        return False
    try:
        return Cursor.parse(name).kind == "mark"
    except UsageError:
        return False


def _req_pattern(t: Table, key: str) -> str:
    regex = t.req_regex(key)
    return "" if regex is None else regex.pattern


def _human(t: Table, base: Step) -> Step:
    return replace(base, prompt=t.req_text("prompt"), confirm=_req_pattern(t, "confirm"))


def _external(t: Table, base: Step) -> Step:
    return replace(
        base,
        run=t.req_text("run"),
        undo=t.opt_text("undo", nonempty=True),
        cwd=t.opt_text("cwd", nonempty=True),
        env=t.text_map("env"),
    )


def _cmd(t: Table, base: Step) -> Step:
    if t.has("send") == t.has("command"):
        t.error(None, "needs exactly one of send and command")
    if t.has("args") and not t.has("command"):
        t.error("args", "only applies with command")
    if t.data.get("capture") is True and t.has("expect"):
        t.error(None, "capture and expect are exclusive")
    return replace(
        base,
        send=t.opt_text("send", nonempty=True),
        command=t.opt_text("command", nonempty=True),
        args=t.text_map("args", scalars=True),
        expect=_pattern(t, "expect"),
        error=[regex.pattern for regex in t.regex_list("error")],
        capture=t.opt_flag("capture"),
        secret=t.flag("secret", False),
    )


def _wait(t: Table, base: Step, marks: set[str]) -> Step:
    if not any(t.has(key) for key in ("pattern", "absent", "order")):
        t.error(None, "needs at least one of pattern, absent and order")
    if t.has("pattern") and t.has("order"):
        t.error(None, "pattern and order are exclusive")
    if t.has("window") and not t.has("absent"):
        t.error("window", "only applies with absent")
    order: list[str] | None = None
    if t.has("order"):
        if t.data["order"] == []:
            t.error("order", "must not be empty")
        order = [regex.pattern for regex in t.regex_list("order")]
    since = t.text("since", "prev")
    if since not in SINCE_KEYWORDS and since not in marks:
        t.error(
            "since",
            f"unknown mark {since!r}: set it in an earlier mark step, "
            f"or use one of {', '.join(SINCE_KEYWORDS)}",
        )
    return replace(
        base,
        pattern=_pattern(t, "pattern"),
        since=since,
        absent=_pattern(t, "absent"),
        window=_duration(t, "window"),
        order=order,
    )


def _flash(t: Table, base: Step) -> Step:
    if t.has("artifacts") == t.has("files"):
        t.error(None, "needs exactly one of artifacts and files")
    only = t.opt_text_list("only")
    if only is not None and not t.has("artifacts"):
        t.error("only", "only applies with artifacts: files are named by address")
    for name in only or []:
        if name not in IMAGE_NAMES:
            t.error("only", f"unknown image {name!r}; one of {', '.join(IMAGE_NAMES)}")
    after = t.opt_text("after")
    if after is not None and after not in FLASH_AFTER:
        t.error("after", f"must be one of {', '.join(FLASH_AFTER)}; got {after!r}")
    return replace(
        base,
        artifacts=_flash_artifacts(t),
        files=_flash_files(t),
        only=only,
        after=after,
    )


def _mark(t: Table, base: Step, marks: set[str]) -> Step:
    if not t.has("name"):
        t.error("name", "required: the mark's name")
    elif base.name is not None:
        if not _is_mark_name(base.name):
            t.error("name", f"{base.name!r} is a cursor keyword or an offset, not a mark name")
        marks.add(base.name)
    return base


def _step(t: Table, index: int, kind: StepKind, marks: set[str]) -> Step:
    base = Step(
        index=index,
        kind=kind,
        name=t.opt_text("name", nonempty=True),
        at=_at(t),
        timeout=_duration(t, "timeout"),
        expect_reset=t.flag("expect_reset", False),
        continue_on_fail=t.flag("continue_on_fail", False),
        trigger=t.flag("trigger", kind in _TRIGGER_KINDS),
        unit=t.opt_text("unit", nonempty=True),
    )
    if kind == "human":
        return _human(t, base)
    if kind == "external":
        return _external(t, base)
    if kind == "cmd":
        return _cmd(t, base)
    if kind == "wait":
        return _wait(t, base, marks)
    if kind == "reset":
        return replace(base, bootloader=t.flag("bootloader", False))
    if kind == "flash":
        return _flash(t, base)
    return _mark(t, base, marks)


def build_scenario(data: Mapping[str, Any], path: Path) -> Scenario:
    """Validate parsed TOML into a Scenario. Raises ConfigError listing every problem."""
    checker = Checker(path)
    root = Table(checker, data, "", _TOP_KEYS)
    pre = root.table("preconditions", _PRECONDITION_KEYS)
    marks: set[str] = set()
    steps: list[Step] = []
    if root.data.get("step") in (None, []):
        root.error("step", "needs at least one step")
    for index, raw in enumerate(root.tables("step", None), 1):
        kind = raw.data.get("kind")
        if kind is None:
            raw.error("kind", f"required: one of {', '.join(STEP_KINDS)}")
            continue
        if kind not in STEP_KINDS:
            raw.error("kind", f"must be one of {', '.join(STEP_KINDS)}; got {kind!r}")
            continue
        t = Table(checker, raw.data, raw.path, _COMMON_KEYS + _KIND_KEYS[kind])
        steps.append(_step(t, index, cast(StepKind, kind), marks))
    scenario = Scenario(
        name=root.req_text("name"),
        path=path,
        steps=steps,
        profile=root.opt_text("profile", nonempty=True),
        budget=_duration(root, "budget"),
        preconditions=Preconditions(
            env_gate=pre.flag("env_gate", False),
            fw_version=_pattern(pre, "fw_version"),
        ),
    )
    checker.done()
    return scenario


def load_scenario(path: Path) -> Scenario:
    """The scenario in `path`. Raises ConfigError (a UsageError, exit 2)."""
    return build_scenario(load_toml(path), path)


# --- the `validate` verb -----------------------------------------------------------------

Kind = Literal["profile", "scenario", "units"]


def _kind_of(path: Path) -> Kind:
    if path.name == PROFILE_FILE:
        return "profile"
    if path.name == UNITS_FILE:
        return "units"
    try:
        data = load_toml(path)
    except ConfigError:
        return "scenario"  # the scenario check reports the parse error
    if isinstance(data.get("profile"), dict):
        return "profile"
    if "units" in data:
        return "units"
    return "scenario"


def _targets(path: Path) -> list[tuple[Kind, Path]]:
    if not path.is_dir():
        return [(_kind_of(path), path)] if path.exists() else []
    found: list[tuple[Kind, Path]] = []
    if (path / PROFILE_FILE).is_file():
        found.append(("profile", path / PROFILE_FILE))
    if (path / UNITS_FILE).is_file():
        found.append(("units", path / UNITS_FILE))
    scenarios = path / SCENARIOS_DIR
    if scenarios.is_dir():
        found += [("scenario", file) for file in sorted(scenarios.glob("*.toml"))]
    if not found:
        found = [(_kind_of(file), file) for file in sorted(path.glob("*.toml"))]
    return found


def _check(kind: Kind, path: Path) -> list[str]:
    try:
        if kind == "profile":
            read_profile(path)
        elif kind == "units":
            read_registry(path)
        else:
            load_scenario(path)
    except ConfigError as exc:
        return exc.errors
    except UsageError as exc:
        return [f"{path}: {exc.message}"]
    return []


def validate_paths(paths: list[Path]) -> list[dict[str, object]]:
    """The `validate` verb's rows, `{path, kind, ok, errors}`: one per profile, units or
    scenario file found at or under each path. Never raises."""
    rows: list[dict[str, object]] = []
    for path in paths:
        targets = _targets(path)
        if not targets:
            reason = "not found" if not path.exists() else "no profile, units or scenario files"
            kind: Kind = "profile" if path.is_dir() else "scenario"
            rows.append({"path": str(path), "kind": kind, "ok": False, "errors": [reason]})
            continue
        for kind, file in targets:
            errors = _check(kind, file)
            rows.append({"path": str(file), "kind": kind, "ok": not errors, "errors": errors})
    return rows
