"""The shell-ready gate (design Section 4.6): may a command be sent yet?

A pure state machine over seconds-since-attach; the session feeds it lines, tail changes,
resets and port changes, ticks it, and sends the probes it asks for. Every input returns the
transitions it caused, so the session can emit one `gate` event each.
"""

from __future__ import annotations

import re
from typing import Literal

from benchctl.types import GateState

GateName = Literal["booting", "settling", "ready", "unknown", "bootloader"]
# States a ready signal (the pattern, or the prompt after a probe) moves on to settling. Not
# `ready`: the prompt reprints after every command and must not restart the settle time.
_AWAITING_SHELL = ("booting", "unknown")


class Gate:
    def __init__(
        self,
        *,
        ready: str | None,
        prompt: str | None,
        settle: float,
        ready_timeout: float,
        probe_interval: float,
        now: float,
    ) -> None:
        self._ready = None if ready is None else re.compile(ready)
        self._prompt = prompt
        self._settle = settle
        self._ready_timeout = ready_timeout
        self._probe_interval = probe_interval
        self._state: GateName = "booting"
        self._since = now
        self._method: str | None = None
        self._last_probe: float | None = None

    @property
    def state(self) -> GateState:
        return GateState(self._state, self._since, self._method)

    @property
    def name(self) -> GateName:
        return self._state

    def _enter(self, state: GateName, method: str | None, now: float) -> list[GateState]:
        # Re-entering the same state restarts its clock (a second reset while booting) but is
        # not a transition to report.
        changed = (state, method) != (self._state, self._method)
        self._state, self._method, self._since = state, method, now
        self._last_probe = None
        return [self.state] if changed else []

    def _signal(self, now: float) -> list[GateState]:
        return self._enter("settling", "pattern" if self._last_probe is None else "probe", now)

    def on_reset(self, now: float, *, download: bool) -> list[GateState]:
        """A reset line. `download`: its boot mode says the chip is in the ROM bootloader."""
        return self._enter("bootloader" if download else "booting", None, now)

    def on_line(self, text: str, now: float) -> list[GateState]:
        if self._state in _AWAITING_SHELL and self._ready is not None and self._ready.search(text):
            return self._signal(now)
        return []

    def on_tail(self, tail: str, now: float) -> list[GateState]:
        if self._state not in _AWAITING_SHELL:
            return []
        if self._ready is not None and self._ready.search(tail):
            return self._signal(now)
        # A custom ready pattern is a boot line no probe reprints; the probe answers with the
        # prompt.
        if self._last_probe is not None and self._prompt and tail.endswith(self._prompt):
            return self._signal(now)
        return []

    def on_port_change(self, now: float) -> list[GateState]:
        """The port was lost, or came back without a reset line: the shell's state is unknown."""
        return self._enter("unknown", None, now)

    def set(self, state: GateName, method: str | None, now: float) -> list[GateState]:
        return self._enter(state, method, now)

    def tick(self, now: float) -> list[GateState]:
        """Time-driven transitions due at `now`."""
        changes: list[GateState] = []
        if self._prompt is None and (
            self._state == "unknown"
            or (self._state == "booting" and now >= self._since + self._ready_timeout)
        ):
            # Without a prompt a probe has no answer to look for, so the step that would probe
            # trusts the shell instead (settle still applies).
            changes += self._enter("settling", None, now)
        if self._state == "settling" and now >= self._since + self._settle:
            changes += self._enter("ready", self._method, now)
        return changes

    def next_tick(self, now: float) -> float | None:
        """When `tick` will next have something to do, or None."""
        if self._state == "settling":
            return self._since + self._settle
        if self._prompt is None and self._state == "booting":
            return self._since + self._ready_timeout
        if self._prompt is None and self._state == "unknown":
            return now
        return None

    def next_probe(self) -> float | None:
        """When the next probe is due, or None when the gate is not probing."""
        if self._prompt is None:
            return None
        if self._state == "unknown":
            start = self._since
        elif self._state == "booting":
            start = self._since + self._ready_timeout
        else:
            return None
        return start if self._last_probe is None else self._last_probe + self._probe_interval

    def probe_sent(self, now: float) -> None:
        self._last_probe = now
