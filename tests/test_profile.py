"""Profile TOML load, validation, lookup and render (design Section 7.3)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from benchctl import errors
from benchctl.idf import DEFAULT_SHA_PATTERN
from benchctl.profile import (
    Command,
    ConfigError,
    Profile,
    load_profile,
    parse_profile,
    placeholders,
    read_profile,
    render,
)
from conftest import EXAMPLE_PROFILE, PROFILE_NAME, SIP_PASS_REDACT, TIMESCALE
from fakedev import PROMPT

MINIMAL = '[profile]\nname = "p"\n'


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def errors_of(tmp_path: Path, text: str) -> tuple[Path, list[str]]:
    path = write(tmp_path / "profile.toml", text)
    with pytest.raises(ConfigError) as raised:
        read_profile(path)
    return path, raised.value.errors


# --- the example and the fixture ----------------------------------------------------------


def test_example_profile_validates() -> None:
    profile = read_profile(EXAMPLE_PROFILE)

    assert profile.name == "esp-idf"
    assert profile.path == EXAMPLE_PROFILE
    assert profile.flash.chip == "esp32s3"
    assert profile.console.prompt is not None
    assert profile.boot.ready == re.escape(profile.console.prompt)
    assert profile.boot.sha_pattern == DEFAULT_SHA_PATTERN
    assert {field.name for field in profile.identity} >= {"fw_version"}
    assert profile.command("restart").reboots
    assert profile.env_gate is not None


def test_profile_fixture_loads_the_installed_example(profile: Profile) -> None:
    # install_profile writes nested tables through dump_toml and scales the boot timings.
    assert profile.name == PROFILE_NAME
    assert profile.console.prompt == PROMPT
    assert profile.boot.ready == re.escape(PROMPT)
    assert profile.boot.settle == pytest.approx(2.5 * TIMESCALE)
    assert profile.boot.probe_interval == pytest.approx(1.0 * TIMESCALE)
    assert SIP_PASS_REDACT in [rule.pattern for rule in profile.redact]


def test_nested_table_form_and_float_durations_load(tmp_path: Path) -> None:
    profile = parse_profile(
        MINIMAL
        + '[console]\nprompt = "> "\ntimeout = 2\n'
        + "[boot]\nsettle = 0.125\nready_timeout = 1\n"
        + '[commands.ver]\nsend = "version"\ncapture = true\n'
        + '[[identity.field]]\nname = "fw"\nsource = "boot"\nregex = "v(\\\\S+)"\n',
        tmp_path / "profile.toml",
    )
    assert profile.console.timeout == 2.0
    assert profile.boot.settle == 0.125
    assert profile.boot.ready_timeout == 1.0
    assert profile.identity[0].regex == r"v(\S+)"


# --- hail's config and render cases, ported -----------------------------------------------


def test_config_parses_with_defaults(tmp_path: Path) -> None:
    # hail's [device] is benchctl's [console]; hail has no [profile] name and no
    # serial-framing keys here.
    profile = parse_profile(
        MINIMAL + '[console]\nbaud = 9600\n[commands.foo]\nsend = "do {a}"\nexpect = "OK"\n',
        tmp_path / "profile.toml",
    )
    assert profile.console.baud == 9600
    assert profile.console.line_ending == "\r\n"
    assert profile.commands["foo"].send == "do {a}"
    assert profile.commands["foo"].expect == "OK"


def test_config_parses_capture_command(tmp_path: Path) -> None:
    profile = parse_profile(
        MINIMAL + '[console]\nprompt = "demo> "\n'
        '[commands.ver]\nsend = "version"\ncapture = true\n',
        tmp_path / "profile.toml",
    )
    command = profile.commands["ver"]
    assert command.capture
    assert command.expect is None


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ('expect = "OK"', None),
        ('expect = "OK"\ncapture = true', "commands.x: capture and expect are exclusive"),
        ('expect = ""', "commands.x.expect: must not be empty"),
    ],
)
def test_validate_command_rejects_conflicts(tmp_path: Path, entry: str, error: str | None) -> None:
    text = MINIMAL + '[console]\nprompt = "> "\n[commands.x]\nsend = "x"\n' + entry + "\n"
    path = write(tmp_path / "profile.toml", text)
    if error is None:
        assert read_profile(path).commands["x"].expect == "OK"
        return
    with pytest.raises(ConfigError) as raised:
        read_profile(path)
    assert f"{path}: {error}" in "\n".join(raised.value.errors)


def test_render_fills_and_validates() -> None:
    args = {"hour": "14", "minute": "30"}
    assert render("set_clock -h {hour} -m {minute}", args) == "set_clock -h 14 -m 30"

    with pytest.raises(errors.UsageError, match="missing argument 'minute='"):
        render("x {hour} {minute}", {"hour": "1"})

    with pytest.raises(errors.UsageError, match="unknown argument"):
        render("set {hour} {minute}", {**args, "typo": "9"})


def test_render_rejects_unclosed_brace_and_lists_placeholders() -> None:
    with pytest.raises(errors.UsageError, match="unclosed"):
        render("led {index", {"index": "1"})
    assert placeholders("led {index} {state}") == ["index", "state"]
    assert render("no args", {}) == "no args"
    assert render("brace } alone", {}) == "brace } alone"


HAIL_EXAMPLE_MAP = r"""
[console]
baud = 115200
prompt = "> "
line_ending = "\r\n"
timeout = 5.0
tx_chunk = 64
tx_chunk_delay_ms = 10

