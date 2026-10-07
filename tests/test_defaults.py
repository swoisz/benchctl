"""The shared defaults module, chiefly that `iso_now` keeps the existing timestamp format."""

from __future__ import annotations

import re
import time
from datetime import datetime

import pytest

from benchctl.defaults import iso_now

# ISO 8601 with exactly three fractional digits and a numeric UTC offset.
ISO_MS_OFFSET = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d")
TS = 1_791_234_567.891


def test_iso_now_is_iso8601_with_ms_and_offset() -> None:
    text = iso_now()
    assert ISO_MS_OFFSET.fullmatch(text)
    parsed = datetime.fromisoformat(text)
    assert parsed.utcoffset() is not None
    assert abs(parsed.timestamp() - time.time()) < 1.0


def test_iso_now_formats_a_given_timestamp() -> None:
    expected = iso_now(TS)
    assert ISO_MS_OFFSET.fullmatch(expected)
    assert datetime.fromisoformat(expected).timestamp() == pytest.approx(TS, abs=0.001)
