"""Checks that `ledger add` rejects dates that do not exist (exit code 2, nothing saved) and accepts real ones."""

import pytest

from ledger.cli import main


@pytest.mark.parametrize("bad", ["2024-02-30", "2023-02-29", "2024-04-31", "2024-01-32", "2024-13-01"])
def test_impossible_date_is_rejected_and_nothing_is_saved(tmp_path, bad):
    path = tmp_path / "ledger.json"
    with pytest.raises(SystemExit) as exc:
        main(["--file", str(path), "add", bad, "20", "food"])
    assert exc.value.code == 2
    assert not path.exists()


@pytest.mark.parametrize("good", ["2024-02-29", "2024-01-31", "2023-02-28", "2024-12-31"])
def test_real_dates_are_kept_as_typed(tmp_path, capsys, good):
    path = str(tmp_path / "ledger.json")
    assert main(["--file", path, "add", good, "20", "food"]) == 0
    capsys.readouterr()
    assert main(["--file", path, "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{good}  20  food"]
