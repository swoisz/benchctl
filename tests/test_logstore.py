"""LogStore offsets, serial.log format, marks and cursors (design Section 4.7)."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from benchctl.errors import UsageError
from benchctl.logstore import LogStore, format_line

SERIAL_LOG_LINE = re.compile(r"\d\d:\d\d:\d\d\.\d{3} \+\d{5}\.\d{3} (.*)")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LogStore]:
    opened = LogStore(tmp_path / "serial.log")
    yield opened
    opened.close()


def test_offsets_count_every_line_from_zero(store: LogStore) -> None:
    assert store.next_offset == 0
    first = store.append("attach unit=u1", meta=True)
    second = store.append("I (24) boot: hello")
    assert (first.offset, first.meta, first.text) == (0, True, "## attach unit=u1")
    assert (second.offset, second.meta) == (1, False)
    assert store.next_offset == 2
    assert first.t <= second.t
    assert [line.offset for line in store.lines(0)] == [0, 1]
    assert [line.offset for line in store.lines(1, 2)] == [1]
    assert store.lines(5) == []


def test_serial_log_is_flushed_per_line_in_the_documented_format(
    store: LogStore, tmp_path: Path
) -> None:
    store.append("attach unit=u1", meta=True)
    store.append("rst:0x1 (POWERON)")
    rows = (tmp_path / "serial.log").read_text(encoding="utf-8").splitlines()
    assert [SERIAL_LOG_LINE.fullmatch(row) is not None for row in rows] == [True, True]
    assert rows[1].endswith(" rst:0x1 (POWERON)")
    assert rows[0] == format_line(store.lines(0)[0])
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}", store.lines(0)[0].ts)


def test_a_meta_line_stays_one_line(store: LogStore, tmp_path: Path) -> None:
    store.append("port lost error=a\nb\r\nc", meta=True)
    assert store.lines(0)[0].text == "## port lost error=a b c"
    assert len((tmp_path / "serial.log").read_text(encoding="utf-8").splitlines()) == 1


def test_cursors_resolve_marks_boot_attach_and_offsets(store: LogStore) -> None:
    for text in ("a", "b", "c"):
        store.append(text)
    store.boot = 2
    store.set_mark("here", 1)
    assert store.resolve("attach") == 0
    assert store.resolve("boot") == 2
    assert store.resolve("7") == 7
    assert store.resolve("here") == 1
    with pytest.raises(UsageError, match="unknown mark 'nope'"):
        store.resolve("nope")
    with pytest.raises(UsageError):
        store.resolve("-1")


def test_before_gives_context_lines(store: LogStore) -> None:
    for n in range(30):
        store.append(f"line {n}")
    assert [line.text for line in store.before(25, 3)] == ["line 22", "line 23", "line 24"]
    assert [line.text for line in store.before(1, 20)] == ["line 0"]


def test_tail_changes_notify_waiters(store: LogStore) -> None:
    assert store.set_tail("demo> ")
    assert not store.set_tail("demo> ")
    seen: list[str] = []

    def waiter() -> None:
        with store.cond:
            store.cond.wait_for(lambda: store.tail == "", timeout=1.0)
            seen.append(store.tail)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.02)
    store.set_tail("")
    thread.join(1.0)
    assert seen == [""]


def test_close_is_idempotent_and_keeps_memory(store: LogStore) -> None:
    store.append("x")
    store.sync()
    store.close()
    store.close()
    store.append("after close")
    assert [line.text for line in store.lines(0)] == ["x", "after close"]
