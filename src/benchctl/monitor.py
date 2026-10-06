"""Interactive pass-through (design Section 7.1 `monitor`), line-oriented in v1.

Live lines come from a `tail` feed on a printer thread; each typed line is sent like
`cmd --no-wait` with `source: "monitor"`. `!reset` resets the unit; Ctrl-] or end of input
exits.
"""

from __future__ import annotations

import contextlib
import io
import os
import termios
import threading
from collections.abc import Iterator
from typing import TextIO

import click

from benchctl.errors import BenchctlError
from benchctl.profile import Profile
from benchctl.types import DeviceClient, LogLine, Offset, Subscription

EXIT_CHAR = "\x1d"  # Ctrl-]
RESET_COMMAND = "!reset"
SOURCE = "monitor"
_FEED_TICK_S = 0.2
_JOIN_S = 1.0


def handle_line(client: DeviceClient, profile: Profile, line: str) -> bool:
    """Act on one typed line; False means exit. Errors are reported and the session goes on."""
    if EXIT_CHAR in line:
        return False
    try:
        if line.strip() == RESET_COMMAND:
            client.reset(False)
        else:
            profile.check_allowed(line)
            client.cmd(
                line,
                expect=None,
                error=[],
                capture=False,
                timeout=profile.console.timeout,
                reboots=False,
                secret=False,
                force=False,
                source=SOURCE,
            )
    except BenchctlError as exc:
        click.echo(f"benchctl: {exc.message}", err=True)
    return True


def run_monitor(client: DeviceClient, profile: Profile, stdin: TextIO, *, since: Offset) -> int:
    """Print the unit's lines from `since` while sending what is typed; returns 0 on exit."""
    feed = client.tail(since, events=False)
    stop = threading.Event()
    printer = threading.Thread(target=_print_feed, args=(feed, stop), daemon=True)
    printer.start()
    try:
        with _exit_key_ends_line(stdin):
            while raw := stdin.readline():
                if not handle_line(client, profile, raw.rstrip("\r\n")):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        feed.close()
        printer.join(_JOIN_S)
    return 0


def _print_feed(feed: Subscription, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            item = feed.get(_FEED_TICK_S)
        except BenchctlError as exc:
            click.echo(f"benchctl: {exc.message}", err=True)
            return
        if isinstance(item, LogLine):
            click.echo(item.text)


@contextlib.contextmanager
def _exit_key_ends_line(stream: TextIO) -> Iterator[None]:
    """On a terminal, make Ctrl-] an extra end-of-line character so it is delivered at once
    while the line editing of canonical mode stays (raw mode would lose it)."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        yield
        return
    if not os.isatty(fd):
        yield
        return
    saved = termios.tcgetattr(fd)
    changed = [*saved[:6], list(saved[6])]
    changed[6][termios.VEOL] = EXIT_CHAR.encode()
    termios.tcsetattr(fd, termios.TCSANOW, changed)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
