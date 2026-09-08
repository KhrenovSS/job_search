"""Salary normalisation for hh.ru `compensation` objects.

hh.ru shape (search cards and vacancy pages alike):
    {"from": 140000, "to": 180000, "currencyCode": "RUR", "gross": true, "mode": "MONTH", ...}
    {"noCompensation": {}}                      -> not stated
Only monthly RUR figures are converted to net; anything else is kept raw with a note
so the AI can reason about it itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from hh_scout.config import GROSS_TO_NET


@dataclass(frozen=True)
class Salary:
    from_net: int | None
    to_net: int | None
    currency: str | None
    gross: bool | None
    mode: str | None  # MONTH / HOUR / SHIFT / ... as given by hh
    note: str | None  # why the figures were not normalised, if so

    @property
    def stated(self) -> bool:
        return self.from_net is not None or self.to_net is not None

    def human(self) -> str:
        """'250–300 тыс. ₽ на руки' style string for the digest."""
        if not self.stated:
            return "зарплата не указана"
        rub = self.currency in (None, "RUR", "RUB")
        unit = "тыс. ₽" if rub else (self.currency or "")

        def k(v: int) -> str:
            # rubles in thousands ("250 тыс. ₽"), foreign currency as-is ("2 500 USD")
            n = round(v / 1000) if rub else v
            return f"{n:,}".replace(",", " ")

        if self.from_net is not None and self.to_net is not None:
            core = f"{k(self.from_net)}–{k(self.to_net)} {unit}"
        elif self.from_net is not None:
            core = f"от {k(self.from_net)} {unit}"
        else:
            core = f"до {k(self.to_net or 0)} {unit}"
        suffix = " на руки" if self.note is None else f" ({self.note})"
        return core + suffix


def normalize(compensation: dict | None) -> Salary:
    if not compensation or "noCompensation" in compensation:
        return Salary(None, None, None, None, None, None)
    raw_from = compensation.get("from")
    raw_to = compensation.get("to")
    currency = compensation.get("currencyCode")
    gross = compensation.get("gross")
    mode = compensation.get("mode") or "MONTH"
    if raw_from is None and raw_to is None:
        return Salary(None, None, currency, gross, mode, None)

    note = None
    if currency not in (None, "RUR", "RUB"):
        note = f"валюта {currency}, не пересчитано"
        factor = 1.0
    elif mode != "MONTH":
        note = f"ставка за {mode.lower()}, не месячная"
        factor = 1.0
    elif gross:
        factor = GROSS_TO_NET
    else:
        factor = 1.0

    def conv(v: int | float | None) -> int | None:
        return None if v is None else int(round(v * factor))

    return Salary(conv(raw_from), conv(raw_to), currency, gross, mode, note)
