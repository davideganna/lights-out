"""Checks that every printed amount ends with ` €` (add, list, report) and that the data file is unchanged."""

import json

import pytest

from ledger.cli import main


@pytest.fixture
def data(tmp_path):
    return str(tmp_path / "ledger.json")


def run(capsys, *argv):
    capsys.readouterr()
    assert main(list(argv)) == 0
    return capsys.readouterr().out.splitlines()


def test_add_confirmation_shows_the_symbol(data, capsys):
    out = run(capsys, "--file", data, "add", "2024-03-05", "12.50", "food", "--note", "lunch")
    assert out == ["Added 12.50 € to food on 2024-03-05"]


def test_list_shows_the_symbol_after_the_amount(data, capsys):
    run(capsys, "--file", data, "add", "2024-03-05", "12.50", "food", "--note", "lunch")
    run(capsys, "--file", data, "add", "2024-03-06", "3.00", "transport")
    assert run(capsys, "--file", data, "list") == ["2024-03-05  12.50 €  food  lunch", "2024-03-06  3.00 €  transport"]


def test_report_by_category_shows_the_symbol(data, capsys):
    run(capsys, "--file", data, "add", "2024-03-05", "12.50", "food")
    run(capsys, "--file", data, "add", "2024-03-06", "8.20", "food")
    run(capsys, "--file", data, "add", "2024-03-07", "3.00", "transport")
    assert run(capsys, "--file", data, "report") == ["food  20.70 €", "transport  3.00 €"]


def test_report_by_month_shows_the_symbol(data, capsys):
    run(capsys, "--file", data, "add", "2024-03-05", "12.50", "food")
    run(capsys, "--file", data, "add", "2024-04-06", "8.20", "food")
    assert run(capsys, "--file", data, "report", "--by", "month") == ["2024-03  12.50 €", "2024-04  8.20 €"]


def test_data_file_keeps_plain_amounts(data, capsys):
    run(capsys, "--file", data, "add", "2024-03-05", "12.50", "food")
    with open(data, encoding="utf-8") as f:
        stored = json.load(f)
    assert stored[0]["amount"] == "12.50"
    assert "€" not in json.dumps(stored, ensure_ascii=False)


def test_amounts_are_not_double_marked_after_reloading(data, capsys):
    run(capsys, "--file", data, "add", "2024-03-05", "12.50", "food")
    run(capsys, "--file", data, "add", "2024-03-06", "1.00", "food")
    assert run(capsys, "--file", data, "list")[0] == "2024-03-05  12.50 €  food"
