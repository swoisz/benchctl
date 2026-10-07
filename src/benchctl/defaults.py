"""One home for defaults several modules share, so the CLI, daemon and runner cannot drift.

Imports nothing from benchctl, so any module can use it without an import cycle.
"""

from __future__ import annotations

from datetime import datetime

# The ROM prints `rst:` within milliseconds of a reset; 5 s also covers a native-USB re-enumeration.
RESET_WAIT_S = 5.0

# `wait` with no --timeout: long enough for a boot plus Wi-Fi/MQTT bring-up, short enough to fail.
WAIT_TIMEOUT_S = 60.0

# `log` with no --limit: a full boot and a few minutes of chatter without flooding a terminal.
LOG_LIMIT = 10_000

# Progress tick on blocking ops: lets a client tell a slow wait from a dead daemon.
PROGRESS_S = 5.0

# Stands in for a --secret line wherever its text would otherwise be stored or shown.
REDACTED = "<redacted>"


def iso_now(timestamp: float | None = None) -> str:
    """Host time as events and leases carry it: ISO 8601, ms precision, local UTC offset."""
    when = datetime.now() if timestamp is None else datetime.fromtimestamp(timestamp)
    return when.astimezone().isoformat(timespec="milliseconds")
