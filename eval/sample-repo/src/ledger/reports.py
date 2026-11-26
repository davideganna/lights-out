"""Totals over a list of expenses."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import Expense


def totals_by_category(expenses: list[Expense]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for e in expenses:
        totals[e.category] += e.amount
    return dict(sorted(totals.items()))


def totals_by_month(expenses: list[Expense]) -> dict[str, Decimal]:
    """Totals per calendar month, keyed "YYYY-MM"."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for e in expenses:
        totals[e.date.strftime("%Y-%m")] += e.amount
    return dict(sorted(totals.items()))
