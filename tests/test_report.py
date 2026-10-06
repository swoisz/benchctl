"""results.md rendering (design Section 7.7)."""

from __future__ import annotations

from pathlib import Path

from benchctl.report import EVIDENCE_CHARS, ReportRow, at_offset, code, render_results

RUN_DIR = Path("/state/runs/u1/20261006T120000-restart-held-during-a-call")


def render(
    rows: list[ReportRow],
    *,
    result: str = "pass",
    fw: str | None = "1.7.4",
    expected: list[int | None] | None = None,
    unexpected: list[int | None] | None = None,
    notes: list[str] | None = None,
) -> str:
    return render_results(
        scenario="restart held during a call",
        result=result,
        unit="u1",
        fw=fw,
        date="2026-10-06",
        rows=rows,
        expected_resets=[] if expected is None else expected,
        unexpected_resets=[] if unexpected is None else unexpected,
        notes=[] if notes is None else notes,
        run_dir=RUN_DIR,
    )


def test_the_section_7_7_example_renders_verbatim() -> None:
    rows = [
        ReportRow(
            1,
            "place a call",
            "human",
            "pass",
            42.1,
            at_offset("ENTERED new state: CALL_ACTIVE", 812),
        ),
        ReportRow(2, "reboot rpc", "external", "pass", 47.3, "exit 0"),
        ReportRow(
            3, "mqtt_silent 600", "cmd", "pass", 53.0, at_offset("## tx mqtt_silent 600", 830)
        ),
        ReportRow(
            4,
            "held for the call",
            "wait",
            "pass",
            61.2,
            f"{at_offset('… held for the call', 877)}; no {code('rst:')} in 50 s",
        ),
    ]
    assert render(rows, expected=[2]) == (
        "## restart held during a call — pass (u1, fw 1.7.4, 2026-10-06)\n"
        "\n"
        "| # | Step | Kind | Result | t (s) | Evidence |\n"
        "|---|------|------|--------|-------|----------|\n"
        "| 1 | place a call | human | pass | 42.1 | `ENTERED new state: CALL_ACTIVE` @ 812 |\n"
        "| 2 | reboot rpc | external | pass | 47.3 | exit 0 |\n"
        "| 3 | mqtt_silent 600 | cmd | pass | 53.0 | `## tx mqtt_silent 600` @ 830 |\n"
        "| 4 | held for the call | wait | pass | 61.2 | `… held for the call` @ 877; "
        "no `rst:` in 50 s |\n"
        "\n"
        f"Resets: 1 expected (step 2), 0 unexpected. Run dir: `{RUN_DIR}`.\n"
    )


def test_evidence_lines_are_truncated_to_120_chars() -> None:
    long = "x" * 300
    span = code(long)
    assert span.startswith("`") and span.endswith("`")
    inner = span[1:-1]
    assert len(inner) == EVIDENCE_CHARS
    assert inner.endswith("…")
    assert code("y" * EVIDENCE_CHARS) == "`" + "y" * EVIDENCE_CHARS + "`"


def test_backticks_in_a_line_get_a_longer_fence() -> None:
    assert code("run `idf.py`") == "`` run `idf.py` ``"
    assert code("a ``b`` c") == "``` a ``b`` c ```"


def test_pipes_and_newlines_cannot_break_the_table() -> None:
    rows = [ReportRow(1, "a | b", "cmd", "fail", 1.0, at_offset("x | y", 3) + "; multi\nline")]
    row = render(rows, result="fail").splitlines()[4]
    assert row == "| 1 | a \\| b | cmd | fail | 1.0 | `x \\| y` @ 3; multi line |"


def test_skipped_steps_and_unknown_fw() -> None:
    rows = [ReportRow(2, "later", "mark", "skip", None, "not run: step 1 failed")]
    text = render(rows, result="fail", fw=None)
    assert text.startswith("## restart held during a call — fail (u1, fw unknown, 2026-10-06)\n")
    assert "| 2 | later | mark | skip | - | not run: step 1 failed |" in text


def test_resets_line_counts_and_names_steps() -> None:
    text = render([], expected=[2, 5, 5, None], unexpected=[4])
    assert "Resets: 4 expected (steps 2, 5), 1 unexpected (step 4)." in text
    assert "Resets: 0 expected, 0 unexpected." in render([])


def test_notes_sit_between_the_table_and_the_resets_line() -> None:
    text = render([], result="precondition_failed", notes=["Precondition failed: no ip"])
    assert text.endswith(
        "|---|------|------|--------|-------|----------|\n"
        "\n"
        "- Precondition failed: no ip\n"
        "\n"
        f"Resets: 0 expected, 0 unexpected. Run dir: `{RUN_DIR}`.\n"
    )


def test_long_step_names_are_truncated_too() -> None:
    rows = [ReportRow(1, "n" * 200, "human", "pass", 1.0, "")]
    row = render(rows).splitlines()[4]
    assert "n" * (EVIDENCE_CHARS - 1) + "…" in row
    assert "n" * EVIDENCE_CHARS not in row
