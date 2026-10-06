"""The run directory's events.jsonl writer and record builders (design Section 7.5)."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from benchctl.events import (
    EventWriter,
    external_record,
    human_record,
    run_record,
    step_record,
)

COMMON = ["seq", "ts", "t", "unit", "fw", "dev", "type", "offset"]
ISO_MS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d")


def read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def writer(path: Path, t: float = 12.5) -> EventWriter:
    return EventWriter(path, unit="u1", fw="1.2.3", dev="AA:BB", now_t=lambda: t)


def test_runner_records_carry_the_common_fields_first(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = writer(path, t=3.14159)
    events.write(human_record(prompt="Press RESET.", result="confirmed", line="rst:0x1"), offset=7)
    events.write(
        step_record(index=1, name="n", kind="human", result="pass", duration=1.23456, detail="d")
    )
    events.close()

    human, step = read(path)
    assert list(human)[: len(COMMON)] == COMMON
    assert ISO_MS.fullmatch(human["ts"])
    assert (human["seq"], human["t"], human["unit"], human["fw"], human["dev"]) == (
        1,
        3.142,
        "u1",
        "1.2.3",
        "AA:BB",
    )
    assert (human["type"], human["offset"], human["prompt"], human["result"], human["line"]) == (
        "human",
        7,
        "Press RESET.",
        "confirmed",
        "rst:0x1",
    )
    assert step["offset"] is None
    assert step["duration"] == 1.235
    assert "daemon_seq" not in human and "daemon_seq" not in step


def test_builders_produce_the_section_7_5_fields() -> None:
    budget: dict[str, float | None] = {"declared": 60.0, "computed": 75.0}
    run = run_record(phase="end", scenario="s", run_dir="/r", result="pass", budget=budget)
    assert run == {
        "type": "run",
        "phase": "end",
        "scenario": "s",
        "run_dir": "/r",
        "result": "pass",
        "budget": budget,
    }
    external = external_record(
        phase="undo", cmd="rm x", exit=None, duration=0.5, stdout_tail="", stderr_tail="e"
    )
    assert external == {
        "type": "external",
        "phase": "undo",
        "cmd": "rm x",
        "exit": None,
        "duration": 0.5,
        "stdout_tail": "",
        "stderr_tail": "e",
    }
    step = step_record(index=2, name="n", kind="cmd", result="fail", duration=0.0, detail="x")
    assert set(step) == {"type", "index", "name", "kind", "result", "duration", "detail"}


def test_passthrough_renumbers_and_keeps_the_daemon_seq(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = writer(path)
    events.write(run_record(phase="start", scenario="s", run_dir="/r", result=None, budget={}))
    daemon: dict[str, Any] = {
        "seq": 412,
        "ts": "2026-10-06T12:00:00.000+02:00",
        "t": 99.5,
        "unit": "u1",
        "fw": "2.0.0",
        "dev": "CC:DD",
        "type": "reset",
        "offset": 830,
        "expected": True,
    }
    events.passthrough(daemon)
    events.write(human_record(prompt="p", result="timeout", line=None))
    events.close()

    start, reset, human = read(path)
    assert [start["seq"], reset["seq"], human["seq"]] == [1, 2, 3]
    assert reset["daemon_seq"] == 412
    assert {k: v for k, v in reset.items() if k not in ("seq", "daemon_seq")} == {
        k: v for k, v in daemon.items() if k != "seq"
    }
    assert daemon["seq"] == 412, "the caller's record is not modified"
    # The daemon applies units.toml's id_field; its tags win for later runner records.
    assert (human["fw"], human["dev"]) == ("2.0.0", "CC:DD")


def test_tag_fills_identity_without_erasing_it(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = EventWriter(path, unit="u1", fw=None, dev=None, now_t=lambda: 0.0)
    events.tag("1.0", None)
    events.write(human_record(prompt="p", result="timeout", line=None))
    events.tag(None, "EE:FF")
    events.write(human_record(prompt="p", result="timeout", line=None))
    events.close()
    first, second = read(path)
    assert (first["fw"], first["dev"]) == ("1.0", None)
    assert (second["fw"], second["dev"]) == ("1.0", "EE:FF")


def test_concurrent_writers_get_unique_consecutive_seqs(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = writer(path)
    per_thread = 200

    def daemon_side() -> None:
        for n in range(per_thread):
            events.passthrough({"seq": n, "type": "tx", "offset": n})

    def runner_side() -> None:
        for _ in range(per_thread):
            events.write(human_record(prompt="p", result="timeout", line=None))

    threads = [threading.Thread(target=daemon_side), threading.Thread(target=runner_side)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events.close()
    assert [record["seq"] for record in read(path)] == list(range(1, 2 * per_thread + 1))


def test_non_ascii_lines_stay_readable(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = writer(path)
    events.write(human_record(prompt="p", result="confirmed", line="temp 21°C"))
    events.close()
    assert "21°C" in path.read_text(encoding="utf-8")


def test_a_reused_run_dir_starts_a_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq": 1, "type": "run"}\n{"seq": 2, "type": "run"}\n', encoding="utf-8")
    events = writer(path)
    events.write(human_record(prompt="p", result="timeout", line=None))
    events.close()
    assert [(record["seq"], record["type"]) for record in read(path)] == [(1, "human")]
