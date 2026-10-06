"""Scenario execution (design Section 5).

Scaffold stub: the signatures are the contract `daemon.py`/`cli.py` build against; U7
replaces this file wholesale.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from benchctl.types import DeviceClient

if TYPE_CHECKING:
    from benchctl.profile import Profile
    from benchctl.scenario import Scenario


@dataclass(frozen=True, slots=True)
class RunResult:
    run_dir: Path
    result: Literal["pass", "fail", "precondition_failed"]
    budget: dict[str, float | None]  # {"declared": ..., "computed": ...}
    steps: list[dict[str, object]]


def run_scenario(
    client: DeviceClient,
    scenario: Scenario,
    profile: Profile,
    *,
    run_dir: Path,
    variables: dict[str, str],
    skip_gate: bool,
    dry_run: bool,
    prompt_out: TextIO,
) -> RunResult:
    raise NotImplementedError("run_scenario is provided by U7")


def compute_budget(scenario: Scenario) -> float:
    """Worst-case seconds: step timeouts plus `at` offsets."""
    raise NotImplementedError("compute_budget is provided by U7")
