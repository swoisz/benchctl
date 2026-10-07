"""Every verb's `--json` shape and exit code (design Section 7.1) through the in-process CLI,
with the daemon spawned as `tests/fake_daemon.py` (Section 6.2, U5)."""

from __future__ import annotations

import json
import os
import pty
import shutil
import tempfile
import termios
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner, Result

from benchctl import capture, cli, flash, monitor, paths, runner
from benchctl.client import DAEMON_ARGV_ENV, Client, daemon_pid
from benchctl.lease import OWNER_ENV, PortLock
from benchctl.profile import load_profile
from benchctl.runner import RunResult
from benchctl.types import (
    CmdResult,
    DeviceClient,
    FlashFile,
    FlashResult,
    LogLine,
    Subscription,
)
from conftest import PROFILE_NAME, UNIT, install_profile
from fake_daemon import (
    APP_VERSION,
    FAKE_DAEMON_ENV,
    FAKE_MAC,
    daemon_argv,
    reap_daemons,
    register_unit,
)
from fakedev import FakeDevice

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
SCENARIO = EXAMPLES / "scenarios" / "boot_banner.toml"
OTHER_MAC = "11:22:33:44:55:66"
STATUS_KEYS = {
    "daemon",
    "profile",
    "lease",
    "identity",
    "last_reset",
    "offset",
    "gate",
    "session_dir",
}
RESET_KEYS = {
    "offset",
    "t",
    "code",
    "reason",
    "boot_mode",
    "elf_sha",
    "expected",
    "cause",
    "context",
}
LINE_KEYS = {"offset", "t", "ts", "text"}


