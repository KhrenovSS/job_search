"""Lead card callbacks: 👍 / 👎 (+reason) feedback and ✅ Написал / ⏸ Позже actions."""

from __future__ import annotations

import logging
import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from hh_scout.bot.keyboards import REASONS, parse_callback, reason_kb, vote_kb
from hh_scout.bot.lead_actions import collapse_lead
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)
router = Router(name="feedback")
_REASON_LABEL = dict(REASONS)


def _deferred(conn: sqlite3.Connection, vacancy_id: int) -> bool:
    return conn.execute("SELECT 1 FROM lead_actions WHERE vacancy_id = ? AND action = 'deferred'", (vacancy_id,)).fetchone() is not None


def _liked(conn: sqlite3.Connection, vacancy_id: int) -> bool:
    return conn.execute("SELECT 1 FROM lead_actions WHERE vacancy_id = ? AND action = 'liked'", (vacancy_id,)).fetchone() is not None


async def _refresh_kb(cb: CallbackQuery, conn: sqlite3.Connection, vacancy_id: int) -> None:
    try:
        await cb.message.edit_reply_markup(reply_markup=vote_kb(vacancy_id, liked=_liked(conn, vacancy_id), deferred=_deferred(conn, vacancy_id)))
    except TelegramBadRequest:
        pass  # keyboard unchanged


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
            if not _liked(conn, vacancy_id):
                repo.add_action(conn, vacancy_id, "liked")
        await _refresh_kb(cb, conn, vacancy_id)
        await cb.answer("Учтено как хороший лид")
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
    reason_code = None if code == "skip" else code
    with conn:
        repo.update_feedback_reason(conn, vacancy_id, reason_code)
    collapsed = await collapse_lead(cb.bot, conn, cb.message.chat.id, vacancy_id, "disliked", reason_code)
    if not collapsed:  # lead was not open (e.g. preview card) — just freeze the keyboard
        from hh_scout.bot.keyboards import done_kb
        label = "👎 учтено" if code == "skip" else f"👎 {_REASON_LABEL.get(code, code)}"
        await cb.message.edit_reply_markup(reply_markup=done_kb(label))
    await cb.answer("Учтено")


@router.callback_query(F.data.startswith("act:"))
async def action(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parsed = parse_callback(cb.data or "")
    if not parsed:
        await cb.answer("Не понял кнопку")
        return
    _, vacancy_id, what = parsed
    if what == "responded":
        with conn:
            repo.add_feedback(conn, vacancy_id, +1)
        if await collapse_lead(cb.bot, conn, cb.message.chat.id, vacancy_id, "responded"):
            await cb.answer("Отмечено: написал")
        else:
            await cb.answer("Лид уже закрыт")
    elif what == "later":
        with conn:
            if not _deferred(conn, vacancy_id):
                repo.add_action(conn, vacancy_id, "deferred")
        await _refresh_kb(cb, conn, vacancy_id)
        await cb.answer("Отложено — в /inbox внизу списка")
    else:
        await cb.answer("Неизвестное действие")
