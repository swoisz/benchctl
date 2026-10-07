"""ESP-IDF line parsing, reset and ELF SHA detection, ms-regression inference."""

from __future__ import annotations

import pytest

from benchctl.errors import UsageError
from benchctl.idf import (
    DEFAULT_MS_REGRESSION,
    Detection,
    IdfDetector,
    IdfLine,
    ResetMatch,
    compile_reset_patterns,
    compile_sha_pattern,
    parse_idf_line,
)
from benchctl.linebuf import LineEngine
from fakedev import DEFAULT_BOOT, ELF_SHA_PREFIX, RST_DOWNLOAD, RST_POWERON, RST_SW

SHA_LINE = f"I (305) app_init: ELF file SHA256:  {ELF_SHA_PREFIX}"


def _lines(data: bytes) -> list[str]:
    return LineEngine().feed(data)


def _idf(ms: int) -> str:
    return f"I ({ms}) main: tick"


def test_parse_idf_line_fields() -> None:
    assert parse_idf_line(SHA_LINE) == IdfLine(
        "I", 305, "app_init", f"ELF file SHA256:  {ELF_SHA_PREFIX}"
    )


@pytest.mark.parametrize("level", ["I", "W", "E", "D", "V"])
def test_parse_idf_line_levels(level: str) -> None:
    parsed = parse_idf_line(f"{level} (7) tag: msg")
    assert parsed is not None
    assert parsed.level == level


def test_parse_idf_line_tag_stops_at_first_colon() -> None:
    assert parse_idf_line("E (10) wifi: reason: 201") == IdfLine("E", 10, "wifi", "reason: 201")


@pytest.mark.parametrize(
    "text",
    [
        "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
        "demo> version",
        "## tx version",
        "X (10) tag: msg",
        "I (12:34:56.789) tag: msg",
        " I (10) tag: msg",
    ],
)
def test_parse_idf_line_rejects_other_lines(text: str) -> None:
    assert parse_idf_line(text) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (RST_POWERON, ResetMatch(0x1, "POWERON", "SPI_FAST_FLASH_BOOT", inferred=False)),
        (RST_SW, ResetMatch(0xC, "RTC_SW_CPU_RST", "SPI_FAST_FLASH_BOOT", inferred=False)),
        (RST_DOWNLOAD, ResetMatch(0x1, "POWERON", "DOWNLOAD", inferred=False)),
        (
            b"rst:0x10 (RTCWDT_RTC_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n",
            ResetMatch(0x10, "RTCWDT_RTC_RESET", "SPI_FAST_FLASH_BOOT", inferred=False),
        ),
        (b"rst:0x3 (RTC_SW_SYS_RST)\r\n", ResetMatch(0x3, "RTC_SW_SYS_RST", None, inferred=False)),
        (
            b"\x00\x00garbage rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n",
            ResetMatch(0xC, "RTC_SW_CPU_RST", "SPI_FAST_FLASH_BOOT", inferred=False),
        ),
    ],
)
def test_reset_line_detected(raw: bytes, expected: ResetMatch) -> None:
    detector = IdfDetector()
    assert detector.feed(_lines(raw)[0]) == Detection(expected, None)


def test_profile_reset_pattern_adds_to_default() -> None:
    detector = IdfDetector(reset_patterns=[r"Rebooting \((?P<reason>\w+), code (?P<code>\w+)\)"])
    assert detector.feed("Rebooting (panic, code 0x1c)") == Detection(
        ResetMatch(0x1C, "panic", None, inferred=False), None
    )
    reset = detector.feed(_lines(RST_SW)[0])
    assert reset is not None
    assert reset.reset is not None
    assert reset.reset.reason == "RTC_SW_CPU_RST"


def test_profile_reset_pattern_without_code_or_boot() -> None:
    detector = IdfDetector(reset_patterns=[r"restart cause=(?P<reason>\w+)"])
    assert detector.feed("restart cause=ota") == Detection(
        ResetMatch(None, "ota", None, inferred=False), None
    )


@pytest.mark.parametrize("pattern", [r"rst:(\w+)", r"(?P<reason>"])
def test_reset_pattern_needs_reason_and_must_compile(pattern: str) -> None:
    with pytest.raises(UsageError):
        compile_reset_patterns([pattern])
    with pytest.raises(UsageError):
        IdfDetector(reset_patterns=[pattern])


