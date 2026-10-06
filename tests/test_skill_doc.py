"""SKILL.md is a Claude Code skill that teaches the whole CLI."""

from __future__ import annotations

import re
from pathlib import Path

from benchctl import cli

SKILL = Path(__file__).resolve().parent.parent / "SKILL.md"
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER.match(text)
    assert match, "SKILL.md must open with a --- frontmatter block"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        assert sep, f"frontmatter line without a key: {line!r}"
        fields[key.strip()] = value.strip()
    return fields


def test_skill_has_claude_code_frontmatter() -> None:
    fields = frontmatter(SKILL.read_text(encoding="utf-8"))
    assert fields["name"] == "benchctl"
    assert len(fields["description"]) > 40


def test_skill_names_every_verb() -> None:
    text = SKILL.read_text(encoding="utf-8")
    verbs = sorted(cli.main.commands)  # hidden `daemon` included
    assert "attach" in verbs
    missing = [verb for verb in verbs if not re.search(rf"`benchctl {verb}\b", text)]
    assert missing == []


def test_skill_teaches_the_workflow_rules() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for needle in (
        "--json",
        "--since",
        "mark",
        "busy",
        "--take",
        "resets the unit",
        "--dry-run",
        "results.md",
        "identity",
    ):
        assert needle in text
    for code in range(5):
        assert re.search(rf"^\| {code} \|", text, re.MULTILINE), f"exit code {code} undocumented"
