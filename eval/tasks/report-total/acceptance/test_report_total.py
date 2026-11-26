"""Checks that `ledger report` ends with a `total  <sum>` line in both modes, `total  0` when empty."""

import pytest

from ledger.cli import main


@pytest.fixture
def data(tmp_path):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "12.50", "food"])
    main(["--file", path, "add", "2024-03-06", "3", "transport"])
    main(["--file", path, "add", "2024-04-07", "8.20", "food"])
    return path


def report(capsys, *argv):
    capsys.readouterr()
    assert main(list(argv)) == 0
    return capsys.readouterr().out.splitlines()


def test_total_by_category(data, capsys):
    assert report(capsys, "--file", data, "report") == ["food  20.70", "transport  3", "total  23.70"]


def test_total_by_month(data, capsys):
    out = report(capsys, "--file", data, "report", "--by", "month")
    assert out == ["2024-03  15.50", "2024-04  8.20", "total  23.70"]


def test_total_of_whole_numbers(tmp_path, capsys):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "10", "food"])
    main(["--file", path, "add", "2024-03-06", "5", "food"])
    assert report(capsys, "--file", path, "report") == ["food  15", "total  15"]


def test_total_is_exact(tmp_path, capsys):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "0.10", "food"])
    main(["--file", path, "add", "2024-03-06", "0.20", "rent"])
    assert report(capsys, "--file", path, "report")[-1] == "total  0.30"


@pytest.mark.parametrize("extra", [[], ["--by", "month"]])
def test_empty_ledger_total(tmp_path, capsys, extra):
    assert report(capsys, "--file", str(tmp_path / "none.json"), "report", *extra) == ["total  0"]
