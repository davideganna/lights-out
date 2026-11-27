"""Checks that `ledger report` (by category and by month) adds amounts exactly, with no float rounding noise."""

import pytest

from ledger.cli import main


def _report(tmp_path, capsys, amounts, *report_args):
    path = str(tmp_path / "ledger.json")
    for i, amount in enumerate(amounts):
        assert main(["--file", path, "add", f"2024-03-{i + 1:02d}", amount, "food"]) == 0
    capsys.readouterr()
    assert main(["--file", path, "report", *report_args]) == 0
    return capsys.readouterr().out.splitlines()


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        (["0.10", "0.20"], "0.30"),
        (["0.1", "0.2"], "0.3"),
        (["19.99", "19.99", "19.99"], "59.97"),
        (["1.10", "2.20", "3.30"], "6.60"),
    ],
)
def test_report_by_category_is_exact(tmp_path, capsys, amounts, expected):
    assert _report(tmp_path, capsys, amounts) == [f"food  {expected}"]


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [(["0.10", "0.20"], "0.30"), (["19.99", "19.99", "19.99"], "59.97")],
)
def test_report_by_month_is_exact(tmp_path, capsys, amounts, expected):
    assert _report(tmp_path, capsys, amounts, "--by", "month") == [f"2024-03  {expected}"]


def test_whole_totals_are_still_shown_without_decimals(tmp_path, capsys):
    assert _report(tmp_path, capsys, ["10", "5"]) == ["food  15"]
