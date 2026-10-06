"""Redaction rules: one group kept, no group masks the whole match, idempotence."""

from __future__ import annotations

import pytest

from benchctl.errors import UsageError
from benchctl.redact import Redactor
from conftest import SIP_PASS_REDACT
from fakedev import DEFAULT_REPLIES, SECRET

TOKEN = r"tok_[A-Za-z0-9]+"


def test_one_group_is_kept_and_the_rest_masked() -> None:
    redacted = Redactor([SIP_PASS_REDACT]).apply(DEFAULT_REPLIES["secret"])
    assert redacted == "sip_user=bench sip_pass=***"
    assert SECRET not in redacted


def test_one_group_handles_json_shaped_output() -> None:
    redactor = Redactor([SIP_PASS_REDACT])
    assert redactor.apply('{"sip_pass": "hunter2", "x": 1}') == '{"sip_pass": "***", "x": 1}'


def test_no_group_masks_the_whole_match() -> None:
    assert Redactor([TOKEN]).apply("auth tok_abc123 ok") == "auth *** ok"


def test_every_occurrence_is_masked() -> None:
    redacted = Redactor([TOKEN]).apply("tok_a then tok_b")
    assert redacted == "*** then ***"


def test_patterns_are_case_sensitive() -> None:
    assert Redactor([SIP_PASS_REDACT]).apply("SIP_PASS=x") == "SIP_PASS=x"


def test_patterns_apply_in_order() -> None:
    redactor = Redactor([SIP_PASS_REDACT, TOKEN])
    assert redactor.apply("sip_pass=hunter2 tok_x") == "sip_pass=*** ***"


def test_optional_group_that_did_not_participate_masks_the_match() -> None:
    assert Redactor([r"(pass=)?secret[0-9]+"]).apply("got secret42") == "got ***"


def test_no_patterns_is_identity() -> None:
    assert Redactor([]).apply("sip_pass=hunter2") == "sip_pass=hunter2"


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        (SIP_PASS_REDACT, DEFAULT_REPLIES["secret"]),
        (TOKEN, "auth tok_abc123 ok"),
        (r"(key=)\S+", "key=abc def"),  # the masked part can re-match `***`
        (r"\S+", "a bc"),
        (r"(sip_pass=)\S*", "sip_pass= and sip_pass=x"),  # empty secret part
        (r"x*", "abc"),  # zero-width matches
        (r".", "ab"),  # each masked `*` re-matches on its own
    ],
)
def test_apply_is_idempotent(pattern: str, text: str) -> None:
    redactor = Redactor([pattern])
    once = redactor.apply(text)
    assert redactor.apply(once) == once


def test_already_masked_text_is_left_alone() -> None:
    assert Redactor([r"(key=)\S"]).apply("key=***") == "key=***"


@pytest.mark.parametrize("pattern", [r"(a)(b)", r"(sip_pass=)(\S+)", r"(?P<k>a)(b)"])
def test_more_than_one_group_is_rejected(pattern: str) -> None:
    with pytest.raises(UsageError) as info:
        Redactor([pattern])
    assert info.value.code == "usage"


def test_non_capturing_groups_do_not_count() -> None:
    redactor = Redactor([r"(?:sip|ua)_(pass=)\S+"])
    assert redactor.apply("ua_pass=x") == "pass=***"


def test_invalid_regex_is_rejected() -> None:
    with pytest.raises(UsageError):
        Redactor(["(unclosed"])
