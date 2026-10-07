"""ESP-IDF console conventions: `L (ms) tag: msg` lines, ROM `rst:` lines, the ELF SHA line,
and resets inferred from a device-ms regression (design Section 4.4)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from benchctl.errors import UsageError

# The boot group stops at the name: download-mode lines nest parentheses
# (`boot:0x0 (DOWNLOAD(USB/UART0))`), which a closing `\)` would reject.
DEFAULT_RESET_PATTERN = (
    r"rst:0x(?P<code>[0-9a-fA-F]+) \((?P<reason>[A-Z_0-9]+)\)"
    r"(?:,boot:0x[0-9a-fA-F]+ \((?P<boot>[A-Z_0-9]+))?"
)
DEFAULT_SHA_PATTERN = r"ELF file SHA256:\s*(?P<sha>[0-9a-fA-F]+)"
DEFAULT_MS_REGRESSION = 1000
UNKNOWN_REASON = "unknown"

_IDF_LINE = re.compile(r"^([IWEDV]) \((\d+)\) ([^:]+): (.*)$")
_HEX = 16


@dataclass(frozen=True, slots=True)
class IdfLine:
    level: str
    device_ms: int
    tag: str
    msg: str


@dataclass(frozen=True, slots=True)
class ResetMatch:
    code: int | None
    reason: str
    boot_mode: str | None
    inferred: bool  # from a device-ms regression; no reset line was seen


@dataclass(frozen=True, slots=True)
class Detection:
    """What one stored line revealed. A missed reset can surface on the SHA line itself; the
    session keeps the first SHA seen for each reset."""

    reset: ResetMatch | None
    elf_sha: str | None


def parse_idf_line(text: str) -> IdfLine | None:
    match = _IDF_LINE.match(text)
    if match is None:
        return None
    level, device_ms, tag, msg = match.groups()
    return IdfLine(level, int(device_ms), tag, msg)


def _compile(pattern: str, required_group: str, what: str) -> re.Pattern[str]:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise UsageError(f"{what} {pattern!r} does not compile: {exc}") from exc
    if required_group not in compiled.groupindex:
        raise UsageError(f"{what} {pattern!r} has no (?P<{required_group}>…) group")
    return compiled


def compile_reset_patterns(extra: Sequence[str] = ()) -> list[re.Pattern[str]]:
    """The default `rst:` pattern followed by `extra`. Raises UsageError for a pattern that
    does not compile or lacks a `reason` group."""
    return [_compile(p, "reason", "reset pattern") for p in (DEFAULT_RESET_PATTERN, *extra)]


def compile_sha_pattern(pattern: str = DEFAULT_SHA_PATTERN) -> re.Pattern[str]:
    """Raises UsageError for a pattern that does not compile or lacks a `sha` group."""
    return _compile(pattern, "sha", "sha pattern")


def _parse_code(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text, _HEX)
    except ValueError:
        return None


class IdfDetector:
    """Scans stored lines in order for resets and the ELF SHA. Stateful: ms-regression
    inference compares each IDF line with the previous one since the last reset."""

    def __init__(
        self,
        reset_patterns: Sequence[str] = (),
        sha_pattern: str = DEFAULT_SHA_PATTERN,
        ms_regression: int = DEFAULT_MS_REGRESSION,
    ) -> None:
        self._reset_patterns = compile_reset_patterns(reset_patterns)
        self._sha_pattern = compile_sha_pattern(sha_pattern)
        self._ms_regression = ms_regression
        self._last_ms: int | None = None

    def feed(self, text: str) -> Detection | None:
        reset = self._match_reset(text)
        idf = parse_idf_line(text)
        ms = None if idf is None else idf.device_ms
        if (
            reset is None
            and ms is not None
            and self._last_ms is not None
            # 0 turns inference off, for firmware whose buffered logs replay older timestamps.
            and self._ms_regression > 0
            and self._last_ms - ms > self._ms_regression
        ):
            reset = ResetMatch(None, UNKNOWN_REASON, None, inferred=True)
        if reset is not None and not reset.inferred:
            # The reset line may itself be IDF-shaped, but its ms belongs to the old boot; the
            # next IDF line starts the new one.
            self._last_ms = None
        elif ms is not None:
            self._last_ms = ms
        sha_match = self._sha_pattern.search(text)
        elf_sha = None if sha_match is None else sha_match["sha"]
        if reset is None and elf_sha is None:
            return None
        return Detection(reset, elf_sha)

    def _match_reset(self, text: str) -> ResetMatch | None:
        for pattern in self._reset_patterns:
            match = pattern.search(text)
            if match is not None:
                groups = match.groupdict()
                return ResetMatch(
                    _parse_code(groups.get("code")),
                    groups.get("reason") or UNKNOWN_REASON,
                    groups.get("boot"),
                    inferred=False,
                )
        return None
