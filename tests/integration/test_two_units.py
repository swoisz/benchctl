"""Two fake devices, two units, two daemons, driven concurrently (design Section 3.5)."""

from __future__ import annotations

import threading
import time
from typing import Any

from fakedev import DEFAULT_REPLIES, DeviceScript
from integration.conftest import CLI_TIMEOUT_S, Bench, shell_lines

UNITS = ("u1", "u2")
ROUNDS = 3


def whoami(unit: str) -> str:
    return f"I am {unit}"


def token(unit: str, n: int) -> str:
    return f"tok-{unit}-{n}"


def test_two_units_interleave_without_cross_talk(bench: Bench) -> None:
    devices = {
        unit: bench.add_unit(
            unit, DeviceScript(replies={**DEFAULT_REPLIES, "whoami": whoami(unit)})
        )
        for unit in UNITS
    }
    start = threading.Barrier(len(UNITS), timeout=CLI_TIMEOUT_S)
    rounds: dict[str, list[dict[str, dict[str, Any]]]] = {unit: [] for unit in UNITS}
    spans: dict[str, tuple[float, float]] = {}
    failures: list[BaseException] = []

    def drive(unit: str) -> None:
        try:
            start.wait()  # both auto-attaches race, then the commands interleave
            began = time.monotonic()
            for n in range(ROUNDS):
                done = {
                    "mark": bench.cli("mark", f"m{n}", "--unit", unit),
                    "who": bench.cli("cmd", "whoami", "--unit", unit),
                    "sent": bench.cli("cmd", token(unit, n), "--unit", unit),
                    "found": bench.cli("wait", token(unit, n), "--since", f"m{n}", "--unit", unit),
                }
                rounds[unit].append(done)
            spans[unit] = (began, time.monotonic())
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=drive, args=(unit,)) for unit in UNITS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(CLI_TIMEOUT_S * 2)
    assert not failures, failures
    (a_start, a_end), (b_start, b_end) = spans["u1"], spans["u2"]
    assert a_start < b_end and b_start < a_end  # the two drives overlapped

    statuses = {unit: bench.cli("status", "--unit", unit) for unit in UNITS}
    assert statuses["u1"]["daemon"]["pid"] != statuses["u2"]["daemon"]["pid"]
    for unit, other in (UNITS, UNITS[::-1]):
        device = devices[unit]
        assert statuses[unit]["daemon"]["port"] == device.path
        last = -1
        for n, done in enumerate(rounds[unit]):
            assert {result["unit"] for result in done.values()} == {unit}
            assert done["who"]["captured"] == whoami(unit)
            assert done["sent"]["captured"] == "command not found"
            assert done["found"]["line"] == f"demo> {token(unit, n)}"
            offsets = [done[key]["offset"] for key in ("mark", "who", "sent", "found")]
            assert last < offsets[0] < offsets[1] < offsets[2] < offsets[3]
            last = offsets[3]
            # The tx offset each reply named is this unit's own `## tx` line.
            tx = done["sent"]["offset"]
            at_tx = bench.cli("log", "--since", str(tx), "--until", str(tx + 1), "--unit", unit)
            assert [line["text"] for line in at_tx["lines"]] == [f"## tx {token(unit, n)}"]

        log = bench.serial_log(unit)
        assert all(token(unit, n) in log for n in range(ROUNDS))
        assert whoami(unit) in log
        assert f"tok-{other}" not in log
        assert whoami(other) not in log
        events = bench.events(unit)
        assert {event["unit"] for event in events} == {unit}
        sent = [e["line"] for e in events if e["type"] == "tx" and e["source"] == "cmd"]
        assert sent == [line for n in range(ROUNDS) for line in ("whoami", token(unit, n))]
        assert shell_lines(device) == sent
        assert device.resets == ["poweron"]
        assert bench.bridge.pulses[unit] == [False]

    for unit in UNITS:
        assert bench.cli("detach", "--unit", unit)["detached"] is True
