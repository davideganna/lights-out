"""Checks `ledger delete N`: removes the N-th listed expense, prints `Deleted <list line>`, rejects bad numbers."""

import pytest

from ledger.cli import main

ROWS = [
    ("2024-03-05", "12.50", "food", "lunch"),
    ("2024-03-06", "3", "transport", ""),
    ("2024-03-07", "8.20", "food", "dinner"),
]
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
    for day, amount, category, note in ROWS:
        assert main(["--file", path, "add", day, amount, category, "--note", note]) == 0
    return path


def listed(capsys, data):
    return run(capsys, "--file", data, "list")[1].splitlines()


def test_delete_middle_expense(data, capsys):
    code, out, _ = run(capsys, "--file", data, "delete", "2")
    assert code == 0
    assert out.splitlines() == ["Deleted 2024-03-06  3  transport"]
    assert listed(capsys, data) == [LINES[0], LINES[2]]


def test_delete_first_and_last(data, capsys):
    code, out, _ = run(capsys, "--file", data, "delete", "1")
    assert (code, out.splitlines()) == (0, [f"Deleted {LINES[0]}"])
    code, out, _ = run(capsys, "--file", data, "delete", "2")
    assert (code, out.splitlines()) == (0, [f"Deleted {LINES[2]}"])
    assert listed(capsys, data) == [LINES[1]]


def test_delete_everything(data, capsys):
    for _ in range(3):
        assert run(capsys, "--file", data, "delete", "1")[0] == 0
    assert listed(capsys, data) == []


@pytest.mark.parametrize("bad", ["0", "4", "99", "abc", "-1", "1.5"])
def test_bad_number_fails_and_changes_nothing(data, capsys, bad):
    before = open(data, encoding="utf-8").read()
    code, out, _ = run(capsys, "--file", data, "delete", bad)
    assert code != 0
    assert not out.startswith("Deleted")
    assert open(data, encoding="utf-8").read() == before
    assert listed(capsys, data) == LINES
