"""`results.md` (design Section 7.7): the run as a table that drops into a PR body.

A step's evidence is composed once, by the runner, with `code` and `at_offset`; the same
text is the step's `detail` in `summary.json` and `events.jsonl`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EVIDENCE_CHARS = 120
_ELLIPSIS = "…"
_TABLE_HEAD = (
    "| # | Step | Kind | Result | t (s) | Evidence |\n"
    "|---|------|------|--------|-------|----------|"
)


@dataclass(frozen=True, slots=True)
class ReportRow:
    index: int
    name: str
    kind: str
    result: str
    t: float | None  # seconds from the run's start to the step's end; None when not run
    evidence: str


def truncate(text: str, limit: int = EVIDENCE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + _ELLIPSIS


def _span(text: str) -> str:
    # A fence one backtick longer than any run inside the text, padded when the text touches
    # a backtick, is CommonMark's way to put backticks in a code span.
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if longest or not text else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def code(text: str) -> str:
    """`text` (a stored log line or a pattern) as an inline code span, truncated."""
    return _span(truncate(text))


def at_offset(text: str | None, offset: int | None) -> str:
    """Evidence for a stored line: the line as code and its log offset."""
    if text is None:
        return "" if offset is None else f"@ {offset}"
    return code(text) if offset is None else f"{code(text)} @ {offset}"


def _cell(text: str) -> str:
    return text.replace("\n", " ").replace("|", "\\|")


def _resets(label: str, steps: list[int | None]) -> str:
    known = sorted({step for step in steps if step is not None})
    if not known:
        return f"{len(steps)} {label}"
    noun = "step" if len(known) == 1 else "steps"
    return f"{len(steps)} {label} ({noun} {', '.join(map(str, known))})"


def render_results(
    *,
    scenario: str,
    result: str,
    unit: str,
    fw: str | None,
    date: str,
    rows: list[ReportRow],
    expected_resets: list[int | None],
    unexpected_resets: list[int | None],
    notes: list[str],
    run_dir: Path,
) -> str:
    """The whole `results.md`. Each reset is given as the step it fell in, None if none."""
    lines = [f"## {scenario} — {result} ({unit}, fw {fw or 'unknown'}, {date})", "", _TABLE_HEAD]
    for row in rows:
        t = "-" if row.t is None else f"{row.t:.1f}"
        lines.append(
            f"| {row.index} | {_cell(truncate(row.name))} | {row.kind} | {row.result} | {t} "
            f"| {_cell(row.evidence)} |"
        )
    lines.append("")
    if notes:
        lines += [f"- {note}" for note in notes] + [""]
    lines.append(
        f"Resets: {_resets('expected', expected_resets)}, "
        f"{_resets('unexpected', unexpected_resets)}. Run dir: {_span(str(run_dir))}."
    )
    return "\n".join(lines) + "\n"
