"""Checks that every expense is reported in the month of its date, including the last day of a month."""

import datetime
from decimal import Decimal

import pytest

from ledger.cli import main
from ledger.models import Expense
from ledger.reports import totals_by_month

CASES = [
    (datetime.date(2024, 1, 31), "2024-01"),
    (datetime.date(2023, 2, 28), "2023-02"),
    (datetime.date(2024, 2, 29), "2024-02"),
    (datetime.date(2024, 4, 30), "2024-04"),
    (datetime.date(2024, 12, 31), "2024-12"),
    (datetime.date(2024, 1, 1), "2024-01"),
    (datetime.date(2024, 6, 15), "2024-06"),
]


@pytest.mark.parametrize(("day", "month"), CASES)
def test_report_uses_the_month_of_the_date(day, month):
    assert totals_by_month([Expense(day, Decimal("10"), "rent")]) == {month: Decimal("10")}


def test_totals_add_up_across_a_boundary():
    items = [
        Expense(datetime.date(2024, 1, 31), Decimal("950"), "rent"),
        Expense(datetime.date(2024, 1, 10), Decimal("50"), "food"),
        Expense(datetime.date(2024, 2, 1), Decimal("7"), "food"),
    ]
    assert totals_by_month(items) == {"2024-01": Decimal("1000"), "2024-02": Decimal("7")}


@pytest.mark.parametrize(("day", "month"), CASES)
def test_cli_report_by_month(tmp_path, capsys, day, month):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", day.isoformat(), "10", "rent"])
    capsys.readouterr()
    assert main(["--file", path, "report", "--by", "month"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{month}  10"]
