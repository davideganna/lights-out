"""Checks the storage refactor: MemoryStore and JsonFileStore, and `main(argv, store=...)`; CLI output unchanged."""

import datetime
from decimal import Decimal

from ledger.cli import main
from ledger.models import Expense


def test_cli_with_a_memory_store_touches_no_files(tmp_path, monkeypatch, capsys):
    from ledger.storage import MemoryStore

    monkeypatch.chdir(tmp_path)
    store = MemoryStore()
    assert main(["add", "2024-03-05", "12.50", "food", "--note", "lunch"], store=store) == 0
    assert main(["add", "2024-03-06", "3", "transport"], store=store) == 0
    capsys.readouterr()
    assert main(["list"], store=store) == 0
    assert capsys.readouterr().out.splitlines() == ["2024-03-05  12.50  food  lunch", "2024-03-06  3  transport"]
    assert store.load() == [
        Expense(datetime.date(2024, 3, 5), Decimal("12.50"), "food", "lunch"),
        Expense(datetime.date(2024, 3, 6), Decimal("3"), "transport"),
    ]
    assert list(tmp_path.iterdir()) == []


def test_given_store_wins_over_file_option(tmp_path, monkeypatch):
    from ledger.storage import MemoryStore

    monkeypatch.chdir(tmp_path)
    store = MemoryStore()
    other = tmp_path / "other.json"
    assert main(["--file", str(other), "add", "2024-03-05", "1", "food"], store=store) == 0
    assert len(store.load()) == 1
    assert not other.exists()


def test_memory_store_starts_from_a_copy(tmp_path):
    from ledger.storage import MemoryStore

    start = [Expense(datetime.date(2024, 3, 5), Decimal("1"), "food")]
    store = MemoryStore(start)
    assert store.load() == start
    main(["add", "2024-03-06", "2", "food"], store=store)
    assert len(store.load()) == 2
    assert len(start) == 1
    assert MemoryStore().load() == []


def test_memory_store_reports(capsys):
    from ledger.storage import MemoryStore

    store = MemoryStore()
    main(["add", "2024-03-05", "10", "food"], store=store)
    main(["add", "2024-03-06", "5", "food"], store=store)
    capsys.readouterr()
    assert main(["report"], store=store) == 0
    assert capsys.readouterr().out.splitlines() == ["food  15"]


def test_json_file_store_round_trip(tmp_path):
    from ledger.storage import JsonFileStore

    path = tmp_path / "l.json"
    store = JsonFileStore(path)
    assert store.load() == []
    items = [
        Expense(datetime.date(2024, 3, 5), Decimal("12.50"), "food", "lunch"),
        Expense(datetime.date(2024, 3, 6), Decimal("3"), "transport"),
    ]
    store.save(items)
    assert JsonFileStore(path).load() == items
    assert path.exists()


def test_cli_with_a_json_file_store(tmp_path, capsys):
    from ledger.storage import JsonFileStore

    store = JsonFileStore(tmp_path / "l.json")
    assert main(["add", "2024-03-05", "12.50", "food"], store=store) == 0
    capsys.readouterr()
    assert main(["list"], store=store) == 0
    assert capsys.readouterr().out.splitlines() == ["2024-03-05  12.50  food"]
    assert len(JsonFileStore(tmp_path / "l.json").load()) == 1


def test_file_option_behaves_as_before(tmp_path, capsys):
    path = str(tmp_path / "ledger.json")
    assert main(["--file", path, "add", "2024-03-05", "12.50", "food", "--note", "lunch"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Added 12.50 to food on 2024-03-05"]
    assert main(["--file", path, "list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["2024-03-05  12.50  food  lunch"]
    assert main(["--file", path, "report", "--by", "month"]) == 0
    assert capsys.readouterr().out.splitlines() == ["2024-03  12.50"]
    assert (tmp_path / "ledger.json").exists()
