"""Command line interface: ledger add | list | report."""

from __future__ import annotations

import argparse
import datetime
from decimal import Decimal, InvalidOperation

from . import reports, storage
from .models import Expense


def _date(text: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (use YYYY-MM-DD): {text!r}") from None


def _amount(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not an amount: {text!r}") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(f"amount must be a positive number: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description="Track personal expenses.")
    parser.add_argument("--file", default="ledger.json", help="data file (default: ledger.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="record an expense")
    add.add_argument("date", type=_date, help="YYYY-MM-DD")
    add.add_argument("amount", type=_amount)
    add.add_argument("category")
    add.add_argument("--note", default="")

    sub.add_parser("list", help="show all expenses")

    report = sub.add_parser("report", help="show totals")
    report.add_argument("--by", choices=["category", "month"], default="category")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    expenses = storage.load(args.file)

    if args.command == "add":
        expenses.append(Expense(args.date, args.amount, args.category, args.note))
        storage.save(args.file, expenses)
        print(f"Added {args.amount} to {args.category} on {args.date.isoformat()}")
    elif args.command == "list":
        for e in expenses:
            print(f"{e.date.isoformat()}  {e.amount}  {e.category}  {e.note}".rstrip())
    elif args.command == "report":
        totals = reports.totals_by_month(expenses) if args.by == "month" else reports.totals_by_category(expenses)
        for key, total in totals.items():
            print(f"{key}  {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
