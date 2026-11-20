"""Lead card callbacks: 👍 / 👎 (+reason) feedback and ✅ Написал / ⏸ Позже actions."""

from __future__ import annotations

import logging
import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
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


_rendering: set[int] = set()  # leads whose card is being collapsed by a tap right now (a second tap must not redo it)


async def _answer(cb: CallbackQuery, text: str | None = None) -> None:
    """Answer the tap, never letting a failed answer stop the work after it (v9.55).

    20.11: the answer to «✅ Написал» hung 24 s, Telegram replied «query is too old», the exception left the handler
    before `render_closed` — the lead was closed in the base, the card stayed in the chat, and every next tap said
    only «Лид уже закрыт»."""
    try:
        await cb.answer(text)
    except (TelegramBadRequest, TelegramNetworkError) as e:
        log.warning("Не удалось ответить на нажатие «%s»: %s", cb.data, getattr(e, "message", e))


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
    await _answer(cb)


@router.callback_query(F.data.startswith("fb:"))
async def vote(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parsed = parse_callback(cb.data or "")
    if not parsed:
        await _answer(cb, "Не понял кнопку")
        return
    _, vacancy_id, value = parsed
    if value == "up":
        with conn:
            repo.add_feedback(conn, vacancy_id, +1)
            if not _liked(conn, vacancy_id):
                repo.add_action(conn, vacancy_id, "liked")
        await _refresh_kb(cb, conn, vacancy_id)
        await _answer(cb, "Учтено как хороший лид")
    else:
        with conn:
            repo.add_feedback(conn, vacancy_id, -1)
        await cb.message.edit_reply_markup(reply_markup=reason_kb(vacancy_id))
        await _answer(cb, "Почему не подходит?")


@router.callback_query(F.data.startswith("fbr:"))
async def reason(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parsed = parse_callback(cb.data or "")
    if not parsed:
        await _answer(cb, "Не понял кнопку")
        return
    _, vacancy_id, code = parsed
    reason_code = None if code in ("skip", "text") else code
    with conn:
        repo.update_feedback_reason(conn, vacancy_id, reason_code)
    collapsed = close_lead(conn, vacancy_id, "disliked", reason_code)
    await _answer(cb, "Учтено")  # before the Telegram edits, like «✅ Написал» (v9.53)
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


async def _render_once(cb: CallbackQuery, conn: sqlite3.Connection, vacancy_id: int, kind: str,
                       reason: str | None = None) -> None:
    """Collapse the card; the caller has put `vacancy_id` into `_rendering` before its first await."""
    try:
        await render_closed(cb.bot, conn, cb.message.chat.id, vacancy_id, kind, reason)
    finally:
        _rendering.discard(vacancy_id)


@router.callback_query(F.data.startswith("act:"))
async def action(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parsed = parse_callback(cb.data or "")
    if not parsed:
        await _answer(cb, "Не понял кнопку")
        return
    _, vacancy_id, what = parsed
    if what == "responded":
        # record first, answer the tap at once, edit the chat after (v9.53): the two Telegram calls of the
        # collapse took up to 7 s on 17.11 and the spinning button was pressed twice
        if not close_lead(conn, vacancy_id, "responded"):
            closed = repo.closing_action(conn, vacancy_id)
            if closed is None or vacancy_id in _rendering:
                await _answer(cb, "Лид уже закрыт")
                return
            # the button is only on a card that was never collapsed: the earlier collapse did not reach the chat
            # (v9.55) — redo it with the action that closed the lead; no second feedback row
            _rendering.add(vacancy_id)
            await _answer(cb, "Лид уже закрыт — сворачиваю карточку")
            kind, why = closed
            await _render_once(cb, conn, vacancy_id, kind, why)
            return
        _rendering.add(vacancy_id)  # before any await: a second tap during a slow answer must not redo the collapse
        with conn:
            repo.add_feedback(conn, vacancy_id, +1)
        await _answer(cb, "Отмечено: написал")
        await _render_once(cb, conn, vacancy_id, "responded")
    elif what == "later":
        with conn:
            if not _deferred(conn, vacancy_id):
                repo.add_action(conn, vacancy_id, "deferred")
        await _refresh_kb(cb, conn, vacancy_id)
        await _answer(cb, "Отложено — в /inbox внизу списка")
    else:
        await _answer(cb, "Неизвестное действие")


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
        await _answer(cb, "Не понял кнопку")
        return
    _, vacancy_id, kind = parsed
    row = repo.vacancy_by_id(conn, vacancy_id)
    if row is None:
        await _answer(cb, "Лид не найден")
        return
    with conn:
        repo.record_mail_outcome(conn, vacancy_id, kind)
    try:
        await cb.message.edit_text(collapsed_with_outcome(conn, row, kind), reply_markup=outcome_kb(vacancy_id, kind))
    except TelegramBadRequest:
        pass  # the same button twice: nothing to change
    await _answer(cb, f"Записано: {dict(MAIL_OUTCOMES)[kind]}")
