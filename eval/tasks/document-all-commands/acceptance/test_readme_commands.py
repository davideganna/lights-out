"""Checks that the README documents every command in `ledger --help`, with an example line and all its options."""

import re
from pathlib import Path

import pytest

from ledger.cli import main

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def help_text(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def commands(capsys):
    group = re.search(r"\{([a-z][a-z,-]*)\}", help_text(capsys))
    assert group, "could not find the command list in `ledger --help`"
    return group.group(1).split(",")


def test_the_known_commands_are_found(capsys):
    assert {"add", "list", "report"} <= set(commands(capsys))


def test_every_command_has_an_example_line_in_the_readme(capsys):
    missing = [c for c in commands(capsys) if not re.search(rf"^\s*(\$ )?ledger {re.escape(c)}\b", README, re.M)]
    assert missing == [], f"no example line `ledger <command>` in the README for: {missing}"


def test_every_option_of_every_command_is_in_the_readme(capsys):
    missing = []
    for command in commands(capsys):
        for option in sorted(set(re.findall(r"--[a-z][a-z0-9-]*", help_text(capsys, command))) - {"--help"}):
            if option not in README:
                missing.append(f"{command} {option}")
    assert missing == [], f"options missing from the README: {missing}"
