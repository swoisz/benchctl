"""`capture --reset` in daemon and direct mode (design Section 4.11).

Scaffold stub: the signatures are the contract `daemon.py`/`cli.py` build against; U6
replaces this file wholesale.
"""

from __future__ import annotations

from benchctl.types import LogLine, ModemLines, ResetInfo, Session


def capture_on_session(session: Session, seconds: float) -> tuple[list[LogLine], ResetInfo | None]:
    """Reset, then collect `seconds` of lines from the reset's offset."""
    raise NotImplementedError("capture_on_session is provided by U6")


def capture_direct(
    port_path: str, baud: int, seconds: float, lines: ModemLines | None = None
) -> list[LogLine]:
    """Open the port, reset through `lines`, read for `seconds`; `t` is since the reset."""
    raise NotImplementedError("capture_direct is provided by U6")
