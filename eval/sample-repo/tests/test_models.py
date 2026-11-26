import datetime
from decimal import Decimal

from ledger.models import Expense


def test_round_trip():
    e = Expense(datetime.date(2024, 3, 5), Decimal("12.50"), "food", "lunch")
    assert Expense.from_dict(e.to_dict()) == e


def test_amount_is_stored_as_string():
    e = Expense(datetime.date(2024, 3, 5), Decimal("0.10"), "food")
    assert e.to_dict()["amount"] == "0.10"


def test_note_is_optional():
    e = Expense.from_dict({"date": "2024-03-05", "amount": "1", "category": "food"})
    assert e.note == ""
