"""The shared defaults module, chiefly that `iso_now` keeps the existing timestamp format."""

from __future__ import annotations

import re
import time
from datetime import datetime, tzinfo
from typing import Self

import pytest

from benchctl import events, lease, session
from benchctl.defaults import iso_now

# ISO 8601 with exactly three fractional digits and a numeric UTC offset.
ISO_MS_OFFSET = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d")
TS = 1_791_234_567.891


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(TS, tz)


def test_iso_now_is_iso8601_with_ms_and_offset() -> None:
    text = iso_now()
    assert ISO_MS_OFFSET.fullmatch(text)
    parsed = datetime.fromisoformat(text)
    assert parsed.utcoffset() is not None
    assert abs(parsed.timestamp() - time.time()) < 1.0


def test_iso_now_matches_the_existing_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = iso_now(TS)
    assert ISO_MS_OFFSET.fullmatch(expected)
    assert lease._iso(TS) == expected
    # The other helpers read the clock; freeze it at TS so the comparison is byte for byte.
    for module in (events, lease, session):
        monkeypatch.setattr(module, "datetime", _FrozenClock)
    assert events.host_timestamp() == expected
    assert lease.now_iso() == expected
    assert session._iso_now() == expected
