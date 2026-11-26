# ledger

A tiny personal-expenses library and command line tool. Expenses are kept in a JSON file.

```
ledger add 2024-01-31 950.00 rent --note "January"
ledger add 2024-02-03 12.50 food
ledger list
ledger report --by category   # or --by month
```

Use `--file PATH` (before the command) to pick the data file; the default is `ledger.json`.

## Development

`make check` runs ruff and the tests (needs `ruff` and `pytest` installed).
Layout: `src/ledger/` has `models.py`, `storage.py`, `reports.py` and `cli.py`.
