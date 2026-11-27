"""The expense record."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Expense:
    date: datetime.date
    amount: Decimal
    category: str
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        """JSON-friendly form; the amount is a string so no precision is lost."""
        return {
            "date": self.date.isoformat(),
            "amount": str(self.amount),
            "category": self.category,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Expense:
        return cls(
            date=datetime.date.fromisoformat(data["date"]),
            amount=Decimal(data["amount"]),
            category=data["category"],
            note=data.get("note", ""),
        )
