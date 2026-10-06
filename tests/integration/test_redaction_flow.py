"""Redaction end to end (design Sections 4.7, 4.8): a reply caught by a profile pattern, and a
`cmd --secret` line no pattern covers."""

from __future__ import annotations

from conftest import UNIT
from fakedev import SECRET
from integration.conftest import Bench

REDACTED_REPLY = "sip_user=bench sip_pass=***"
# No profile pattern matches this line; only --secret keeps SECRET out of the record.
SECRET_LINE = f"wifi set bench {SECRET}"


def secret_session(bench: Bench) -> None:
    """Attach, have the unit print a sip_pass, send a --secret line, wait on the reply."""
    bench.add_unit(UNIT)
    reply = bench.cli("cmd", "secret")
    assert reply["captured"] == REDACTED_REPLY
    hidden = bench.cli("cmd", "--secret", SECRET_LINE)
    assert hidden["status"] == "ok"
    assert hidden["captured"] == "command not found"
    found = bench.cli("wait", "sip_pass", "--since", "attach")
    assert found["line"] == REDACTED_REPLY


def test_secrets_stay_out_of_json_events_and_daemon_log(bench: Bench) -> None:
    secret_session(bench)
    events = bench.events(UNIT)
    sent = [event for event in events if event["type"] == "tx" and event["source"] == "cmd"]
    assert [event["line"] for event in sent] == ["secret", "<redacted>"]
    serial = bench.serial_log(UNIT)
    assert REDACTED_REPLY in serial
    assert "## tx <redacted>" in serial
    assert f"sip_pass={SECRET}" not in serial
    assert bench.cli("detach")["detached"] is True

    for call in bench.calls:
        assert SECRET not in call.stdout, call.args
        assert SECRET not in call.stderr, call.args
    events_file = (bench.session_dir(UNIT) / "events.jsonl").read_text(encoding="utf-8")
    assert SECRET not in events_file
    assert SECRET not in bench.daemon_log(UNIT)


def test_secret_line_echo_stays_out_of_serial_log_and_log_json(bench: Bench) -> None:
    secret_session(bench)
    log = bench.run("log", "--since", "attach", "--json")
    assert SECRET not in log.stdout
    assert SECRET not in bench.serial_log(UNIT)