[commands.version]
description = "Print the firmware version."
send = "version"
expect = "fw "
error = ["ERROR"]

[commands.set-led]
description = "Turn an LED on or off."
send = "led {index} {state}"
expect = "OK"
error = ["ERROR"]

[commands.status]
description = "Dump status."
send = "status"
capture = true

[commands.reset]
description = "Reboot the device."
send = "reset"
"""


def test_hail_command_map_loads_unchanged(tmp_path: Path) -> None:
    profile = parse_profile(MINIMAL + HAIL_EXAMPLE_MAP, tmp_path / "profile.toml")
    assert profile.console.prompt == "> "
    assert profile.command("set-led").render({"index": "3", "state": "on"}) == "led 3 on"
    assert profile.commands["version"] == Command(
        name="version",
        send="version",
        description="Print the firmware version.",
        expect="fw ",
        error=["ERROR"],
    )
    assert profile.commands["status"].capture
    reset = profile.commands["reset"]
    assert (reset.expect, reset.capture, reset.reboots) == (None, False, False)


# --- validation rules (Section 7.3), each path-qualified -----------------------------------


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[bogus]\nx = 1\n", "bogus: unknown key"),
        ("[console]\nbaudrate = 9600\n", "console.baudrate: unknown key"),
        ('[commands.v]\nsend = "v"\nexpects = "OK"\n', "commands.v.expects: unknown key"),
        ('[[identity.field]]\nname = "a"\n', "identity.field[1].source: required"),
        ('[commands.v]\nsend = "v"\ncapture = true\n', "commands.v.capture: needs console.prompt"),
        ('[commands.v]\ndescription = "no send"\n', "commands.v.send: required"),
        ('[commands.v]\nsend = "led {index"\n', "commands.v.send: unclosed '{'"),
        (
            '[[redact]]\nname = "r"\npattern = "(unclosed"\n',
            "redact[1].pattern: redact pattern '(unclosed' does not",
        ),
        (
            '[[redact]]\nname = "r"\npattern = "(a)(b)"\n',
            "redact[1].pattern: redact pattern '(a)(b)' has 2",
        ),
        ('[[redact]]\npattern = "a"\n', "redact[1].name: required"),
        (
            "[boot]\nreset_patterns = ['rst:(?P<code>\\d+)']\n",
            "boot.reset_patterns[1]: reset pattern",
        ),
        (
            "[boot]\nsha_pattern = 'SHA (\\w+)'\n",
            "boot.sha_pattern: sha pattern 'SHA (",
        ),
        ('[boot]\nready = "["\n', "boot.ready: invalid regex"),
        ("[boot]\nms_regression = 1000.0\n", "boot.ms_regression: expected an integer"),
        ('[commands.v]\nsend = "v"\nerror = ["ERR", ""]\n', "commands.v.error[2]: must not be"),
        ('[boot]\nsettle = "2.5s"\n', "boot.settle: expected seconds as a number, got a string"),
        ("[console]\ntimeout = true\n", "console.timeout: expected seconds as a number"),
        ("[boot]\nready_timeout = 0\n", "boot.ready_timeout: must be greater than 0"),
        ("[boot]\nsettle = -1.0\n", "boot.settle: must not be negative"),
        ("[console]\nbaud = 115200.0\n", "console.baud: expected an integer, got a float"),
        ('[console]\nprompt = ""\n', "console.prompt: must not be empty"),
        (
            "[[identity.field]]\nname = 'a'\nsource = 'uart'\nregex = '(x)'\n",
            "identity.field[1].source: must be one of cmd, boot",
        ),
        (
            "[[identity.field]]\nname = 'a'\nsource = 'cmd'\nregex = '(x)'\n",
            "identity.field[1].send: required when source",
        ),
        (
            "[[identity.field]]\nname = 'a'\nsource = 'cmd'\nsend = 'v'\nregex = 'x'\n",
            "identity.field[1].regex: needs a capture group",
        ),
        (
            "[[identity.field]]\nname = 'a'\nsource = 'boot'\nsend = 'v'\nregex = '(x)'\n",
            "identity.field[1].send: only applies",
        ),
        ("[[deny]]\npattern = '^reboot'\n", "deny[1].reason: required"),
        ("[[deny]]\npattern = '('\nreason = 'r'\n", "deny[1].pattern: invalid regex"),
        ('[flash]\nafter = "soft-reset"\n', "flash.after: must be one of hard-reset, no-reset"),
        ('[flash]\nbefore = "reset"\n', "flash.before: must be one of default-reset"),
        ("[env_gate]\nrequire = ['(']\n", "env_gate.require[1]: invalid regex"),
        ("[env_gate]\nforbid = ['']\n", "env_gate.forbid[1]: must not be empty"),
        ("[env_gate]\nforbid = 'x'\n", "env_gate.forbid: expected an array of strings"),
    ],
)
def test_rule_violation_is_path_qualified(tmp_path: Path, text: str, error: str) -> None:
    path, found = errors_of(tmp_path, MINIMAL + text)
    assert any(line.startswith(f"{path}: {error}") for line in found), found


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('commands = "v"\n', "commands: expected a table, got a string"),
        ('deny = "x"\n', "deny: expected an array of tables, got a string"),
    ],
)
def test_top_level_type_is_path_qualified(tmp_path: Path, text: str, error: str) -> None:
    path, found = errors_of(tmp_path, text + MINIMAL)
    assert f"{path}: {error}" in found


def test_profile_table_and_name_are_required(tmp_path: Path) -> None:
    path, found = errors_of(tmp_path, "[console]\nbaud = 9600\n")
    assert f"{path}: profile: required" in found
    path, found = errors_of(tmp_path, '[profile]\nchip = "esp32"\n')
    assert f"{path}: profile.name: required" in found


def test_every_problem_is_reported_in_one_pass(tmp_path: Path) -> None:
    path, found = errors_of(tmp_path, MINIMAL + "[console]\nbaud = 'x'\nbogus = 1\n")
    assert sorted(found) == [
        f"{path}: console.baud: expected an integer, got a string",
        f"{path}: console.bogus: unknown key",
    ]


def test_config_error_is_a_usage_error(tmp_path: Path) -> None:
    path = write(tmp_path / "profile.toml", MINIMAL + "[bogus]\n")
    with pytest.raises(errors.UsageError) as raised:
        read_profile(path)
    assert raised.value.exit_code == 2
    assert raised.value.data == {"errors": [f"{path}: bogus: unknown key"]}


def test_toml_syntax_error_names_the_file(tmp_path: Path) -> None:
    path, found = errors_of(tmp_path, "[profile\nname = 'p'\n")
    assert len(found) == 1
    assert found[0].startswith(f"{path}: ")


# --- lookup ------------------------------------------------------------------------------


def profile_text(name: str) -> str:
    return f'[profile]\nname = "{name}"\n'


def test_lookup_by_path_to_dir_or_file(tmp_path: Path, config_dir: Path) -> None:
    write(tmp_path / "proj" / "prof" / "profile.toml", profile_text("by-path"))
    cwd = tmp_path / "proj"

    assert load_profile("prof", cwd, config_dir).name == "by-path"
    assert load_profile("./prof/profile.toml", cwd, config_dir).name == "by-path"
    absolute = str(cwd / "prof")
    assert load_profile(absolute, tmp_path, config_dir).name == "by-path"
    with pytest.raises(errors.UsageError, match="no profile at"):
        load_profile("./missing/profile.toml", cwd, config_dir)


def test_lookup_by_name_in_the_config_dir(tmp_path: Path, config_dir: Path) -> None:
    # The file's name need not equal its directory's.
    write(config_dir / "profiles" / "bench" / "profile.toml", profile_text("other"))
    assert load_profile("bench", tmp_path, config_dir).name == "other"
    with pytest.raises(errors.UsageError, match="'absent' not found"):
        load_profile("absent", tmp_path, config_dir)


def test_local_profile_is_found_walking_up(tmp_path: Path, config_dir: Path) -> None:
    write(tmp_path / "repo" / ".benchctl" / "profile.toml", profile_text("local"))
    write(config_dir / "profiles" / "local" / "profile.toml", profile_text("installed"))
    write(config_dir / "profiles" / "elsewhere" / "profile.toml", profile_text("elsewhere"))
    cwd = tmp_path / "repo" / "src" / "deep"
    cwd.mkdir(parents=True)

    assert load_profile(None, cwd, config_dir).name == "local"
    # The project's own profile wins over the installed copy of the same name...
    assert load_profile("local", cwd, config_dir).path.parent.name == ".benchctl"
    # ...but does not shadow a different profile.
    assert load_profile("elsewhere", cwd, config_dir).name == "elsewhere"


def test_broken_local_profile_does_not_hide_a_named_one(tmp_path: Path, config_dir: Path) -> None:
    write(tmp_path / "repo" / ".benchctl" / "profile.toml", "[profile\nname = 'local'\n")
    write(config_dir / "profiles" / "elsewhere" / "profile.toml", profile_text("elsewhere"))
    cwd = tmp_path / "repo"

    assert load_profile("elsewhere", cwd, config_dir).name == "elsewhere"
    with pytest.raises(ConfigError):
        load_profile(None, cwd, config_dir)


def test_invalid_local_profile_with_the_requested_name_is_reported(
    tmp_path: Path, config_dir: Path
) -> None:
    write(tmp_path / "repo" / ".benchctl" / "profile.toml", profile_text("local") + "bogus = 1\n")
    write(config_dir / "profiles" / "local" / "profile.toml", profile_text("local"))
    with pytest.raises(ConfigError, match=r"profile\.bogus: unknown key"):
        load_profile("local", tmp_path / "repo", config_dir)


def test_lookup_without_any_profile_is_a_usage_error(tmp_path: Path, config_dir: Path) -> None:
    with pytest.raises(errors.UsageError, match="no profile"):
        load_profile(None, tmp_path, config_dir)
    with pytest.raises(errors.UsageError, match="invalid profile name"):
        load_profile("bad name", tmp_path, config_dir)


# --- lookups on a loaded profile -----------------------------------------------------------


def test_command_lookup_and_deny_list(tmp_path: Path) -> None:
    profile = parse_profile(
        MINIMAL
        + '[commands.v]\nsend = "version"\n'
        + "[[deny]]\npattern = '^sip disable\\b'\nreason = 'crashes the console'\n",
        tmp_path / "profile.toml",
    )
    assert profile.command("v").send == "version"
    with pytest.raises(errors.UsageError, match="unknown command 'w'"):
        profile.command("w")

    profile.check_allowed("sip status")
    with pytest.raises(errors.UsageError, match=r"^denied: crashes the console$") as raised:
        profile.check_allowed("sip disable")
    assert raised.value.exit_code == 2


def test_command_markers_convert_to_regexes_with_hail_semantics() -> None:
    command = Command(name="t", send="t", expect="OK.", error=["FAIL(", "E*"])
    assert command.expect_regex is not None
    assert re.search(command.expect_regex, "OKx") is None
    assert re.search(command.expect_regex, "  OK. done") is not None
    assert re.search(command.expect_regex, "not OK.") is None
    assert command.error_regexes == [re.escape("FAIL("), re.escape("E*")]
    assert re.search(command.error_regexes[0], "self-test FAIL(3)") is not None
    assert Command(name="c", send="c", capture=True).expect_regex is None


def test_flash_overrides_use_hyphen_names(tmp_path: Path) -> None:
    profile = parse_profile(
        MINIMAL + '[flash]\nflash_size = "8MB"\nflash_freq = "40m"\n',
        tmp_path / "profile.toml",
    )
    assert profile.flash.overrides == {"flash-size": "8MB", "flash-freq": "40m"}
    assert (profile.flash.baud, profile.flash.before, profile.flash.after) == (
        460800,
        "default-reset",
        "hard-reset",
    )
    assert profile.flash.chip is None
    assert profile.env_gate is None
