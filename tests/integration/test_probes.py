"""Behaviour the unit tests could not reach: `cmd --no-wait` on the real session, and a client
`--profile` that differs from the running daemon's, which the daemon refuses."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from conftest import PROFILE_NAME, TIMESCALE, UNIT, dump_toml
from fakedev import DeviceScript
from integration.conftest import Bench, shell_lines

SLOW_DEVICE_S = 60.0  # the fake answers `slow` after this many device seconds
SLOW_REPLY_S = SLOW_DEVICE_S * TIMESCALE  # 3 s of wall time
OTHER = "other"
OTHER_DENY = "denied by other"


def test_cmd_no_wait_returns_at_tx(bench: Bench) -> None:
    bench.add_unit(UNIT, DeviceScript(slow_s=SLOW_DEVICE_S))
    bench.cli("attach")

    sent = bench.cli("cmd", "slow", "--no-wait")
    assert sent["status"] == "ok"
    assert (sent["line"], sent["groups"], sent["captured"]) == (None, [], None)
    assert sent["duration"] < SLOW_REPLY_S / 3

    reply = bench.cli("wait", "^slow: done$", "--since", str(sent["offset"]), "--timeout", "30")
    tx = bench.cli("log", "--since", str(sent["offset"]), "--limit", "1")["lines"][0]
    assert tx["text"] == "## tx slow"
    # The reply arrived a whole slow_s after the line went out; cmd had long returned.
    assert reply["t"] - tx["t"] > SLOW_REPLY_S * 0.8 > sent["duration"]


def install_other_profile(config: Path) -> None:
    """The installed example as profile OTHER: it denies `version` instead of `deep_sleep`
    and also redacts sip_user."""
    source = config / "profiles" / PROFILE_NAME / "profile.toml"
    data: dict[str, Any] = tomllib.loads(source.read_text(encoding="utf-8"))
    data["profile"]["name"] = OTHER
    data["deny"] = [{"pattern": r"^version\b", "reason": OTHER_DENY}]
    data["redact"] = [*data["redact"], {"name": "sip_user", "pattern": r"(sip_user=)\S+"}]
    target = config / "profiles" / OTHER / "profile.toml"
    target.parent.mkdir(parents=True)
    target.write_text(dump_toml(data), encoding="utf-8")


def test_client_profile_against_a_daemon_running_another(bench: Bench) -> None:
    """The daemon keeps the profile it was spawned with. A verb that renders or deny-checks
    with another (`cmd`) is refused before anything is sent; `status` names the daemon's
    profile and, like `detach`, works whatever `--profile` says."""
    device = bench.add_unit(UNIT)
    install_other_profile(bench.config)
    bench.cli("attach")  # the daemon runs the unit's profile

    # OTHER denies `version` client-side before any connection (see the end of this test).
    refused = bench.cli("cmd", "secret", "--profile", OTHER, code=2)
    assert f"runs profile {PROFILE_NAME}" in refused["error"]["message"]
    for args in (("status",), ("status", "--profile", OTHER)):
        status = bench.cli(*args)
        assert status["profile"]["name"] == PROFILE_NAME
    assert set(status) == {
        "ok",
        "unit",
        "daemon",
        "profile",
        "lease",
        "identity",
        "last_reset",
        "offset",
        "gate",
        "session_dir",
    }
    assert shell_lines(device) == []
    assert bench.cli("detach", "--profile", OTHER)["detached"] is True

    # The verb that spawns the daemon picks its profile, and it outlives that verb.
    bench.cli("mark", "--profile", OTHER)
    assert bench.cli("status")["profile"]["name"] == OTHER
    reply = bench.cli("cmd", "secret", "--profile", OTHER)
    assert reply["captured"] == "sip_user=*** sip_pass=***"
    mismatch = bench.cli("cmd", "version", code=2)  # resolves the unit's default profile
    assert f"runs profile {OTHER}" in mismatch["error"]["message"]
    denied = bench.cli("cmd", "version", "--profile", OTHER, code=2)
    assert denied["error"]["message"] == f"denied: {OTHER_DENY}"
    assert shell_lines(device) == ["secret"]
    assert bench.cli("detach")["detached"] is True
