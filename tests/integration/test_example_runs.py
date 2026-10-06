"""`benchctl run` of every example scenario against the fake (design Sections 5, 7.6, 7.7)."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import PROFILE_NAME, UNIT
from fakedev import APP_VERSION, FakeDevice
from integration.conftest import CLI_TIMEOUT_S, Bench, shell_lines

SCENARIOS = Path(__file__).resolve().parents[2] / "examples" / "scenarios"
RUN_FILES = {
    "scenario.toml",
    "profile.toml",
    "serial.log",
    "events.jsonl",
    "results.md",
    "summary.json",
}
TABLE_HEAD = "| # | Step | Kind | Result | t (s) | Evidence |"
# external_undo.toml's human step and the file its external step creates and undoes.
PRESS_RESET = "Press the unit's RESET button."
MARKER = "benchctl-example.marker"
_ROW = re.compile(r"\| (\d+) \| (.*?) \| (\w+) \| (\w+) \| ([\d.]+|-) \| (.*) \|")


@dataclass
class Run:
    data: dict[str, Any]
    stderr: list[str]
    marker_during_human: bool | None  # whether MARKER existed when the operator was asked

    @property
    def run_dir(self) -> Path:
        return Path(self.data["run_dir"])


def run_example(bench: Bench, device: FakeDevice, name: str) -> Run:
    """`benchctl run <example> --json`, playing the operator: when the run asks for the
    RESET button, reset the fake (the prompt follows the step's start mark)."""
    proc = subprocess.Popen(
        bench.argv("run", str(SCENARIOS / f"{name}.toml"), "--json"),
        cwd=bench.workdir,
        env=bench.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout: list[str] = []
    stderr: list[str] = []
    seen: list[bool] = []

    def operator() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr.append(line)
            if PRESS_RESET in line:
                seen.append((bench.workdir / MARKER).exists())
                device.reset()

    def collect() -> None:
        assert proc.stdout is not None
        stdout.append(proc.stdout.read())

    # One reader per pipe: communicate() would also drain stderr and could eat the prompt.
    readers = [threading.Thread(target=fn, daemon=True) for fn in (operator, collect)]
    with proc:
        for reader in readers:
            reader.start()
        try:
            proc.wait(timeout=CLI_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(f"run {name} ran past {CLI_TIMEOUT_S:g} s: {''.join(stderr)}")
        finally:
            for reader in readers:
                reader.join(CLI_TIMEOUT_S)
    out = "".join(stdout)
    bench.record(("run", name, "--json"), proc.returncode, out, "".join(stderr))
    assert proc.returncode == 0, (out, "".join(stderr))
    data: dict[str, Any] = json.loads(out)
    return Run(data, stderr, seen[0] if seen else None)


def results_rows(run_dir: Path) -> tuple[str, list[tuple[str, ...]], str]:
    """results.md as (header line, table rows, Resets line)."""
    lines = (run_dir / "results.md").read_text(encoding="utf-8").splitlines()
    assert lines[2] == TABLE_HEAD
    rows = [match.groups() for line in lines if (match := _ROW.fullmatch(line))]
    resets = next(line for line in lines if line.startswith("Resets: "))
    return lines[0], rows, resets


def check_run(
    bench: Bench, run: Run, name: str, scenario: str, kinds: list[str]
) -> list[dict[str, Any]]:
    """The checks every run shares: pass, the six files, a results.md row per step. Returns
    the run dir's events."""
    assert run.data["ok"] is True
    assert run.data["result"] == "pass"
    assert [step["result"] for step in run.data["steps"]] == ["pass"] * len(kinds)
    run_dir = run.run_dir
    assert run_dir.parent == bench.state / "runs" / UNIT
    assert {path.name for path in run_dir.iterdir()} == RUN_FILES

    header, rows, _ = results_rows(run_dir)
    assert header == f"## {scenario} — pass ({UNIT}, fw {APP_VERSION}, {date.today()})"
    assert [(row[0], row[2], row[3]) for row in rows] == [
        (str(index), kind, "pass") for index, kind in enumerate(kinds, start=1)
    ]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["result"] == "pass"
    assert summary["unit"] == UNIT
    assert (run_dir / "scenario.toml").read_bytes() == (SCENARIOS / f"{name}.toml").read_bytes()
    profile = bench.config / "profiles" / PROFILE_NAME / "profile.toml"
    assert (run_dir / "profile.toml").read_bytes() == profile.read_bytes()

    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    runs = [event for event in events if event["type"] == "run"]
    assert [(event["phase"], event["result"]) for event in runs] == [
        ("start", None),
        ("end", "pass"),
    ]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert (run_dir / "serial.log").read_text(encoding="utf-8")
    return events


def test_the_examples_are_the_three_covered_here() -> None:
    assert sorted(path.stem for path in SCENARIOS.glob("*.toml")) == [
        "boot_banner",
        "cmd_roundtrip",
        "external_undo",
    ]


def test_run_boot_banner(bench: Bench) -> None:
    device = bench.add_unit(UNIT)
    bench.cli("attach")
    run = run_example(bench, device, "boot_banner")
    check_run(bench, run, "boot_banner", "boot banner", ["reset", "wait", "wait"])
    _, rows, resets = results_rows(run.run_dir)
    assert rows[0][5].startswith("`rst:0x1 (POWERON)")
    assert "no `rst:` in 5 s" in rows[2][5]
    assert resets.startswith("Resets: 1 expected (step 1), 0 unexpected.")
    assert device.resets == ["poweron", "poweron"]  # attach, then the reset step


def test_run_cmd_roundtrip(bench: Bench) -> None:
    device = bench.add_unit(UNIT)
    bench.cli("attach")
    run = run_example(bench, device, "cmd_roundtrip")
    check_run(bench, run, "cmd_roundtrip", "command round trip", ["mark", "cmd", "cmd", "wait"])
    _, rows, _ = results_rows(run.run_dir)
    assert rows[1][5].startswith("`## tx version` @ ")
    assert shell_lines(device) == ["version", "version"]
    log = (run.run_dir / "serial.log").read_text(encoding="utf-8")
    assert "## mark before_version" in log
    assert log.count("## tx version") == 2


def test_run_external_undo_runs_the_undo(bench: Bench) -> None:
    device = bench.add_unit(UNIT)
    bench.cli("attach")
    run = run_example(bench, device, "external_undo")
    events = check_run(
        bench, run, "external_undo", "external step with undo", ["external", "human", "cmd"]
    )
    # Step 1 created the marker before the operator was asked; the undo removed it.
    assert run.marker_during_human is True
    assert not (bench.workdir / MARKER).exists()
    external = [
        (event["phase"], event["cmd"], event["exit"])
        for event in events
        if event["type"] == "external"
    ]
    assert external == [
        ("run", f"touch {MARKER}", 0),
        ("undo", f"rm -f {MARKER}", 0),
    ]
    human = next(event for event in events if event["type"] == "human")
    assert human["result"] == "confirmed"
    assert human["line"].startswith("rst:0x1 (POWERON)")
    _, _, resets = results_rows(run.run_dir)
    assert resets.startswith("Resets: 1 expected (step 2), 0 unexpected.")


@pytest.mark.xfail(
    strict=True,
    reason="_Run.run learns fw from a read-only `status` before its first mutating op; on a "
    "run that auto-attaches, Daemon._handle probes identity only for that op (the runner's "
    "`mark(None)`) and the runner never reads it again, so results.md says `fw unknown` and "
    "the runner's own records carry fw null",
)
def test_run_that_attaches_names_the_fw(bench: Bench) -> None:
    device = bench.add_unit(UNIT)
    run = run_example(bench, device, "cmd_roundtrip")  # no attach first: the run attaches
    assert run.data["result"] == "pass"
    header, _, _ = results_rows(run.run_dir)
    assert f"fw {APP_VERSION}" in header
    events = (run.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["fw"] == APP_VERSION for line in events)
