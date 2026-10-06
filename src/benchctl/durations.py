"""Duration strings as scenarios write them (design Section 7.8): `250ms`, `50s`, `15m`, `2h`."""

from __future__ import annotations

import re

from benchctl.errors import UsageError

_DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)?")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def parse_duration(value: str | float) -> float:
    """Seconds for a duration string or a bare number (seconds). Raises UsageError."""
    # bool is an int subclass; `timeout = true` in TOML is a mistake, not one second.
    if isinstance(value, bool):
        raise UsageError(f"invalid duration {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise UsageError(f"invalid duration {value!r}: negative")
        return float(value)
    match = _DURATION.fullmatch(value)
    if match is None:
        raise UsageError(f"invalid duration {value!r}: expected e.g. 250ms, 50s, 15m, 2h")
    number, unit = match.groups()
    return float(number) * _UNIT_SECONDS[unit or "s"]


def format_duration(seconds: float) -> str:
    """Compact human form for budgets and heartbeats: `50s`, `0.25s`, `23m40s`, `1h5m`."""
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:g}s"
    total = round(seconds)
    hours, rest = divmod(total, _SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, _SECONDS_PER_MINUTE)
    parts = [
        f"{hours}h" if hours else "",
        f"{minutes}m" if minutes else "",
        f"{secs}s" if secs else "",
    ]
    return "".join(parts)
