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


def _list(capsys, *args):
    capsys.readouterr()
    assert main(list(args)) == 0
    return capsys.readouterr().out.splitlines()


def test_filter_shows_only_that_category(data, capsys):
    lines = _list(capsys, "--file", data, "list", "--category", "food")
    assert lines == ["2024-03-05  12.50  food  lunch", "2024-03-07  8.20  food  dinner"]


def test_unfiltered_list_is_unchanged(data, capsys):
    lines = _list(capsys, "--file", data, "list")
    assert lines == [
        "2024-03-05  12.50  food  lunch",
        "2024-03-06  3  transport",
        "2024-03-07  8.20  food  dinner",
        "2024-03-08  950  rent",
    ]


def test_unknown_category_lists_nothing(data, capsys):
    assert _list(capsys, "--file", data, "list", "--category", "travel") == []


def test_filter_does_not_match_prefixes(data, capsys):
    lines = _list(capsys, "--file", data, "list", "--category", "foo")
    assert lines == []
