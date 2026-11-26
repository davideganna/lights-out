"""Checks `ledger report --json`: one JSON object, same keys as the text report, totals as strings."""

import json

import pytest

from ledger.cli import main


@pytest.fixture
def data(tmp_path):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "12.50", "food"])
    main(["--file", path, "add", "2024-03-06", "3", "transport"])
    main(["--file", path, "add", "2024-04-07", "8.20", "food"])
    return path


def run(capsys, *argv):
    capsys.readouterr()
    assert main(list(argv)) == 0
    return capsys.readouterr().out


def test_json_by_category(data, capsys):
    out = run(capsys, "--file", data, "report", "--json")
    assert json.loads(out) == {"food": "20.70", "transport": "3"}


def test_json_by_month(data, capsys):
    out = run(capsys, "--file", data, "report", "--by", "month", "--json")
    assert json.loads(out) == {"2024-03": "15.50", "2024-04": "8.20"}
    assert list(json.loads(out)) == ["2024-03", "2024-04"]


def test_json_option_before_by(data, capsys):
    out = run(capsys, "--file", data, "report", "--json", "--by", "month")
    assert json.loads(out) == {"2024-03": "15.50", "2024-04": "8.20"}


def test_json_output_is_a_single_document(data, capsys):
    out = run(capsys, "--file", data, "report", "--json")
    assert len(out.strip().splitlines()) >= 1
    json.loads(out)  # the whole output parses, nothing else is printed


def test_json_empty_ledger(tmp_path, capsys):
    path = str(tmp_path / "none.json")
    assert json.loads(run(capsys, "--file", path, "report", "--json")) == {}
    assert json.loads(run(capsys, "--file", path, "report", "--by", "month", "--json")) == {}


def test_text_report_is_unchanged(data, capsys):
    assert run(capsys, "--file", data, "report").splitlines() == ["food  20.70", "transport  3"]
