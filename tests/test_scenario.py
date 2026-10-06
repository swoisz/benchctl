"""Scenario TOML load and validation, and the `validate` verb's rows (design Section 7.8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchctl import errors
from benchctl.profile import ConfigError
from benchctl.scenario import Step, load_scenario, validate_paths
from conftest import EXAMPLE_PROFILE

EXAMPLES = EXAMPLE_PROFILE.parent
EXAMPLE_SCENARIOS = sorted((EXAMPLES / "scenarios").glob("*.toml"))
NAMED = 'name = "s"\n'


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def steps_of(tmp_path: Path, text: str) -> list[Step]:
    return load_scenario(write(tmp_path / "s.toml", NAMED + text)).steps


# --- the examples ------------------------------------------------------------------------


def test_the_three_example_scenarios_exist() -> None:
    names = [path.name for path in EXAMPLE_SCENARIOS]
    assert names == ["boot_banner.toml", "cmd_roundtrip.toml", "external_undo.toml"]


@pytest.mark.parametrize("path", EXAMPLE_SCENARIOS, ids=lambda path: path.stem)
def test_example_scenario_validates(path: Path) -> None:
    scenario = load_scenario(path)
    assert scenario.path == path
    assert scenario.steps
    assert scenario.budget is not None


def test_validate_examples_dir_checks_the_profile_and_every_scenario() -> None:
    rows = validate_paths([EXAMPLES])
    assert [(row["kind"], Path(str(row["path"])).name) for row in rows] == [
        ("profile", "profile.toml"),
        *[("scenario", path.name) for path in EXAMPLE_SCENARIOS],
    ]
    assert all(row["ok"] and row["errors"] == [] for row in rows), rows


def test_example_values_are_parsed() -> None:
    boot = load_scenario(EXAMPLES / "scenarios" / "boot_banner.toml")
    assert [step.kind for step in boot.steps] == ["reset", "wait", "wait"]
    assert boot.budget == 60.0
    assert (boot.steps[2].since, boot.steps[2].absent, boot.steps[2].window) == (
        "trigger",
        "rst:",
        5.0,
    )

    external = load_scenario(EXAMPLES / "scenarios" / "external_undo.toml")
    run, human, cmd = external.steps
    assert (run.undo, run.env, run.timeout) == (
        "rm -f benchctl-example.marker",
        {"BENCHCTL_EXAMPLE": "1"},
        10.0,
    )
    assert (human.trigger, human.expect_reset, human.timeout) == (True, True, 300.0)
    assert (cmd.at, cmd.trigger) == (1.0, False)


# --- parsed values -----------------------------------------------------------------------


def test_steps_are_numbered_from_one_and_parse_durations(tmp_path: Path) -> None:
    steps = steps_of(
        tmp_path,
        """
budget = "1.5h"
[preconditions]
env_gate = true
fw_version = '^1\\.7'

[[step]]
kind = "wait"
pattern = "ready"
timeout = "250ms"
window = 30
trigger = false

[[step]]
kind = "cmd"
command = "led"
args = { index = 3, state = "on" }
capture = true
at = "+15m"
""",
    )
    wait, cmd = steps
    assert (wait.index, wait.timeout, wait.window, wait.trigger, wait.since) == (
        1,
        0.25,
        30.0,
        False,
        "prev",
    )
    assert (cmd.index, cmd.at, cmd.args, cmd.capture, cmd.expect) == (
        2,
        900.0,
        {"index": "3", "state": "on"},
        True,
        None,
    )
    scenario = load_scenario(tmp_path / "s.toml")
    assert scenario.budget == 5400.0
    assert (scenario.preconditions.env_gate, scenario.preconditions.fw_version) == (
        True,
        r"^1\.7",
    )


def test_flash_steps_give_artifacts_resolve_specs(tmp_path: Path) -> None:
    by_dir, by_files = steps_of(
        tmp_path,
        """
[[step]]
kind = "flash"
artifacts = "build/debug"
only = ["app", "otadata"]
after = "no-reset"

[[step]]
kind = "flash"
files = [{ addr = "0x220000", file = "app.bin" }, { addr = 53248, file = "ota.bin" }]
""",
    )
    assert (by_dir.flash_specs, by_dir.only, by_dir.after) == (
        ["build/debug"],
        ["app", "otadata"],
        "no-reset",
    )
    assert by_files.files == [(0x220000, "app.bin"), (0xD000, "ota.bin")]
    assert by_files.flash_specs == ["0x220000=app.bin", "0xd000=ota.bin"]


def test_since_accepts_keywords_and_earlier_marks(tmp_path: Path) -> None:
    steps = steps_of(
        tmp_path,
        """
