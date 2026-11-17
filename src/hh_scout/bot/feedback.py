"""Lead card callbacks: 👍 / 👎 (+reason) feedback and ✅ Написал / ⏸ Позже actions."""

from __future__ import annotations

import logging
import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from datetime import datetime

from hh_scout.bot.keyboards import MAIL_OUTCOMES, REASONS, outcome_kb, parse_callback, reason_kb, vote_kb
from hh_scout.bot.lead_actions import close_lead, render_closed
from hh_scout.config import TZ
from hh_scout.db import kv_get, kv_set
from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import format_collapsed

log = logging.getLogger(__name__)
router = Router(name="feedback")
_REASON_LABEL = dict(REASONS)
AWAITING_REASON_KEY = "awaiting_reason"   # kv: "<vacancy_id>:<prompt message id>" while the owner types a reason
ASK_REASON_TEXT = "Напишите причину ответом на это сообщение — она попадёт в калибровку оценщика."


def awaiting_reason(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """(vacancy_id, prompt message id) if the bot is waiting for the owner's own words on a 👎."""
    raw = kv_get(conn, AWAITING_REASON_KEY) or ""
    vid, _, mid = raw.partition(":")
    return (int(vid), int(mid)) if vid.isdigit() and mid.isdigit() else None


def set_awaiting_reason(conn: sqlite3.Connection, vacancy_id: int | None, message_id: int | None = None) -> None:
    with conn:
        kv_set(conn, AWAITING_REASON_KEY, f"{vacancy_id}:{message_id}" if vacancy_id is not None else None)


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
    reason_code = None if code in ("skip", "text") else code
    with conn:
        repo.update_feedback_reason(conn, vacancy_id, reason_code)
    collapsed = close_lead(conn, vacancy_id, "disliked", reason_code)
    await cb.answer("Учтено")  # before the Telegram edits, like «✅ Написал» (v9.53)
    if collapsed:
        await render_closed(cb.bot, conn, cb.message.chat.id, vacancy_id, "disliked", reason_code)
    if code == "text":
        # the owner's own words (v9.11): the next reply to this prompt lands in feedback.reason as free text
        prompt = await cb.message.answer(ASK_REASON_TEXT)
        set_awaiting_reason(conn, vacancy_id, prompt.message_id)
    if not collapsed:  # lead was not open (e.g. preview card) — just freeze the keyboard
        from hh_scout.bot.keyboards import done_kb
        label = "👎 учтено" if code == "skip" else f"👎 {_REASON_LABEL.get(code, code)}"
        await cb.message.edit_reply_markup(reply_markup=done_kb(label))


@router.callback_query(F.data.startswith("act:"))
async def action(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parsed = parse_callback(cb.data or "")
    if not parsed:
        await cb.answer("Не понял кнопку")
        return
    _, vacancy_id, what = parsed
    if what == "responded":
        # record first, answer the tap at once, edit the chat after (v9.53): the two Telegram calls of the
        # collapse took up to 7 s on 17.11 and the spinning button was pressed twice
        if not close_lead(conn, vacancy_id, "responded"):
            await cb.answer("Лид уже закрыт")
            return
        with conn:
            repo.add_feedback(conn, vacancy_id, +1)
        await cb.answer("Отмечено: написал")
        await render_closed(cb.bot, conn, cb.message.chat.id, vacancy_id, "responded")
    elif what == "later":
        with conn:
            if not _deferred(conn, vacancy_id):
                repo.add_action(conn, vacancy_id, "deferred")
        await _refresh_kb(cb, conn, vacancy_id)
        await cb.answer("Отложено — в /inbox внизу списка")
    else:
        await cb.answer("Неизвестное действие")


def collapsed_with_outcome(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> str:
    """The collapsed line of an e-mail lead with the company's answer added; the date stays the day of the letter."""
    when = None
    raw = repo.responded_at(conn, row["id"])
    if raw:
        try:
            when = datetime.fromisoformat(raw).astimezone(TZ)
        except ValueError:
            when = None
    return format_collapsed("responded", row, when=when, outcome=kind)


@router.callback_query(F.data.startswith("out:"))
async def mail_outcome(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    """💬 Ответили / 🤝 Разговор / 🚫 Отказ under a collapsed e-mail lead (v9.47): the outcome hh.ru cannot report."""
    parsed = parse_callback(cb.data or "")
    if not parsed or parsed[2] not in repo.MAIL_STATES:
        await cb.answer("Не понял кнопку")
        return
    _, vacancy_id, kind = parsed
    row = repo.vacancy_by_id(conn, vacancy_id)
    if row is None:
        await cb.answer("Лид не найден")
        return
    with conn:
        repo.record_mail_outcome(conn, vacancy_id, kind)
    try:
        await cb.message.edit_text(collapsed_with_outcome(conn, row, kind), reply_markup=outcome_kb(vacancy_id, kind))
    except TelegramBadRequest:
        pass  # the same button twice: nothing to change
    await cb.answer(f"Записано: {dict(MAIL_OUTCOMES)[kind]}")