def test_sha_line_detected() -> None:
    assert IdfDetector().feed(SHA_LINE) == Detection(None, ELF_SHA_PREFIX)


def test_profile_sha_pattern() -> None:
    detector = IdfDetector(sha_pattern=r"elf=(?P<sha>[0-9a-f]+)")
    assert detector.feed("app elf=abcdef12") == Detection(None, "abcdef12")
    assert detector.feed(SHA_LINE) is None


@pytest.mark.parametrize("pattern", [r"ELF file SHA256:\s*([0-9a-f]+)", r"(?P<sha>"])
def test_sha_pattern_needs_sha_group_and_must_compile(pattern: str) -> None:
    with pytest.raises(UsageError):
        compile_sha_pattern(pattern)


def test_ms_regression_infers_a_reset_only_beyond_the_threshold() -> None:
    inferred = Detection(ResetMatch(None, "unknown", None, inferred=True), None)

    at_threshold = IdfDetector()
    assert at_threshold.feed(_idf(5000)) is None
    assert at_threshold.feed(_idf(5000 - DEFAULT_MS_REGRESSION)) is None

    beyond = IdfDetector()
    assert beyond.feed(_idf(5000)) is None
    assert beyond.feed(_idf(5000 - DEFAULT_MS_REGRESSION - 1)) == inferred


def test_ms_regression_threshold_is_configurable() -> None:
    detector = IdfDetector(ms_regression=100)
    assert detector.feed(_idf(500)) is None
    detection = detector.feed(_idf(399))
    assert detection is not None
    assert detection.reset is not None
    assert detection.reset.inferred


def test_ms_regression_zero_disables_inference() -> None:
    detector = IdfDetector(ms_regression=0)
    assert detector.feed(_idf(5000)) is None
    assert detector.feed(_idf(10)) is None


def test_reset_line_clears_the_ms_baseline() -> None:
    detector = IdfDetector()
    assert detector.feed(_idf(90_000)) is None
    assert detector.feed(_lines(RST_SW)[0]) is not None
    assert detector.feed(_idf(24)) is None


def test_idf_shaped_reset_line_clears_the_ms_baseline() -> None:
    detector = IdfDetector(reset_patterns=[r"restart cause=(?P<reason>\w+)"])
    assert detector.feed("I (90000) app: restart cause=ota") == Detection(
        ResetMatch(None, "ota", None, inferred=False), None
    )
    assert detector.feed("I (100) boot: chip revision") is None


def test_baseline_follows_a_synthetic_reset() -> None:
    detector = IdfDetector()
    detector.feed(_idf(90_000))
    assert detector.feed(_idf(100)) is not None
    assert detector.feed(_idf(200)) is None
    assert detector.feed(_idf(150)) is None


def test_non_idf_lines_leave_the_baseline_alone() -> None:
    detector = IdfDetector()
    detector.feed(_idf(90_000))
    assert detector.feed("demo> version") is None
    assert detector.feed("## mark x") is None
    assert detector.feed(_idf(10)) is not None


def test_missed_reset_surfaces_on_the_sha_line() -> None:
    detector = IdfDetector()
    detector.feed(_idf(90_000))
    assert detector.feed(SHA_LINE) == Detection(
        ResetMatch(None, "unknown", None, inferred=True), ELF_SHA_PREFIX
    )


def test_fake_boot_yields_one_reset_and_the_sha() -> None:
    engine = LineEngine()
    detector = IdfDetector()

    def boot(with_rst: bool) -> list[Detection]:
        lines = engine.feed(RST_POWERON) if with_rst else []
        for _, chunk in DEFAULT_BOOT:
            lines += engine.feed(chunk)
        return [d for d in map(detector.feed, lines) if d is not None]

    poweron = ResetMatch(0x1, "POWERON", "SPI_FAST_FLASH_BOOT", inferred=False)
    assert boot(with_rst=True) == [Detection(poweron, None), Detection(None, ELF_SHA_PREFIX)]

    # A second boot whose rst: line was lost: the bootloader's low ms gives it away.
    inferred = ResetMatch(None, "unknown", None, inferred=True)
    assert boot(with_rst=False) == [Detection(inferred, None), Detection(None, ELF_SHA_PREFIX)]
