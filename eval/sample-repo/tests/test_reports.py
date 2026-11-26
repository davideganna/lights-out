import datetime
from decimal import Decimal

from ledger.models import Expense
from ledger.reports import totals_by_category, totals_by_month


def _e(day, amount, category):
    return Expense(day, Decimal(amount), category)


def test_totals_by_category():
    items = [
        _e(datetime.date(2024, 3, 5), "10.50", "food"),
        _e(datetime.date(2024, 3, 6), "4.50", "food"),
        _e(datetime.date(2024, 3, 7), "2", "transport"),
    ]
    assert totals_by_category(items) == {"food": Decimal("15.00"), "transport": Decimal("2")}


def test_totals_by_month():
    items = [
        _e(datetime.date(2024, 3, 5), "10", "food"),
        _e(datetime.date(2024, 3, 15), "5", "food"),
        _e(datetime.date(2024, 4, 2), "7", "rent"),
    ]
    assert totals_by_month(items) == {"2024-03": Decimal("15"), "2024-04": Decimal("7")}


def test_empty():
    assert totals_by_category([]) == {}
    assert totals_by_month([]) == {}
