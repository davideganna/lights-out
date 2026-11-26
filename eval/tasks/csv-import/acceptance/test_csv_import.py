"""Checks `ledger import PATH`: header-driven CSV import, all-or-nothing, errors name the bad line."""

import pytest

from ledger.cli import main


def run(capsys, *argv):
    capsys.readouterr()
    try:
        code = main(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def data(tmp_path):
    return str(tmp_path / "ledger.json")


def write_csv(tmp_path, text):
    path = tmp_path / "bank.csv"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_import_basic(tmp_path, data, capsys):
    csv_file = write_csv(tmp_path, "date,amount,category\n2024-03-05,12.50,food\n2024-03-06,3,transport\n")
    code, out, _ = run(capsys, "--file", data, "import", csv_file)
    assert (code, out.splitlines()) == (0, ["Imported 2 expenses"])
    assert run(capsys, "--file", data, "list")[1].splitlines() == [
        "2024-03-05  12.50  food",
        "2024-03-06  3  transport",
    ]


def test_columns_in_any_order_with_note(tmp_path, data, capsys):
    csv_file = write_csv(tmp_path, "note,category,amount,date\nlunch,food,12.50,2024-03-05\n,rent,950,2024-03-06\n")
    code, out, _ = run(capsys, "--file", data, "import", csv_file)
    assert (code, out.splitlines()) == (0, ["Imported 2 expenses"])
    assert run(capsys, "--file", data, "list")[1].splitlines() == [
        "2024-03-05  12.50  food  lunch",
        "2024-03-06  950  rent",
    ]


def test_import_appends_to_existing_expenses(tmp_path, data, capsys):
    run(capsys, "--file", data, "add", "2024-01-01", "1", "food")
    csv_file = write_csv(tmp_path, "date,amount,category\n2024-03-05,12.50,food\n")
    assert run(capsys, "--file", data, "import", csv_file)[0] == 0
    assert run(capsys, "--file", data, "list")[1].splitlines() == ["2024-01-01  1  food", "2024-03-05  12.50  food"]


def test_import_one_expense(tmp_path, data, capsys):
    csv_file = write_csv(tmp_path, "date,amount,category\n2024-03-05,12.50,food\n")
    assert run(capsys, "--file", data, "import", csv_file)[1].splitlines() == ["Imported 1 expenses"]


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ("2024-03-05,12.50,food\n2024-02-30,3,transport\n", 3),
        ("2024-03-05,abc,food\n", 2),
        ("2024-03-05,12.50,food\n2024-03-06,-3,transport\n", 3),
        ("2024-03-05,12.50,food\n2024-03-06,3,transport\n2024-03-07,,food\n", 4),
        ("2024-03-05,12.50,\n", 2),
        ("nope,12.50,food\n", 2),
    ],
)
def test_one_bad_row_imports_nothing(tmp_path, data, capsys, body, line):
    run(capsys, "--file", data, "add", "2024-01-01", "1", "food")
    before = open(data, encoding="utf-8").read()
    csv_file = write_csv(tmp_path, "date,amount,category\n" + body)
    code, out, err = run(capsys, "--file", data, "import", csv_file)
    assert code != 0
    assert f"line {line}" in out + err
    assert not out.startswith("Imported")
    assert open(data, encoding="utf-8").read() == before


def test_bad_import_into_a_new_ledger_creates_nothing(tmp_path, data, capsys):
    csv_file = write_csv(tmp_path, "date,amount,category\n2024-03-05,abc,food\n")
    assert run(capsys, "--file", data, "import", csv_file)[0] != 0
    assert run(capsys, "--file", data, "list")[1] == ""
