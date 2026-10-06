"""The one redaction choke point (design Section 4.8): every line is redacted before it is
stored, so nothing derived from stored lines can leak a secret."""

from __future__ import annotations

import re
from collections.abc import Iterable

from benchctl.errors import UsageError

MASK = "***"


class Redactor:
    """Masks profile `[[redact]]` patterns. With one capturing group a match becomes the group
    followed by `***`, wherever the group sat; with none the whole match becomes `***`."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns: list[re.Pattern[str]] = []
        for pattern in patterns:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise UsageError(f"redact pattern {pattern!r} does not compile: {exc}") from exc
            if compiled.groups > 1:
                raise UsageError(
                    f"redact pattern {pattern!r} has {compiled.groups} groups; at most 1 is kept"
                )
            self._patterns.append(compiled)

    def apply(self, text: str) -> str:
        for pattern in self._patterns:
            text = pattern.sub(_mask, text)
        return text


def _mask(match: re.Match[str]) -> str:
    whole = match[0]
    kept = ""
    hidden = whole
    if match.re.groups and match[1] is not None:
        kept = match[1]
        start, end = match.start(1) - match.start(), match.end(1) - match.start()
        hidden = whole[:start] + whole[end:]
    # Leaving an already-masked (or empty) match alone is what makes apply() idempotent
    # (Section 4.8): redacting redacted text changes nothing.
    if not hidden.strip("*"):
        return whole
    return kept + MASK
