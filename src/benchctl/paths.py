"""Config and state locations (design Section 3.1).

Every function reads the environment when called, not at import, so tests can redirect
both trees per test.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from benchctl.errors import UsageError

# Unit names become path components; `runs` would collide with <state>/runs/.
_UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_RESERVED_UNITS = frozenset({"runs"})


def _resolve(override: str, xdg: str, fallback: str) -> Path:
    explicit = os.environ.get(override)
    if explicit:
        return Path(explicit).expanduser()
    # The XDG spec says relative values are invalid and must be ignored.
    base = os.environ.get(xdg)
    if base and Path(base).is_absolute():
        return Path(base) / "benchctl"
    return Path.home() / fallback / "benchctl"


def config_dir() -> Path:
    return _resolve("BENCHCTL_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")


def state_dir() -> Path:
    return _resolve("BENCHCTL_STATE_DIR", "XDG_STATE_HOME", ".local/state")


def units_toml() -> Path:
    return config_dir() / "units.toml"


def profile_dir(name: str) -> Path:
    return config_dir() / "profiles" / name


def check_unit_name(unit: str) -> str:
    """Return `unit` unchanged, or raise UsageError if it cannot be a state directory name."""
    if not _UNIT_NAME.fullmatch(unit) or unit in _RESERVED_UNITS:
        raise UsageError(f"invalid unit name {unit!r}")
    return unit


def unit_dir(unit: str) -> Path:
    return state_dir() / check_unit_name(unit)


def socket_path(unit: str) -> Path:
    return unit_dir(unit) / "sock"


def pid_path(unit: str) -> Path:
    return unit_dir(unit) / "daemon.pid"


def lease_path(unit: str) -> Path:
    return unit_dir(unit) / "lease.json"


def lease_lock_path(unit: str) -> Path:
    return unit_dir(unit) / "lease.lock"


def daemon_log_path(unit: str) -> Path:
    return unit_dir(unit) / "daemon.log"


def sessions_dir(unit: str) -> Path:
    return unit_dir(unit) / "sessions"


def runs_dir(unit: str) -> Path:
    return state_dir() / "runs" / check_unit_name(unit)


def timestamp(when: datetime | None = None) -> str:
    """`YYYYmmddTHHMMSS` in local time, the name of session and run directories."""
    return (when or datetime.now()).strftime("%Y%m%dT%H%M%S")
