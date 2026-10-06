"""The real daemon over the real session, with the unit's reset wired back to a fake device.

Run as `python tests/integration/bridged_daemon.py --unit U`; the integration tests point
BENCHCTL_DAEMON_ARGV here. It is `benchctl daemon` with one difference: the session's reset
strategy (Section 4.9's injected `ModemLines`) asks the test process to call `reset()` on the
fake, because a pty has no RTS/DTR lines and the fake lives in the test process. Daemon,
session, transport, line engine, gate and redaction are all the production code.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

from benchctl import daemon
from benchctl.lease import default_owner
from benchctl.profile import Profile
from benchctl.registry import Unit
from benchctl.session import open_session
from benchctl.types import ModemLines, Session

# Directory holding one `<unit>.sock` reset listener per unit, served by the test process.
RESET_DIR_ENV = "FAKEDEV_RESET_DIR"
LAUNCHER = Path(__file__).resolve()
PULSE_TIMEOUT_S = 5.0
ACK = b"ok\n"


def bridge_socket(reset_dir: Path, unit: str) -> Path:
    return reset_dir / f"{unit}.sock"


def bridged_lines(unit: str) -> ModemLines:
    """A reset strategy that has the test process reset the unit's fake. A missing or silent
    listener raises OSError, which the transport reports as a failed pulse."""
    path = bridge_socket(Path(os.environ[RESET_DIR_ENV]), unit)

    def pulse(bootloader: bool) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(PULSE_TIMEOUT_S)
            sock.connect(str(path))
            sock.sendall(b"1\n" if bootloader else b"0\n")
            with sock.makefile("rb") as stream:
                reply = stream.readline()
        if reply != ACK:
            raise OSError(f"reset bridge {path}: got {reply!r}")

    return pulse


def bridged_session(unit: Unit, profile: Profile) -> Session:
    # daemon.open_session, plus the bridged reset strategy.
    return open_session(
        unit=unit, profile=profile, owner=default_owner(), lines=bridged_lines(unit.name)
    )


def launcher_argv() -> str:
    """The BENCHCTL_DAEMON_ARGV that spawns this file with the test interpreter."""
    return f"{sys.executable} {LAUNCHER}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--profile", default=os.environ.get("BENCHCTL_PROFILE"))
    args = parser.parse_args(argv)
    return daemon.main(args.unit, args.profile, session_factory=bridged_session)


if __name__ == "__main__":
    sys.exit(main())
