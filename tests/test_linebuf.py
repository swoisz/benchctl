"""Line engine. The `test_<name>` cases named after hail's line tests (src/lib.rs) are its
one-to-one ports; hail's marker cases are `Command.expect_regex`'s (test_profile.py) and its
port-level cases (timeout, disconnect) the session's."""

from __future__ import annotations

from benchctl.linebuf import LineEngine, strip_ansi, take_line
from fakedev import DEFAULT_BOOT, ELF_SHA_PREFIX, PROMPT, RST_POWERON

# hail ports


def test_read_until_returns_prefix_and_consumes_marker() -> None:
    # benchctl has no consuming read_until: an unterminated marker (the prompt) is matched in
    # the tail, and the text before and after it stays visible there.
    engine = LineEngine()
    assert engine.feed(b"preamble READYtail") == []
    assert engine.tail.partition("READY") == ("preamble ", "READY", "tail")

    coloured = LineEngine()
    assert coloured.feed(b"\x1b[0;32mpreamble \x1b[0mREADYtail") == []
    assert coloured.tail == "preamble READYtail"


def test_next_line_reads_sequentially() -> None:
    assert LineEngine().feed(b"first\r\nsecond\r\n") == ["first", "second"]


def test_utf8_multibyte_split_across_reads() -> None:
    engine = LineEngine()
    data = "2°C".encode()  # ° is 0xC2 0xB0
    assert engine.feed(data[:2]) == []
    assert engine.tail == "2"
    assert engine.feed(data[2:]) == []
    assert engine.tail == "2°C"


def test_strip_ansi_removes_csi() -> None:
    assert strip_ansi("\x1b[1;32mok\x1b[0m") == "ok"
    assert strip_ansi("plain") == "plain"
    assert strip_ansi("a\x1b[2Kb") == "ab"


def test_strip_ansi_handles_non_letter_finals() -> None:
    assert strip_ansi("a\x1b[2~b") == "ab"


def test_take_line_handles_lf_crlf_and_bare_cr() -> None:
    assert take_line("a\r\nb\nc") == ("a", "b\nc")
    assert take_line("b\nc") == ("b", "c")
    assert take_line("c") is None

    assert take_line("x\ry\r\nz") == ("x", "y\r\nz")
    assert take_line("y\r\nz") == ("y", "z")
    assert take_line("z") is None


def test_take_line_holds_back_lone_trailing_cr() -> None:
    assert take_line("ab\r") is None
    assert take_line("ab\r" + "\ncd") == ("ab", "cd")


# benchctl additions


def test_invalid_utf8_becomes_replacement_char() -> None:
    assert LineEngine().feed(b"a\xffb\r\n") == ["a�b"]


def test_crlf_split_across_reads_is_one_terminator() -> None:
    engine = LineEngine()
    assert engine.feed(b"ab\r") == []
    assert engine.tail == ""
    assert engine.feed(b"\ncd") == ["ab"]
    assert engine.tail == "cd"


def test_held_cr_followed_by_text_is_a_bare_cr_line() -> None:
    engine = LineEngine()
    assert engine.feed(b"x\r") == []
    assert engine.feed(b"y") == ["x"]
    assert engine.tail == "y"


def test_csi_split_across_reads_is_stripped_whole() -> None:
    engine = LineEngine()
    assert engine.feed(b"\x1b[0;3") == []
    assert engine.tail == ""
    assert engine.feed(b"2mok\x1b[0m\r\n") == ["ok"]


def test_prompt_lives_in_the_tail_until_the_echo_completes_it() -> None:
    engine = LineEngine()
    assert engine.feed(PROMPT.encode()) == []
    assert engine.tail == PROMPT
    assert engine.feed(b"version\r\n") == ["demo> version"]
    assert engine.tail == ""


def test_fake_boot_reassembles_split_writes() -> None:
    engine = LineEngine()
    lines = engine.feed(RST_POWERON)
    for _, chunk in DEFAULT_BOOT:
        lines += engine.feed(chunk)
    lines += engine.feed(PROMPT.encode())

    assert lines[0] == "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)"
    assert f"I (305) app_init: ELF file SHA256:  {ELF_SHA_PREFIX}" in lines
    assert "I (420) board: die temp 41.5°C" in lines
    assert len(lines) == 1 + len(DEFAULT_BOOT) - 2  # two lines arrive in two writes each
    assert not any("\x1b" in line for line in lines)
    assert engine.tail == PROMPT
