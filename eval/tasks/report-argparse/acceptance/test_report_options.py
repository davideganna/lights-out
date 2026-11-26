"""Checks that `ledger report` parses `--by` like the other commands: help works, bad values exit 2, same output."""

import pytest

from ledger.cli import main


@pytest.fixture
def data(tmp_path):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "12.50", "food"])
    main(["--file", path, "add", "2024-04-06", "3", "transport"])
    main(["--file", path, "add", "2024-04-07", "8.20", "food"])
    return path


def out_of(capsys, *argv):
    capsys.readouterr()
    assert main(list(argv)) == 0
    return capsys.readouterr().out.splitlines()


def test_report_help_mentions_by_and_exits_0(data, capsys):
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["--file", data, "report", "--help"])
    assert exc.value.code == 0
    assert "--by" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["yearly", "week", ""])
def test_unknown_by_value_exits_2(data, bad):
    with pytest.raises(SystemExit) as exc:
        main(["--file", data, "report", "--by", bad])
    assert exc.value.code == 2


def test_by_without_a_value_exits_2(data):
    with pytest.raises(SystemExit) as exc:
        main(["--file", data, "report", "--by"])
    assert exc.value.code == 2


def test_unknown_option_exits_2(data):
    with pytest.raises(SystemExit) as exc:
        main(["--file", data, "report", "--colour"])
    assert exc.value.code == 2


def test_by_equals_form_works(data, capsys):
    assert out_of(capsys, "--file", data, "report", "--by=month") == ["2024-03  12.50", "2024-04  11.20"]
    assert out_of(capsys, "--file", data, "report", "--by=category") == ["food  20.70", "transport  3"]


def test_output_is_the_same_as_before(data, capsys):
    assert out_of(capsys, "--file", data, "report") == ["food  20.70", "transport  3"]
    assert out_of(capsys, "--file", data, "report", "--by", "category") == ["food  20.70", "transport  3"]
    assert out_of(capsys, "--file", data, "report", "--by", "month") == ["2024-03  12.50", "2024-04  11.20"]
