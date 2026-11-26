"""Checks that notes given with `--note` are saved and shown again by `ledger list`."""

import json

from ledger.cli import main


def test_list_shows_the_note(tmp_path, capsys):
    path = str(tmp_path / "ledger.json")
    assert main(["--file", path, "add", "2024-03-05", "12.50", "food", "--note", "lunch"]) == 0
    capsys.readouterr()
    assert main(["--file", path, "list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["2024-03-05  12.50  food  lunch"]


def test_notes_survive_adding_more_expenses(tmp_path, capsys):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "12.50", "food", "--note", "lunch with Ana"])
    main(["--file", path, "add", "2024-03-06", "3", "transport"])
    main(["--file", path, "add", "2024-03-07", "8", "food", "--note", "dinner"])
    capsys.readouterr()
    main(["--file", path, "list"])
    assert capsys.readouterr().out.splitlines() == [
        "2024-03-05  12.50  food  lunch with Ana",
        "2024-03-06  3  transport",
        "2024-03-07  8  food  dinner",
    ]
    with open(path, encoding="utf-8") as f:
        assert [item["note"] for item in json.load(f)] == ["lunch with Ana", "", "dinner"]
