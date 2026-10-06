"""The shell-ready gate state machine (design Section 4.6), driven with explicit times."""

from __future__ import annotations

import re

from benchctl.gate import Gate

PROMPT = "demo> "
SETTLE = 2.5
READY_TIMEOUT = 20.0
PROBE_INTERVAL = 1.0


def make(prompt: str | None = PROMPT, ready: str | None = None) -> Gate:
    if ready is None and prompt is not None:
        ready = re.escape(prompt)
    return Gate(
        ready=ready,
        prompt=prompt,
        settle=SETTLE,
        ready_timeout=READY_TIMEOUT,
        probe_interval=PROBE_INTERVAL,
        now=0.0,
    )


def test_boot_goes_booting_settling_ready() -> None:
    gate = make()
    assert gate.name == "booting"
    assert gate.on_line("I (24) boot: ESP-IDF", 0.1) == []
    [settling] = gate.on_tail(PROMPT, 1.2)
    assert (settling.state, settling.method, settling.since) == ("settling", "pattern", 1.2)
    assert gate.next_tick(1.2) == 1.2 + SETTLE
    assert gate.tick(3.0) == []
    [ready] = gate.tick(1.2 + SETTLE)
    assert (ready.state, ready.method) == ("ready", "pattern")
    assert gate.next_tick(4.0) is None


def test_prompt_after_every_command_does_not_restart_settling() -> None:
    gate = make()
    gate.on_tail(PROMPT, 1.0)
    gate.tick(4.0)
    assert gate.on_line("demo> version", 5.0) == []
    assert gate.on_tail(PROMPT, 5.1) == []
    assert gate.name == "ready"


def test_any_reset_returns_to_booting_and_download_to_bootloader() -> None:
    gate = make()
    gate.on_tail(PROMPT, 1.0)
    gate.tick(4.0)
    [booting] = gate.on_reset(10.0, download=False)
    assert (booting.state, booting.since) == ("booting", 10.0)
    # A second reset while booting restarts the clock without a transition.
    assert gate.on_reset(11.0, download=False) == []
    assert gate.state.since == 11.0
    [bootloader] = gate.on_reset(12.0, download=True)
    assert bootloader.state == "bootloader"
    assert gate.on_tail(PROMPT, 13.0) == []
    assert gate.next_probe() is None
    assert gate.tick(100.0) == []


def test_set_reports_a_method_change_in_the_same_state() -> None:
    gate = make()
    [first] = gate.set("bootloader", None, 1.0)
    [flash] = gate.set("bootloader", "flash", 2.0)
    assert (first.method, flash.method) == (None, "flash")
    assert gate.set("bootloader", "flash", 3.0) == []


def test_booting_past_ready_timeout_probes_until_the_prompt() -> None:
    gate = make()
    assert gate.next_probe() == READY_TIMEOUT
    gate.probe_sent(READY_TIMEOUT)
    assert gate.next_probe() == READY_TIMEOUT + PROBE_INTERVAL
    [settling] = gate.on_tail(PROMPT, READY_TIMEOUT + 0.1)
    assert (settling.state, settling.method) == ("settling", "probe")
    assert gate.next_probe() is None


def test_port_change_is_unknown_and_probes_at_once() -> None:
    gate = make()
    gate.on_tail(PROMPT, 1.0)
    gate.tick(4.0)
    [unknown] = gate.on_port_change(30.0)
    assert unknown.state == "unknown"
    assert gate.next_probe() == 30.0
    gate.probe_sent(30.0)
    assert gate.on_line("", 30.05) == []
    [settling] = gate.on_tail(PROMPT, 30.1)
    assert settling.method == "probe"


def test_custom_ready_pattern_settles_on_its_line_and_a_probe_on_the_prompt() -> None:
    gate = make(ready=r"console: shell started")
    assert gate.on_tail(PROMPT, 1.0) == [], "the prompt alone is not the ready line"
    [settling] = gate.on_line("I (1150) console: shell started", 1.2)
    assert settling.method == "pattern"
    gate.on_port_change(10.0)
    assert gate.on_tail(PROMPT, 10.1) == [], "no probe sent yet"
    gate.probe_sent(10.2)
    [probed] = gate.on_tail(PROMPT, 10.3)
    assert probed.method == "probe"


def test_without_prompt_or_ready_a_boot_is_trusted_after_ready_timeout() -> None:
    gate = make(prompt=None, ready=None)
    assert gate.next_probe() is None
    assert gate.next_tick(0.0) == READY_TIMEOUT
    assert gate.tick(READY_TIMEOUT - 0.1) == []
    [settling] = gate.tick(READY_TIMEOUT)
    assert (settling.state, settling.method) == ("settling", None)
    [ready] = gate.tick(READY_TIMEOUT + SETTLE)
    assert ready.state == "ready"
    [unknown, settled] = gate.on_port_change(50.0) + gate.tick(50.0)
    assert (unknown.state, settled.state) == ("unknown", "settling")
