"""Checks `--from` / `--to` (inclusive, optional, validated) on `ledger list` and `ledger report`."""

import pytest

from ledger.cli import main

ROWS = [
    ("2024-01-31", "950", "rent"),
    ("2024-02-01", "10", "food"),
    ("2024-02-15", "5", "transport"),
    ("2024-02-29", "20", "food"),
    ("2024-03-01", "7", "food"),
]


@pytest.fixture
def data(tmp_path):
    path = str(tmp_path / "ledger.json")
    for row in ROWS:
        assert main(["--file", path, "add", *row]) == 0
    return path


def out_of(capsys, *argv):
    capsys.readouterr()
    assert main(list(argv)) == 0
    return capsys.readouterr().out.splitlines()


def test_list_from_is_inclusive(data, capsys):
    out = out_of(capsys, "--file", data, "list", "--from", "2024-02-15")
    assert out == ["2024-02-15  5  transport", "2024-02-29  20  food", "2024-03-01  7  food"]


def test_list_to_is_inclusive(data, capsys):
    out = out_of(capsys, "--file", data, "list", "--to", "2024-02-01")
    assert out == ["2024-01-31  950  rent", "2024-02-01  10  food"]


def test_list_from_and_to(data, capsys):
    out = out_of(capsys, "--file", data, "list", "--from", "2024-02-01", "--to", "2024-02-29")
    assert out == ["2024-02-01  10  food", "2024-02-15  5  transport", "2024-02-29  20  food"]


def test_list_without_options_is_unchanged(data, capsys):
    assert len(out_of(capsys, "--file", data, "list")) == 5


def test_empty_range_lists_nothing(data, capsys):
    assert out_of(capsys, "--file", data, "list", "--from", "2024-04-01") == []
    assert out_of(capsys, "--file", data, "list", "--from", "2024-02-20", "--to", "2024-02-10") == []


def test_report_by_category_in_range(data, capsys):
    out = out_of(capsys, "--file", data, "report", "--from", "2024-02-01", "--to", "2024-02-29")
    assert out == ["food  30", "transport  5"]


def test_report_by_month_in_range(data, capsys):
    out = out_of(capsys, "--file", data, "report", "--by", "month", "--from", "2024-02-15")
    assert out == ["2024-02  25", "2024-03  7"]
    assert out_of(capsys, "--file", data, "report", "--by", "month", "--to", "2024-01-31") == ["2024-01  950"]


@pytest.mark.parametrize("command", ["list", "report"])
@pytest.mark.parametrize("option", ["--from", "--to"])
@pytest.mark.parametrize("bad", ["2024-02-30", "yesterday", "2024-13-01"])
def test_invalid_date_is_refused_with_exit_code_2(data, command, option, bad):
    with pytest.raises(SystemExit) as exc:
        main(["--file", data, command, option, bad])
    assert exc.value.code == 2
