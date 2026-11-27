"""Hidden acceptance tests: `ledger list` shows expenses sorted by date."""

import pytest

from ledger.cli import main

LINES = {
    "2024-03-05": "2024-03-05  12.50  food  lunch",
    "2024-03-06": "2024-03-06  3  transport",
    "2024-03-07": "2024-03-07  8.20  food  dinner",
    "2024-03-08": "2024-03-08  950  rent",
}


@pytest.fixture
def data(tmp_path):
    path = str(tmp_path / "ledger.json")
    main(["--file", path, "add", "2024-03-07", "8.20", "food", "--note", "dinner"])
    main(["--file", path, "add", "2024-03-05", "12.50", "food", "--note", "lunch"])
    main(["--file", path, "add", "2024-03-08", "950", "rent"])
    main(["--file", path, "add", "2024-03-06", "3", "transport"])
    return path


def test_list_is_sorted_by_date_in_either_direction(data, capsys):
    capsys.readouterr()
    assert main(["--file", data, "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    ascending = [LINES[d] for d in sorted(LINES)]
    assert lines in (ascending, ascending[::-1])
