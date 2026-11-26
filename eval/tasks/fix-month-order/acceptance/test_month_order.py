"""Checks that `ledger report --by month` lists months oldest first, whatever order expenses were added in."""

from ledger.cli import main


def _month_report(tmp_path, capsys, rows):
    path = str(tmp_path / "ledger.json")
    for day, amount in rows:
        assert main(["--file", path, "add", day, amount, "food"]) == 0
    capsys.readouterr()
    assert main(["--file", path, "report", "--by", "month"]) == 0
    return capsys.readouterr().out.splitlines()


def test_later_month_added_first(tmp_path, capsys):
    out = _month_report(tmp_path, capsys, [("2024-04-02", "30"), ("2024-03-05", "10")])
    assert out == ["2024-03  10", "2024-04  30"]


def test_months_across_years(tmp_path, capsys):
    rows = [("2024-01-10", "1"), ("2023-12-31", "2"), ("2024-11-01", "3"), ("2023-02-03", "4")]
    out = _month_report(tmp_path, capsys, rows)
    assert out == ["2023-02  4", "2023-12  2", "2024-01  1", "2024-11  3"]


def test_same_month_is_still_added_up(tmp_path, capsys):
    rows = [("2024-04-02", "30"), ("2024-03-05", "10"), ("2024-04-20", "5")]
    assert _month_report(tmp_path, capsys, rows) == ["2024-03  10", "2024-04  35"]