[[step]]
kind = "mark"
name = "m1"
"""
        + "".join(
            f'[[step]]\nkind = "wait"\npattern = "x"\nsince = "{since}"\n'
            for since in ("prev", "step", "trigger", "boot", "attach", "m1")
        ),
    )
    assert [step.since for step in steps[1:]] == ["prev", "step", "trigger", "boot", "attach", "m1"]
    assert steps[0].name == "m1"


# --- validation rules (Section 7.8), each path-qualified -----------------------------------


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("bogus = 1\n", "bogus: unknown key"),
        ("budget = '5 min'\n", "budget: invalid duration"),
        ("[preconditions]\nenv = true\n", "preconditions.env: unknown key"),
        ("[preconditions]\nfw_version = '('\n", "preconditions.fw_version: invalid regex"),
        ("[preconditions]\nenv_gate = 'yes'\n", "preconditions.env_gate: expected true or false"),
        ("[[step]]\nname = 'x'\n", "step[1].kind: required"),
        ("[[step]]\nkind = 'sleep'\n", "step[1].kind: must be one of"),
        ("[[step]]\nkind = 'reset'\nfrobnicate = 1\n", "step[1].frobnicate: unknown key"),
        ("[[step]]\nkind = 'cmd'\nsend = 'v'\npattern = 'x'\n", "step[1].pattern: unknown key"),
        ("[[step]]\nkind = 'cmd'\nsend = 'v'\ntrigger = true\n", "step[1].trigger: unknown key"),
        ("[[step]]\nkind = 'reset'\ntimeout = '5x'\n", "step[1].timeout: invalid duration"),
        ("[[step]]\nkind = 'reset'\ntimeout = true\n", "step[1].timeout: expected a duration"),
        ("[[step]]\nkind = 'reset'\nat = '5s'\n", 'step[1].at: expected "+<duration>"'),
        ("[[step]]\nkind = 'reset'\nexpect_reset = 1\n", "step[1].expect_reset: expected true"),
        ("[[step]]\nkind = 'human'\nconfirm = 'x'\n", "step[1].prompt: required"),
        ("[[step]]\nkind = 'human'\nprompt = 'p'\n", "step[1].confirm: required"),
        ("[[step]]\nkind = 'human'\nprompt = 'p'\nconfirm = '['\n", "step[1].confirm: invalid"),
        ("[[step]]\nkind = 'external'\nundo = 'u'\n", "step[1].run: required"),
        ("[[step]]\nkind = 'external'\nrun = 'r'\nenv = { A = 1 }\n", "step[1].env.A: expected"),
        ("[[step]]\nkind = 'cmd'\n", "step[1]: needs exactly one of send and command"),
        ("[[step]]\nkind = 'cmd'\nsend = 'a'\ncommand = 'b'\n", "step[1]: needs exactly one"),
        ("[[step]]\nkind = 'cmd'\nsend = 'a'\nargs = { k = 'v' }\n", "step[1].args: only applies"),
        (
            "[[step]]\nkind = 'cmd'\nsend = 'a'\ncapture = true\nexpect = 'OK'\n",
            "step[1]: capture and expect are exclusive",
        ),
        (
            "[[step]]\nkind = 'flash'\nfiles = [{ addr = 0, file = 'a' }]\nonly = ['app']\n",
            "step[1].only: only applies with artifacts",
        ),
        ("[[step]]\nkind = 'cmd'\nsend = 'a'\nerror = ['(']\n", "step[1].error[1]: invalid regex"),
        ("[[step]]\nkind = 'cmd'\nsend = 'a'\nerror = ['']\n", "step[1].error[1]: must not be"),
        ("[[step]]\nkind = 'wait'\ntimeout = '5s'\n", "step[1]: needs at least one of pattern"),
        ("[[step]]\nkind = 'wait'\npattern = '('\n", "step[1].pattern: invalid regex"),
        ("[[step]]\nkind = 'wait'\norder = []\n", "step[1].order: must not be empty"),
        ("[[step]]\nkind = 'wait'\npattern = 'x'\nsince = 'nope'\n", "step[1].since: unknown mark"),
        ("[[step]]\nkind = 'wait'\npattern = 'x'\nsince = '812'\n", "step[1].since: unknown mark"),
        ("[[step]]\nkind = 'flash'\n", "step[1]: needs exactly one of artifacts and files"),
        (
            "[[step]]\nkind = 'flash'\nartifacts = 'b'\nfiles = [{ addr = 0, file = 'a' }]\n",
            "step[1]: needs exactly one of artifacts and files",
        ),
        (
            "[[step]]\nkind = 'flash'\nfiles = [{ addr = 'app', file = 'a' }]\n",
            "step[1].files[1].addr: expected a flash address",
        ),
        ("[[step]]\nkind = 'flash'\nfiles = [{ addr = 0 }]\n", "step[1].files[1].file: required"),
        ("[[step]]\nkind = 'flash'\nfiles = []\n", "step[1].files: must list at least one"),
        ("[[step]]\nkind = 'flash'\nartifacts = ''\n", "step[1].artifacts: expected a build dir"),
        ("[[step]]\nkind = 'flash'\nartifacts = 'b'\nonly = ['nvs']\n", "step[1].only: unknown"),
        ("[[step]]\nkind = 'flash'\nartifacts = 'b'\nafter = 'x'\n", "step[1].after: must be"),
        ("[[step]]\nkind = 'mark'\n", "step[1].name: required"),
        ("[[step]]\nkind = 'mark'\nname = 'boot'\n", "step[1].name: 'boot' is a cursor keyword"),
        ("[[step]]\nkind = 'mark'\nname = '12'\n", "step[1].name: '12' is a cursor keyword"),
    ],
)
def test_rule_violation_is_path_qualified(tmp_path: Path, text: str, error: str) -> None:
    path = write(tmp_path / "s.toml", NAMED + text)
    with pytest.raises(ConfigError) as raised:
        load_scenario(path)
    found = raised.value.errors
    assert any(line.startswith(f"{path}: {error}") for line in found), found


def test_name_is_required(tmp_path: Path) -> None:
    path = write(tmp_path / "s.toml", "[[step]]\nkind = 'reset'\n")
    with pytest.raises(ConfigError) as raised:
        load_scenario(path)
    assert raised.value.errors == [f"{path}: name: required"]


def test_a_mark_must_come_before_the_wait_that_uses_it(tmp_path: Path) -> None:
    path = write(
        tmp_path / "s.toml",
        NAMED
        + "[[step]]\nkind = 'wait'\npattern = 'x'\nsince = 'later'\n"
        + "[[step]]\nkind = 'mark'\nname = 'later'\n",
    )
    with pytest.raises(ConfigError) as raised:
        load_scenario(path)
    assert [line.split(": ")[1] for line in raised.value.errors] == ["step[1].since"]


def test_every_step_problem_is_reported_in_one_pass(tmp_path: Path) -> None:
    path = write(
        tmp_path / "s.toml",
        "[[step]]\nkind = 'human'\n[[step]]\nkind = 'nope'\n",
    )
    with pytest.raises(errors.UsageError) as raised:
        load_scenario(path)
    assert raised.value.exit_code == 2
    assert sorted(raised.value.data["errors"]) == [
        f"{path}: name: required",
        f"{path}: step[1].confirm: required",
        f"{path}: step[1].prompt: required",
        f"{path}: step[2].kind: must be one of human, external, cmd, wait, reset, flash, mark; "
        "got 'nope'",
    ]


# --- validate_paths ----------------------------------------------------------------------


def test_validate_classifies_files_and_reports_errors(tmp_path: Path) -> None:
    units = write(tmp_path / "units.toml", "[units.u1.console.match]\nserial = 'x'\n")
    copy = write(tmp_path / "demo.toml", EXAMPLE_PROFILE.read_text(encoding="utf-8"))
    broken = write(tmp_path / "broken.toml", "name = 's'\n[[step]\n")
    bad = write(tmp_path / "bad.toml", "name = 's'\n[[step]]\nkind = 'mark'\n")
    missing = tmp_path / "missing.toml"

    rows = validate_paths([units, copy, broken, bad, missing])

    assert [(row["kind"], row["ok"]) for row in rows] == [
        ("units", True),
        ("profile", True),
        ("scenario", False),
        ("scenario", False),
        ("scenario", False),
    ]
    syntax_errors = rows[2]["errors"]
    assert isinstance(syntax_errors, list)
    assert len(syntax_errors) == 1
    assert str(syntax_errors[0]).startswith(f"{broken}: ")
    assert rows[3]["errors"] == [f"{bad}: step[1].name: required: the mark's name"]
    assert rows[4]["errors"] == ["not found"]


def test_validate_a_profile_dir_with_scenarios(tmp_path: Path) -> None:
    write(tmp_path / "demo" / "profile.toml", '[profile]\nname = "demo"\n')
    write(tmp_path / "demo" / "scenarios" / "b.toml", NAMED + "[[step]]\nkind = 'reset'\n")
    write(tmp_path / "demo" / "scenarios" / "a.toml", "[[step]]\nkind = 'reset'\n")
    write(tmp_path / "loose" / "one.toml", NAMED)
    (tmp_path / "empty").mkdir()

    rows = validate_paths([tmp_path / "demo", tmp_path / "loose", tmp_path / "empty"])

    assert [(Path(str(row["path"])).name, row["kind"], row["ok"]) for row in rows] == [
        ("profile.toml", "profile", True),
        ("a.toml", "scenario", False),
        ("b.toml", "scenario", True),
        ("one.toml", "scenario", True),
        ("empty", "profile", False),
    ]
