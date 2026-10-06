"""A unit whose port vanishes and comes back at a new path while the real daemon holds it
(design Sections 4.6, 4.9)."""

from __future__ import annotations

from conftest import UNIT
from fakedev import APP_VERSION
from integration.conftest import Bench


def test_vanish_and_reappear_give_port_lost_port_back_and_a_working_cmd(bench: Bench) -> None:
    device = bench.add_unit(UNIT)
    bench.cli("mark", "before")  # attach and verify
    old_path = device.path

    device.vanish()
    lost = bench.await_event(UNIT, lambda event: event["type"] == "port_lost")
    status = bench.cli("status")
    assert status["daemon"]["connected"] is False
    assert status["gate"]["state"] == "unknown"

    new_path = device.reappear()
    assert new_path != old_path
    back = bench.await_event(UNIT, lambda event: event["type"] == "port_back")
    assert back["path"] == new_path
    assert back["seq"] > lost["seq"]
    assert back["gap"] > 0

    # reappear() replays no boot: the gate reopens through a probe, not a reset line.
    cmd = bench.cli("cmd", "version")
    assert cmd["status"] == "ok"
    assert cmd["captured"] == f"Application Version: {APP_VERSION}"
    status = bench.cli("status")
    assert status["daemon"]["connected"] is True
    assert status["daemon"]["port"] == new_path
    assert status["gate"]["state"] == "ready"

    events = bench.events(UNIT)
    after_back = [event for event in events if event["seq"] > back["seq"]]
    assert any(event["type"] == "tx" and event["source"] == "probe" for event in after_back)
    assert any(
        event["type"] == "gate" and event["state"] == "settling" and event["method"] == "probe"
        for event in after_back
    )
    assert [event["type"] for event in events].count("reset") == 1  # the attach's only
    assert device.resets == ["poweron"]
    assert bench.bridge.pulses[UNIT] == [False]
    port_lines = bench.cli("log", "--grep", "^## port ")["lines"]
    assert [line["text"].split(" ", 3)[2] for line in port_lines] == ["lost", "back"]
    assert f"path={new_path}" in port_lines[1]["text"]
    assert bench.cli("detach")["detached"] is True
