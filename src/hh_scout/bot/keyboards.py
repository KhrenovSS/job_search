from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

REASONS = [("salary", "💰 зарплата"), ("format", "🏢 формат"), ("stack", "🔧 не мой стек"), ("agency", "🏷 агентство"), ("skip", "пропустить")]


def vote_kb(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍", callback_data=f"fb:{vacancy_id}:up"),
        InlineKeyboardButton(text="👎", callback_data=f"fb:{vacancy_id}:down"),
    ]])


def reason_kb(vacancy_id: int) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=label, callback_data=f"fbr:{vacancy_id}:{code}") for code, label in REASONS[:2]]
    row2 = [InlineKeyboardButton(text=label, callback_data=f"fbr:{vacancy_id}:{code}") for code, label in REASONS[2:4]]
    row3 = [InlineKeyboardButton(text=REASONS[4][1], callback_data=f"fbr:{vacancy_id}:skip")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])


def done_kb(label: str = "✓ учтено") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="noop")]])


def parse_callback(data: str) -> tuple[str, int, str] | None:
    """'fb:12:up' -> ('fb', 12, 'up'); None if malformed."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] not in ("fb", "fbr") or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1]), parts[2]
