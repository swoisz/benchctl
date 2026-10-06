"""The run directory's `events.jsonl` (design Section 7.5): the runner is its sole writer.

Daemon records pass through renumbered, with the daemon's own number kept as `daemon_seq`;
the runner's `run`, `step`, `human` and `external` records are stamped here.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from benchctl.types import Event, Offset

_T_DECIMALS = 3


def host_timestamp() -> str:
    """Host time as events carry it: ISO 8601 with the UTC offset, millisecond precision."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def run_record(
    *,
    phase: str,
    scenario: str,
    run_dir: str,
    result: str | None,
    budget: dict[str, float | None],
) -> Event:
    return {
        "type": "run",
        "phase": phase,
        "scenario": scenario,
        "run_dir": run_dir,
        "result": result,
        "budget": dict(budget),
    }


def step_record(
    *, index: int, name: str, kind: str, result: str, duration: float, detail: str
) -> Event:
    return {
        "type": "step",
        "index": index,
        "name": name,
        "kind": kind,
        "result": result,
        "duration": round(duration, _T_DECIMALS),
        "detail": detail,
    }


def human_record(*, prompt: str, result: str, line: str | None) -> Event:
    return {"type": "human", "prompt": prompt, "result": result, "line": line}


def external_record(
    *,
    phase: str,
    cmd: str,
    exit: int | None,
    duration: float,
    stdout_tail: str,
    stderr_tail: str,
) -> Event:
    return {
        "type": "external",
        "phase": phase,
        "cmd": cmd,
        "exit": exit,
        "duration": round(duration, _T_DECIMALS),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


class EventWriter:
    """Writes one `events.jsonl`, numbering records in write order from 1.

    Thread-safe: the runner writes its own records while its tail thread passes daemon
    records through. `now_t` gives seconds since attach for the runner's records.
    """

    def __init__(
        self,
        path: Path,
        *,
        unit: str,
        fw: str | None,
        dev: str | None,
        now_t: Callable[[], float],
    ) -> None:
        # Truncated: a reused --run-dir must not hold two runs whose seqs both start at 1.
        self._file = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self._seq = 0
        self._unit = unit
        self._fw = fw
        self._dev = dev
        self._now_t = now_t

    def tag(self, fw: str | None, dev: str | None) -> None:
        """Identity learned after the writer was opened; None keeps the current value."""
        with self._lock:
            self._fw = fw or self._fw
            self._dev = dev or self._dev

    def write(self, record: Event, *, offset: Offset | None = None) -> Event:
        """Stamp a runner record (from the builders above) with the common fields and write it."""
        with self._lock:
            self._seq += 1
            stamped: Event = {
                "seq": self._seq,
                "ts": host_timestamp(),
                "t": round(self._now_t(), _T_DECIMALS),
                "unit": self._unit,
                "fw": self._fw,
                "dev": self._dev,
                "type": record["type"],
                "offset": offset,
            }
            stamped.update((key, value) for key, value in record.items() if key != "type")
            self._append(stamped)
        return stamped

    def passthrough(self, event: Event) -> Event:
        """Write a daemon record renumbered into this file; its own number moves to
        `daemon_seq`. Its identity tags become the ones runner records carry."""
        with self._lock:
            self._seq += 1
            record = dict(event)
            record["daemon_seq"] = event.get("seq")
            record["seq"] = self._seq
            self._fw = record.get("fw") or self._fw
            self._dev = record.get("dev") or self._dev
            self._append(record)
        return record

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def _append(self, record: Event) -> None:
        # Flushed per record so a killed run still leaves every event written so far.
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()
