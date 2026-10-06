"""Flashing on the daemon's held handle, and direct mode (design Section 4.10).

Scaffold stub: the signatures are the contract `daemon.py`/`cli.py` build against; U6
replaces this file wholesale.
"""

from __future__ import annotations

from benchctl.types import FlashResult, Session


def flash_on_session(
    session: Session,
    files: list[tuple[int, str, str]],
    *,
    chip: str,
    before: str,
    after: str,
    baud: int,
    flash_args: dict[str, str],
) -> FlashResult:
    """Write `files` (addr, path, name) through the session's paused transport."""
    raise NotImplementedError("flash_on_session is provided by U6")


def flash_direct(
    port_path: str,
    files: list[tuple[int, str, str]],
    *,
    chip: str,
    before: str,
    after: str,
    baud: int,
    flash_args: dict[str, str],
) -> FlashResult:
    """Write `files` with esptool opening and closing `port_path` itself (no daemon)."""
    raise NotImplementedError("flash_direct is provided by U6")
