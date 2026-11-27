"""Checks `ledger edit N [--amount X] [--category C]`: changes only the given fields and rejects bad input."""

import pytest

from ledger.cli import main

LINES = ["2024-03-05  12.50  food  lunch", "2024-03-06  3  transport", "2024-03-07  8.20  food  dinner"]


def run(capsys, *argv):
    capsys.readouterr()
    try:
        code = main(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def data(tmp_path):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "12.50", "food", "--note", "lunch"])
    main(["--file", path, "add", "2024-03-06", "3", "transport"])
    main(["--file", path, "add", "2024-03-07", "8.20", "food", "--note", "dinner"])
    return path


def listed(capsys, data):
    return run(capsys, "--file", data, "list")[1].splitlines()


def test_edit_amount_only(data, capsys):
    code, out, _ = run(capsys, "--file", data, "edit", "2", "--amount", "4.50")
    assert (code, out.splitlines()) == (0, ["Updated 2024-03-06  4.50  transport"])
    assert listed(capsys, data) == [LINES[0], "2024-03-06  4.50  transport", LINES[2]]


def test_edit_category_only_keeps_amount_and_note(data, capsys):
    code, out, _ = run(capsys, "--file", data, "edit", "1", "--category", "restaurant")
    assert (code, out.splitlines()) == (0, ["Updated 2024-03-05  12.50  restaurant  lunch"])
    assert listed(capsys, data) == ["2024-03-05  12.50  restaurant  lunch", LINES[1], LINES[2]]


def test_edit_both(data, capsys):
    code, out, _ = run(capsys, "--file", data, "edit", "3", "--amount", "9", "--category", "dining")
    assert (code, out.splitlines()) == (0, ["Updated 2024-03-07  9  dining  dinner"])
    assert listed(capsys, data)[2] == "2024-03-07  9  dining  dinner"


def test_edit_is_reflected_in_reports(data, capsys):
    run(capsys, "--file", data, "edit", "3", "--category", "transport")
    assert run(capsys, "--file", data, "report")[1].splitlines() == ["food  12.50", "transport  11.20"]


@pytest.mark.parametrize(
    "args",
    [
        ["2"],
        ["0", "--amount", "1"],
        ["4", "--amount", "1"],
        ["abc", "--amount", "1"],
        ["2", "--amount", "abc"],
        ["2", "--amount", "-5"],
        ["2", "--amount", "0"],
    ],
)
def test_bad_input_fails_and_changes_nothing(data, capsys, args):
    before = open(data, encoding="utf-8").read()
    code, out, _ = run(capsys, "--file", data, "edit", *args)
    assert code != 0
    assert not out.startswith("Updated")
    assert open(data, encoding="utf-8").read() == before
    assert listed(capsys, data) == LINES
