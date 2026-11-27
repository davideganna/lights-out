"""Hidden acceptance tests: `ledger list` ends with a total line."""

import pytest

from ledger.cli import main


@pytest.fixture
def data(tmp_path):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-05", "12.50", "food", "--note", "lunch"])
    main(["--file", path, "add", "2024-03-06", "3", "transport"])
    main(["--file", path, "add", "2024-03-07", "8.20", "food", "--note", "dinner"])
    main(["--file", path, "add", "2024-03-08", "950", "rent"])
    return path


def test_list_ends_with_a_total_line(data, capsys):
    capsys.readouterr()
    assert main(["--file", data, "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    expenses = [
        "2024-03-05  12.50  food  lunch",
        "2024-03-06  3  transport",
        "2024-03-07  8.20  food  dinner",
        "2024-03-08  950  rent",
    ]
    assert lines[: len(expenses)] == expenses
    assert len(lines) > len(expenses)
    total = lines[-1].lower()
    assert "total" in total
    assert "973.70" in total
    for line in lines[len(expenses) : -1]:
        assert set(line) <= set("-= \t")
