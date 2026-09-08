"""👍/👎 callbacks: store feedback, ask a reason after 👎, replace the keyboard with «✓ учтено»."""

from __future__ import annotations

import logging
import sqlite3

from aiogram import F, Router
from aiogram.types import CallbackQuery

from hh_scout.bot.keyboards import REASONS, done_kb, parse_callback, reason_kb
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)
router = Router(name="feedback")
_REASON_LABEL = dict(REASONS)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data.startswith("fb:"))
async def vote(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parsed = parse_callback(cb.data or "")
    if not parsed:
        await cb.answer("Не понял кнопку")
        return
    _, vacancy_id, value = parsed
    if value == "up":
        with conn:
            repo.add_feedback(conn, vacancy_id, +1)
        await cb.message.edit_reply_markup(reply_markup=done_kb("👍 учтено"))
        await cb.answer("Спасибо, учтено")
    else:
        with conn:
            repo.add_feedback(conn, vacancy_id, -1)
        await cb.message.edit_reply_markup(reply_markup=reason_kb(vacancy_id))
        await cb.answer("Почему не подходит?")


@router.callback_query(F.data.startswith("fbr:"))
async def reason(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parsed = parse_callback(cb.data or "")
    if not parsed:
        await cb.answer("Не понял кнопку")
        return
    _, vacancy_id, code = parsed
    with conn:
        repo.update_feedback_reason(conn, vacancy_id, None if code == "skip" else code)
    label = "👎 учтено" if code == "skip" else f"👎 {_REASON_LABEL.get(code, code)}"
    await cb.message.edit_reply_markup(reply_markup=done_kb(label))
    await cb.answer("Учтено")
