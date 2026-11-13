from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

REASONS = [("salary", "💰 зарплата"), ("format", "🏢 формат"), ("stack", "🔧 не мой стек"), ("agency", "🏷 агентство"),
           ("text", "✍️ своими словами"), ("skip", "пропустить")]


def vote_kb(vacancy_id: int, *, liked: bool = False, deferred: bool = False) -> InlineKeyboardMarkup:
    """Lead card keyboard: feedback row + action row."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 отмечено" if liked else "👍", callback_data=f"fb:{vacancy_id}:up"),
         InlineKeyboardButton(text="👎", callback_data=f"fb:{vacancy_id}:down")],
        [InlineKeyboardButton(text="✅ Написал", callback_data=f"act:{vacancy_id}:responded"),
         InlineKeyboardButton(text="⏸ отложено" if deferred else "⏸ Позже", callback_data=f"act:{vacancy_id}:later")],
    ])


MAIL_OUTCOMES = (("answered", "💬 Ответили"), ("invited", "🤝 Разговор"), ("refused", "🚫 Отказ"))


def outcome_kb(vacancy_id: int, chosen: str | None = None) -> InlineKeyboardMarkup:
    """Under the collapsed card of a lead written to by e-mail: what the company answered (v9.47). hh.ru reports
    that itself for a response; for an e-mail only the owner knows. The buttons stay, so «ответили» can become
    «разговор» a week later."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{label} ✓" if code == chosen else label, callback_data=f"out:{vacancy_id}:{code}")
        for code, label in MAIL_OUTCOMES]])


def reason_kb(vacancy_id: int) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=label, callback_data=f"fbr:{vacancy_id}:{code}") for code, label in REASONS[:2]]
    row2 = [InlineKeyboardButton(text=label, callback_data=f"fbr:{vacancy_id}:{code}") for code, label in REASONS[2:4]]
    row3 = [InlineKeyboardButton(text=label, callback_data=f"fbr:{vacancy_id}:{code}") for code, label in REASONS[4:]]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])


def done_kb(label: str = "✓ учтено") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="noop")]])


def parse_callback(data: str) -> tuple[str, int, str] | None:
    """'fb:12:up' -> ('fb', 12, 'up'); None if malformed."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] not in ("fb", "fbr", "act", "out") or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1]), parts[2]