@pytest.fixture
def bench(config_dir: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    install_profile(config_dir)
    register_unit(config_dir)
    monkeypatch.setenv(DAEMON_ARGV_ENV, daemon_argv())
    monkeypatch.setenv(FAKE_DAEMON_ENV, json.dumps({"progress_s": 0.1}))
    monkeypatch.setenv(OWNER_ENV, "alice")
    yield state_dir
    reap_daemons(state_dir)


def invoke(*args: str, env: Mapping[str, str] | None = None) -> Result:
    return CliRunner().invoke(cli.main, list(args), env=dict(env or {}))


def run_json(*args: str, code: int = 0, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    result = invoke(*args, "--json", env=env)
    assert result.exit_code == code, (result.stdout, result.stderr, result.exception)
    data: dict[str, Any] = json.loads(result.stdout)
    return data


def assert_error(data: dict[str, Any], code: str) -> None:
    assert data["ok"] is False
    assert data["error"]["code"] == code
    assert set(data["error"]) == {"code", "message", "data"}


def as_bob() -> dict[str, str]:
    return {OWNER_ENV: "bob"}


# --- verbs that never attach -------------------------------------------------------------


def test_status_units_validate_never_spawn(bench: Path) -> None:
    status = run_json("status")
    assert status == {"ok": True, "unit": UNIT, **dict.fromkeys(STATUS_KEYS)}
    units = run_json("units", env={"BENCHCTL_PORTS_FILE": str(bench / "none.json")}, code=4)
    assert_error(units, "device")
    run_json("validate", str(EXAMPLES))
    assert invoke("status").stdout.strip() == f"{UNIT}: not attached"
    assert not paths.pid_path(UNIT).exists()
    assert not paths.socket_path(UNIT).exists()


def test_units_reports_port_candidates_daemon_and_lease(
    bench: Path, registry: Path, fakedev: FakeDevice
) -> None:
    data = run_json("units")
    assert data["units"] == [
        {
            "unit": UNIT,
            "profile": "esp-idf",
            "port": fakedev.path,
            "candidates": 1,
            "daemon": False,
            "lease": None,
        }
    ]
    run_json("mark", "x")  # attaches (the fake session ignores the port) and takes the lease
    row = run_json("units")["units"][0]
    assert row["daemon"] is True
    assert set(row["lease"]) == {"owner", "since", "last_seen"}
    assert row["lease"]["owner"] == "alice"
    run_json("detach")


def test_validate_reports_every_file_and_exits_2_on_errors(bench: Path, tmp_path: Path) -> None:
    data = run_json("validate", str(EXAMPLES))
    assert {row["kind"] for row in data["files"]} == {"profile", "scenario"}
    assert all(set(row) == {"path", "kind", "ok", "errors"} for row in data["files"])
    broken = tmp_path / "broken.toml"
    broken.write_text('name = "x"\n[[step]]\nkind = "teleport"\n')
    data = run_json("validate", str(broken), code=2)
    assert_error(data, "usage")
    assert data["files"][0]["ok"] is False
    assert data["files"][0]["errors"]


def test_detach_and_lease_without_a_daemon(bench: Path) -> None:
    assert run_json("detach") == {"ok": True, "unit": UNIT, "detached": False}
    assert invoke("detach").stdout.strip() == "not attached"
    assert run_json("lease") == {"ok": True, "unit": UNIT, "lease": None}
    assert not paths.pid_path(UNIT).exists()


def test_log_without_a_daemon_reads_the_newest_serial_log(bench: Path) -> None:
    for stamp, text in (("20260101T000000", "old"), ("20260102T000000", "rst:0x1 (POWERON)")):
        session = paths.sessions_dir(UNIT) / stamp
        session.mkdir(parents=True)
        (session / "serial.log").write_text(
            "10:00:00.000 +00000.000 ## attach unit=u1\n"
            f"10:00:00.100 +00000.100 {text}\n"
            "10:00:00.200 +00000.200 I (24) boot: hello\n"
        )
    result = invoke("log", "--json", "--since", "boot")
    assert result.exit_code == 0
    assert "not attached; reading" in result.stderr
    data = json.loads(result.stdout)
    assert [line["text"] for line in data["lines"]] == [
        "rst:0x1 (POWERON)",
        "I (24) boot: hello",
    ]
    assert set(data["lines"][0]) == LINE_KEYS
    assert data["lines"][0]["t"] == 0.1
    assert data["next"] == 3
    assert invoke("log").stdout.splitlines()[1] == "1 +0.100 rst:0x1 (POWERON)"
    assert_error(run_json("log", "--since", "somemark", code=2), "usage")
    assert not paths.pid_path(UNIT).exists()


def test_click_usage_errors_are_json_under_json(bench: Path) -> None:
    data = run_json("wait", "x", code=2)  # --since is required
    assert_error(data, "usage")
    assert "--since" in data["error"]["message"]
    assert invoke("wait", "x").exit_code == 2


def test_version() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


# --- attach, status, mark, cmd, wait, reset, identity, log, detach ------------------------


def test_attach_spawns_once_and_returns_status(bench: Path) -> None:
    data = run_json("attach")
    assert set(data) == {"ok", "unit"} | STATUS_KEYS
    assert set(data["daemon"]) == {"pid", "since", "port", "connected", "baud"}
    assert data["identity"]["verified"] is True
    assert data["identity"]["fields"]["fw_version"] == APP_VERSION
    assert set(data["last_reset"]) == RESET_KEYS
    assert data["gate"]["state"] == "ready"
    assert data["lease"]["owner"] == "alice"
    assert data["profile"]["name"] == PROFILE_NAME
    assert data["profile"]["path"].endswith(f"/profiles/{PROFILE_NAME}/profile.toml")
    pid = data["daemon"]["pid"]
    assert run_json("attach")["daemon"]["pid"] == pid  # a second attach reuses the daemon
    status = run_json("status")
    assert set(status) == {"ok", "unit"} | STATUS_KEYS
    assert status["daemon"]["pid"] == pid
    detached = run_json("detach")
    assert detached == {"ok": True, "unit": UNIT, "detached": True}
    assert daemon_pid(UNIT) is None


def test_mark_cmd_wait_reset_log_identity(bench: Path) -> None:
    mark = run_json("mark", "before")
    assert set(mark) == {"ok", "unit", "offset", "name"}
    assert mark["name"] == "before"
    assert invoke("mark").stdout.strip().isdigit()

    cmd = run_json("cmd", "version")
    assert set(cmd) == {"ok", "unit", "offset", "status", "line", "groups", "captured", "duration"}
    assert cmd["status"] == "ok"
    assert cmd["captured"] == f"Application Version: {APP_VERSION}"
    assert invoke("cmd", "version").stdout.strip() == f"Application Version: {APP_VERSION}"
    by_name = run_json("cmd", "--name", "version")
    assert by_name["captured"] == cmd["captured"]
    expected = run_json("cmd", "version", "--expect", r"Version: (\S+)")
    assert expected["line"] == f"Application Version: {APP_VERSION}"
    assert expected["groups"] == [APP_VERSION]

    wait = run_json("wait", "Application Version", "--since", "before", "--timeout", "1s")
    assert set(wait) == {
        "ok",
        "unit",
        "result",
        "line",
        "groups",
        "offset",
        "t",
        "duration",
        "order",
    }
    assert wait["result"] == "match"
    assert invoke("wait", "Application", "--since", "before").stdout.startswith(
        f"{wait['offset']} Application Version"
    )
    absent = run_json("wait", "--absent", "Guru Meditation", "--since", "before", "--window", "0.1")
    assert absent["result"] == "absent_ok"

    reset = run_json("reset")
    assert set(reset) == {"ok", "unit", "offset", "reset"}
    assert reset["reset"]["expected"] is True
    assert set(reset["reset"]) == RESET_KEYS
    since_reset = str(reset["offset"])
    order = run_json(
        "wait", "--order", "rst:,shell started", "--since", since_reset, "--timeout", "1"
    )
    assert order["result"] == "order_ok"
    assert len(order["order"]) == 2
    assert order["order"][0] == reset["reset"]["offset"]
    meta = run_json("wait", "reset requested", "--since", since_reset, "--timeout", "0.1", code=1)
    assert meta["result"] == "timeout"  # `## ` meta lines are never matched

    log = run_json("log", "--since", "before", "--grep", "tx ")
    assert set(log) == {"ok", "unit", "lines", "next"}
    assert [line["text"] for line in log["lines"]][:1] == ["## tx version"]
    assert all(set(line) == LINE_KEYS for line in log["lines"])
    text = invoke("log", "--since", "before", "--limit", "1").stdout.strip()
    assert text == f"{mark['offset']} " + text.split(" ", 1)[1]

    identity = run_json("identity", "--reprobe")
    assert identity == {
        "ok": True,
        "unit": UNIT,
        "fields": {"fw_version": APP_VERSION, "mac": FAKE_MAC},
        "verified": True,
        "mismatches": {},
    }
    run_json("detach")


def test_failed_results_exit_1_with_their_fields(bench: Path) -> None:
    error = run_json("cmd", "fail", "--error", "ERROR", code=1)
    assert_error(error, "assert")
    assert error["status"] == "error"
    assert error["line"] == "ERROR: failed"
    timeout = run_json("cmd", "version", "--expect", "^nope", code=1)
    assert_error(timeout, "timeout")
    assert timeout["status"] == "timeout"
    waited = run_json("wait", "never", "--since", "attach", "--timeout", "0.2", code=1)
    assert_error(waited, "timeout")
    assert waited["result"] == "timeout"
    violated = run_json("wait", "--absent", "rst:", "--since", "attach", code=1)
    assert_error(violated, "assert")
    assert violated["result"] == "absent_violated"
    run_json("detach")


def test_usage_errors_exit_2_before_anything_is_sent(bench: Path) -> None:
    denied = run_json("cmd", "deep_sleep 5", code=2)
    assert_error(denied, "usage")
    assert denied["error"]["message"].startswith("denied: ")
    assert not paths.pid_path(UNIT).exists()  # refused before auto-attach
    for args in (
        ("cmd",),
        ("cmd", "version", "--name", "version"),
        ("cmd", "version", "--expect", "x", "--capture"),
        ("cmd", "--name", "nope"),
        ("cmd", "--name", "log-level", "--arg", "tag=wifi"),
        ("wait", "--since", "attach"),
        ("wait", "(", "--since", "attach"),
        ("wait", "x", "--since", "-4"),
        ("capture",),
        ("log", "--tail"),
        ("wait", "x", "--order", "a,b", "--since", "attach"),
        ("mark", "attach"),
        ("mark", "boot"),
        ("mark", "12"),
        ("mark", " "),
    ):
        assert_error(run_json(*args, code=2), "usage")
    assert not paths.pid_path(UNIT).exists()


def test_cmd_by_name_renders_its_args(bench: Path) -> None:
    data = run_json("cmd", "--name", "log-level", "--arg", "tag=wifi", "--arg", "level=debug")
    assert data["status"] == "ok"
    log = run_json("log", "--since", str(data["offset"]), "--limit", "1")
    assert log["lines"][0]["text"] == "## tx log_level wifi debug"
    secret = run_json("cmd", "secret", "--secret")
    log = run_json("log", "--since", str(secret["offset"]), "--limit", "1")
    assert log["lines"][0]["text"] == "## tx <redacted>"
    run_json("detach")


def test_heartbeat_goes_to_stderr_unless_quiet(
    bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "HEARTBEAT_S", 0.1)
    result = invoke("wait", "never", "--since", "attach", "--timeout", "0.5", "--json")
    assert result.exit_code == 1
    assert "benchctl: waiting " in result.stderr
    assert "/0.5 s for /never/ since attach (offset now " in result.stderr
    json.loads(result.stdout)  # stdout stays one JSON object
    quiet = invoke("wait", "never", "--since", "attach", "--timeout", "0.3", "--quiet")
    assert "waiting" not in quiet.stderr
    run_json("detach")


# --- lease -------------------------------------------------------------------------------


def test_owner_mismatch_is_busy_for_mutating_verbs_and_take_transfers(bench: Path) -> None:
    run_json("attach")
    mutating: tuple[tuple[str, ...], ...] = (
        ("mark", "m"),
        ("cmd", "version"),
        ("reset",),
        ("identity",),
        ("detach",),
    )
    for args in mutating:
        data = run_json(*args, code=3, env=as_bob())
        assert_error(data, "busy")
        assert data["error"]["message"].startswith("busy: held by alice since ")
    read_only: tuple[tuple[str, ...], ...] = (
        ("status",),
        ("log",),
        ("wait", "rst:", "--since", "attach", "--timeout", "1"),
        ("lease",),
        ("units",),
    )
    for args in read_only:
        run_json(*args, env={**as_bob(), "BENCHCTL_PORTS_FILE": str(_empty_ports(bench))})
    text = invoke("cmd", "version", env=as_bob())
    assert text.exit_code == 3
    assert "busy: held by alice since" in text.stderr
    taken = run_json("attach", "--take", env=as_bob())
    assert taken["lease"]["owner"] == "bob"
    assert_error(run_json("mark", code=3), "busy")
    released = run_json("lease", "--release", env=as_bob())
    assert released["lease"] is None
    assert run_json("lease", "--take")["lease"]["owner"] == "alice"
    run_json("detach")


def _empty_ports(state_dir: Path) -> Path:
    path = state_dir / "ports.json"
    path.write_text("[]")
    return path


# --- profile handshake --------------------------------------------------------------------


def test_a_client_with_another_profile_is_refused_until_detach(
    bench: Path, config_dir: Path
) -> None:
    source = config_dir / "profiles" / PROFILE_NAME / "profile.toml"
    other = config_dir / "profiles" / "other" / "profile.toml"
    other.parent.mkdir(parents=True)
    other.write_text(source.read_text().replace(f'name = "{PROFILE_NAME}"', 'name = "other"'))
    run_json("attach")
    refused = run_json("cmd", "version", "--profile", "other", code=2)
    assert_error(refused, "usage")
    assert refused["error"]["message"].startswith(
        f"daemon for {UNIT} runs profile {PROFILE_NAME} ("
    )
    assert refused["error"]["message"].endswith(f"pass --profile {PROFILE_NAME} or detach first")
    assert run_json("status", "--profile", "other")["profile"]["name"] == PROFILE_NAME
    assert invoke("status").stdout.splitlines()[1].startswith(f"profile: {PROFILE_NAME} (")
    assert run_json("detach", "--profile", "other")["detached"] is True


# --- identity mismatch ---------------------------------------------------------------------


def test_identity_mismatch_fails_attach_and_leaves_the_daemon_unverified(
    bench: Path, config_dir: Path
) -> None:
    register_unit(config_dir, expect={"mac": OTHER_MAC})
    data = run_json("attach", code=4)
    assert_error(data, "device")
    assert data["error"]["message"] == f"identity mismatch: mac expected {OTHER_MAC} got {FAKE_MAC}"
    pid = daemon_pid(UNIT)
    assert pid is not None  # the daemon keeps the port
    assert_error(run_json("mark", code=4), "device")
    assert_error(run_json("cmd", "version", code=4), "device")
    status = run_json("status")
    assert status["identity"]["verified"] is False
    assert status["identity"]["mismatches"] == {"mac": [OTHER_MAC, FAKE_MAC]}
    cleared = run_json("attach", "--no-verify")
    assert cleared["identity"]["verified"] is True
    assert cleared["daemon"]["pid"] == pid
    run_json("mark", "ok")
    run_json("detach")


def test_a_mutating_verb_without_attach_still_checks_identity(
    bench: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_unit(config_dir, expect={"mac": OTHER_MAC})
    monkeypatch.setenv(FAKE_DAEMON_ENV, json.dumps({"progress_s": 0.1, "identity_lazy": True}))
    data = run_json("mark", "first", code=4)
    assert_error(data, "device")
    assert data["error"]["message"] == f"identity mismatch: mac expected {OTHER_MAC} got {FAKE_MAC}"
    status = run_json("status")
    assert status["identity"]["verified"] is False
    assert status["identity"]["fields"]["mac"] == FAKE_MAC
    run_json("detach")


def test_a_secret_line_never_reaches_the_client_output(
    bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "HEARTBEAT_S", 0.1)
    result = invoke("cmd", "slow hunter2", "--secret", "--timeout", "0.3", "--json")
    assert result.exit_code == 1
    assert "benchctl: waiting " in result.stderr  # the heartbeat ran and named the line
    assert "<redacted>" in result.stderr
    assert "hunter2" not in result.stdout + result.stderr
    assert_error(json.loads(result.stdout), "timeout")
    run_json("detach")


# --- flash, capture --------------------------------------------------------------------------


def _image(tmp_path: Path) -> Path:
    image = tmp_path / "app.bin"
    image.write_bytes(b"\xe9" * 128)
    return image


def test_flash_through_the_daemon(bench: Path, tmp_path: Path) -> None:
    run_json("attach")
    data = run_json("flash", f"0x10000={_image(tmp_path)}", "--baud", "921600")
    assert set(data) == {"ok", "unit", "files", "app", "baud", "duration", "reset"}
    assert set(data["files"][0]) == {"addr", "path", "name", "sha256", "size"}
    assert data["files"][0]["addr"] == 0x10000
    assert data["files"][0]["size"] == 128
    assert data["baud"] == 921600
    assert data["reset"]["cause"] == "flash"
    no_reset = run_json("flash", f"0x10000={_image(tmp_path)}", "--after", "no-reset")
    assert no_reset["reset"] is None
    assert run_json("status")["gate"]["state"] == "bootloader"
    run_json("detach")


def test_flash_direct_without_a_daemon(
    bench: Path,
    registry: Path,
    fakedev: FakeDevice,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_direct(
        port_path: str, files: list[tuple[int, str, str]], **kwargs: Any
    ) -> FlashResult:
        calls.append({"port": port_path, "files": files, **kwargs})
        return FlashResult(
            [FlashFile(a, p, n, "0" * 64, 1) for a, p, n in files], None, 1, 0.1, None
        )

    monkeypatch.setattr(flash, "flash_direct", fake_direct)
    data = run_json("flash", f"0x10000={_image(tmp_path)}")
    assert calls[0]["port"] == fakedev.path
    assert calls[0]["chip"] == "esp32s3"
    assert calls[0]["after"] == "hard-reset"
    assert set(calls[0]["flash_args"]) == {"flash-mode", "flash-size", "flash-freq"}
    assert data["files"][0]["addr"] == 0x10000
    assert not paths.pid_path(UNIT).exists()


def test_flash_needs_a_chip(bench: Path, config_dir: Path, tmp_path: Path) -> None:
    profile = config_dir / "profiles" / "esp-idf" / "profile.toml"
    profile.write_text(profile.read_text().replace('chip = "esp32s3"\n', ""))
    assert_error(run_json("flash", f"0x10000={_image(tmp_path)}", code=2), "usage")


def test_capture_through_the_daemon_and_direct(
    bench: Path,
    registry: Path,
    fakedev: FakeDevice,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_direct(port_path: str, baud: int, seconds: float, lines: Any = None) -> list[LogLine]:
        return [LogLine(0, 0.01, "10:00:00.000", "sip_user=x sip_pass=hunter2", False)]

    monkeypatch.setattr(capture, "capture_direct", fake_direct)
    out = tmp_path / "boot.log"
    direct = run_json("capture", "--reset", "--seconds", "1", "--out", str(out))
    assert direct["reset"] is None
    assert direct["lines"][0]["text"] == "sip_user=x sip_pass=***"
    assert "hunter2" not in out.read_text()
    assert out.read_text().startswith("0 +0.010 sip_user=x sip_pass=***")

    run_json("attach")
    data = run_json("capture", "--reset", "--seconds", "0.1")
    assert set(data) == {"ok", "unit", "lines", "reset"}
    assert data["reset"]["expected"] is True
    assert data["lines"][0]["offset"] == data["reset"]["offset"]
    run_json("detach")


# --- run, monitor --------------------------------------------------------------------------


def test_run_passes_through_the_runner(bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(client: Any, scenario: Any, profile: Any, **kwargs: Any) -> RunResult:
        seen.update(kwargs, scenario=scenario.name, profile=profile.name)
        result = kwargs["variables"].get("result", "pass")
        steps: list[dict[str, object]] = [
            {
                "index": 1,
                "name": "reset",
                "kind": "reset",
                "result": result,
                "duration": 0.1,
                "detail": "",
            }
        ]
        return RunResult(kwargs["run_dir"], result, {"declared": 60.0, "computed": 25.0}, steps)

    monkeypatch.setattr(runner, "run_scenario", fake_run)
    data = run_json("run", str(SCENARIO), "--dry-run", "--var", "x=1")
    assert set(data) == {"ok", "unit", "run_dir", "result", "budget", "steps"}
    assert data["budget"] == {"declared": 60.0, "computed": 25.0}
    assert seen["dry_run"] is True
    assert seen["variables"] == {"x": "1"}
    assert seen["profile"] == "esp-idf"
    assert Path(data["run_dir"]).parent == paths.runs_dir(UNIT)
    assert Path(data["run_dir"]).name.endswith("-boot-banner")
    assert not paths.pid_path(UNIT).exists()  # the runner never touched the client
    failed = run_json(
        "run", str(SCENARIO), "--var", "result=fail", "--run-dir", str(bench / "r"), code=1
    )
    assert_error(failed, "assert")
    assert failed["result"] == "fail"
    assert failed["run_dir"] == str(bench / "r")
    blocked = run_json("run", str(SCENARIO), "--var", "result=precondition_failed", code=1)
    assert_error(blocked, "assert")
    assert blocked["result"] == "precondition_failed"


def test_a_malformed_status_reply_is_a_protocol_error(
    bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_gate(self: Client, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        return {"unit": UNIT, "pid": 1, "offset": 0}

    monkeypatch.setattr(Client, "connect", lambda self: {})
    monkeypatch.setattr(Client, "request", no_gate)
    result = invoke("status", "--json")
    assert result.exit_code == 4
    assert_error(json.loads(result.stdout), "protocol")
    assert "Traceback" not in result.stdout + result.stderr


def test_a_refused_cmd_names_the_refusal(bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_DAEMON_ENV, json.dumps({"progress_s": 0.1, "shell_not_ready": True}))
    data = run_json("cmd", "version", code=1)
    assert_error(data, "timeout")
    assert data["error"]["message"] == "cmd timeout: shell not ready (state=booting)"
    run_json("detach")


class _RefusingClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def cmd(self, line: str, **kwargs: Any) -> CmdResult:
        self.sent.append(line)
        return CmdResult(7, "timeout", "shell not ready (state=booting)", [], None, 0.0)


def test_monitor_reports_a_refused_line(
    bench: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = load_profile(PROFILE_NAME, config_dir, config_dir)
    fake = _RefusingClient()
    assert monitor.handle_line(cast(DeviceClient, fake), profile, "version")
    assert fake.sent == ["version"]
    err = capsys.readouterr().err
    assert err == "benchctl: cmd timeout: shell not ready (state=booting)\n"


class _TtyClient(_RefusingClient):
    def tail(self, since: Any, events: bool) -> Subscription:
        return Subscription()


def test_monitor_exits_on_ctrl_bracket_from_a_terminal_without_enter(
    bench: Path, config_dir: Path
) -> None:
    profile = load_profile(PROFILE_NAME, config_dir, config_dir)
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] |= termios.ICANON  # canonical, as a shell leaves the terminal
    attrs[3] &= ~termios.ECHO  # nobody reads the master's side here
    attrs[6][termios.VEOL] = monitor.EXIT_CHAR.encode()
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    fake = _TtyClient()
    stdin = os.fdopen(slave, "r", closefd=True)
    done: list[int] = []
    thread = threading.Thread(
        target=lambda: done.append(
            monitor.run_monitor(cast(DeviceClient, fake), profile, stdin, since=0)
        ),
        daemon=True,
    )
    thread.start()
    os.write(master, b"version\r")
    os.write(master, monitor.EXIT_CHAR.encode())  # no newline after it
    thread.join(2.0)
    exited_on_ctrl_bracket = not thread.is_alive()
    os.close(master)  # a reader still blocked on the slave now ends instead of hanging
    thread.join(2.0)
    stdin.close()
    assert exited_on_ctrl_bracket, "monitor waited for Enter after Ctrl-]"
    assert done == [0]
    assert fake.sent == ["version"]


def test_monitor_sends_typed_lines_and_exits_on_ctrl_bracket(bench: Path) -> None:
    run_json("attach")
    result = CliRunner().invoke(
        cli.main, ["monitor"], input="version\ndeep_sleep 1\n!reset\n\x1d\nnot sent\n"
    )
    assert result.exit_code == 0, result.stderr
    assert "denied: " in result.stderr
    texts = [line["text"] for line in run_json("log")["lines"]]
    assert "## tx version" in texts
    assert "## tx deep_sleep 1" not in texts
    assert "## tx not sent" not in texts
    assert texts[-1] != "## tx not sent"
    assert any(text.startswith("## reset requested") for text in texts)
    run_json("detach")


# --- the daemon verb, and two state dirs ---------------------------------------------------


def test_daemon_verb_exits_3_when_another_holds_the_lock(bench: Path) -> None:
    paths.unit_dir(UNIT).mkdir(parents=True)
    holder = PortLock(paths.lease_lock_path(UNIT))
    assert holder.acquire()
    try:
        result = invoke("daemon", "--unit", UNIT)
        assert result.exit_code == 3
        assert json.loads(result.stderr.strip().splitlines()[-1])["error"]["code"] == "busy"
        assert not paths.pid_path(UNIT).exists()
    finally:
        holder.release()


def test_two_state_dirs_run_two_daemons(bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = Path(tempfile.mkdtemp(prefix="bc-", dir="/tmp"))
    try:
        first = run_json("attach")["daemon"]["pid"]
        monkeypatch.setenv("BENCHCTL_STATE_DIR", str(other))
        second = run_json("attach")["daemon"]["pid"]
        assert first != second
        run_json("mark", "two")
        monkeypatch.setenv("BENCHCTL_STATE_DIR", str(bench))
        assert run_json("status")["daemon"]["pid"] == first
        assert_error(run_json("wait", "x", "--since", "two", "--timeout", "0.1", code=2), "usage")
        run_json("detach")
        monkeypatch.setenv("BENCHCTL_STATE_DIR", str(other))
        assert run_json("status")["daemon"]["pid"] == second
        run_json("detach")
    finally:
        reap_daemons(other)
        shutil.rmtree(other, ignore_errors=True)
