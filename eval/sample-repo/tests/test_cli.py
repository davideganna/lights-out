import pytest

from ledger.cli import main


@pytest.fixture
def data(tmp_path):
    return str(tmp_path / "ledger.json")


def test_add_and_list(data, capsys):
    assert main(["--file", data, "add", "2024-03-05", "12.50", "food", "--note", "lunch"]) == 0
    assert main(["--file", data, "add", "2024-03-06", "3", "transport"]) == 0
    capsys.readouterr()
    assert main(["--file", data, "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "2024-03-05  12.50  food  lunch",
        "2024-03-06  3  transport",
    ]


def test_report_by_category(data, capsys):
    main(["--file", data, "add", "2024-03-05", "10", "food"])
    main(["--file", data, "add", "2024-03-06", "5", "food"])
    capsys.readouterr()
    main(["--file", data, "report"])
    assert capsys.readouterr().out.splitlines() == ["food  15"]


def test_report_by_month(data, capsys):
    main(["--file", data, "add", "2024-03-05", "10", "food"])
    main(["--file", data, "add", "2024-04-06", "5", "food"])
    capsys.readouterr()
    main(["--file", data, "report", "--by", "month"])
    assert capsys.readouterr().out.splitlines() == ["2024-03  10", "2024-04  5"]


@pytest.mark.parametrize("bad", [["nope", "1", "x"], ["2024-03-05", "abc", "x"], ["2024-03-05", "-1", "x"]])
def test_add_rejects_bad_input(data, bad):
    with pytest.raises(SystemExit) as exc:
        main(["--file", data, "add", *bad])
    assert exc.value.code == 2
