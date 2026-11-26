import datetime
from decimal import Decimal

from ledger import storage
from ledger.models import Expense


def test_missing_file_is_empty(tmp_path):
    assert storage.load(tmp_path / "nope.json") == []


def test_save_then_load(tmp_path):
    path = tmp_path / "l.json"
    items = [
        Expense(datetime.date(2024, 3, 5), Decimal("12.50"), "food", "lunch"),
        Expense(datetime.date(2024, 3, 6), Decimal("3"), "transport"),
    ]
    storage.save(path, items)
    assert storage.load(path) == items
