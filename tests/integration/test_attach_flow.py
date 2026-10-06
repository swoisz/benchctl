"""Auto-attach through the real CLI and daemon, then mark → cmd → wait (design Sections 3.3,
4.6, 4.7, 7.1), and the identity check that guards a mutating verb (Section 4.12)."""

from __future__ import annotations

from benchctl import paths
from benchctl.lease import pid_alive
from conftest import UNIT
from fakedev import APP_VERSION
from integration.conftest import OWNER, Bench, shell_lines

VERSION_REPLY = f"Application Version: {APP_VERSION}"
NO_MATCH_S = "1"


def test_auto_attach_then_mark_cmd_wait_status_detach(bench: Bench) -> None:
    device = bench.add_unit(UNIT)

    mark = bench.cli("mark", "start")  # no daemon yet: this attaches
    assert mark["name"] == "start"
    start = mark["offset"]

    cmd = bench.cli("cmd", "version")
    tx = cmd["offset"]
    assert tx > start
    assert cmd["status"] == "ok"
    assert cmd["line"] is None
    # The reply alone: no echo, and the capture stopped at the prompt.
    assert cmd["captured"] == VERSION_REPLY
    after_tx = bench.cli("log", "--since", str(tx))["lines"]
    assert [line["text"] for line in after_tx] == [
        "## tx version",
        "demo> version",
        VERSION_REPLY,
    ]
    assert [line["offset"] for line in after_tx] == [tx, tx + 1, tx + 2]

    # `## tx version` (offset tx) contains the pattern; only device lines may match.
    found = bench.cli("wait", "version", "--since", "start")
    assert found["result"] == "match"
    assert found["offset"] == tx + 1
    assert found["line"] == "demo> version"
    meta_only = bench.cli("wait", "^## ", "--since", "start", "--timeout", NO_MATCH_S, code=1)
    assert meta_only["result"] == "timeout"
    assert meta_only["error"]["code"] == "timeout"

    status = bench.cli("status")
    assert status["identity"] == {
        "fields": {"fw_version": APP_VERSION, "project": "fakeapp"},
        "verified": True,
        "mismatches": {},
    }
    assert status["lease"]["owner"] == OWNER
    assert status["daemon"]["port"] == device.path
    assert status["daemon"]["connected"] is True
    assert status["gate"]["state"] == "ready"
    assert status["last_reset"]["cause"] == "attach"
    assert status["last_reset"]["expected"] is True
    pid = status["daemon"]["pid"]

    assert bench.cli("detach") == {"ok": True, "unit": UNIT, "detached": True}
    assert not paths.pid_path(UNIT).exists()
    assert not paths.socket_path(UNIT).exists()
    assert not pid_alive(pid)

    # One attach, one reset: the pulse, the fake's boot, and the session's one reset event.
    assert bench.bridge.pulses[UNIT] == [False]
    assert device.resets == ["poweron"]
    events = bench.events(UNIT)
    resets = [event for event in events if event["type"] == "reset"]
    assert len(resets) == 1
    assert (resets[0]["expected"], resets[0]["cause"], resets[0]["reason"]) == (
        True,
        "attach",
        "POWERON",
    )
    # The gate was ready before the command went out, and the shell did not drop it.
    sent = next(e for e in events if e["type"] == "tx" and e["source"] == "cmd")
    assert sent["line"] == "version"
    gates = [e["state"] for e in events if e["type"] == "gate" and e["seq"] < sent["seq"]]
    assert gates[-1] == "ready"
    assert resets[0]["seq"] < sent["seq"]
    assert [r.outcome for r in device.received if r.line == "version"] == ["answered"]
    assert [e["type"] for e in events][-1] == "detach"


def test_identity_mismatch_refuses_a_mutating_verb_without_attach(bench: Bench) -> None:
    device = bench.add_unit(UNIT, expect={"fw_version": "9.9.9"})
    message = f"identity mismatch: fw_version expected 9.9.9 got {APP_VERSION}"

    refused = bench.cli("cmd", "version", code=4)  # auto-attaches, probes, refuses
    assert refused["ok"] is False
    assert refused["error"]["code"] == "device"
    assert refused["error"]["message"] == message
    assert refused["error"]["data"]["identity"]["verified"] is False
    marked = bench.cli("mark", "x", code=4)
    assert marked["error"]["message"] == message

    # Read-only verbs still answer, so the operator can see what is plugged in.
    status = bench.cli("status")
    assert status["identity"]["verified"] is False
    assert status["identity"]["mismatches"] == {"fw_version": ["9.9.9", APP_VERSION]}
    # Nothing reached the shell, and the daemon kept the port without a second reset.
    assert shell_lines(device) == []
    assert device.resets == ["poweron"]
    identities = [event for event in bench.events(UNIT) if event["type"] == "identity"]
    assert [event["verified"] for event in identities] == [False]
    assert not any(e["type"] == "tx" and e["source"] != "probe" for e in bench.events(UNIT))
    assert bench.cli("detach")["detached"] is True
