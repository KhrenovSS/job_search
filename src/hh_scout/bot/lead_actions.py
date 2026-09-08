"""Collapse processed lead cards in the chat and delete their letters. Shared by callbacks, commands and the scheduler."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import format_collapsed

log = logging.getLogger(__name__)


async def collapse_lead(bot: Bot, conn: sqlite3.Connection, chat_id: int, vacancy_id: int, kind: str,
                        reason: str | None = None) -> bool:
    """Record the closing action, replace the card with one line, delete the letter. Returns False if not open."""
    if not repo.is_lead_open(conn, vacancy_id):
        return False
    row = repo.vacancy_by_id(conn, vacancy_id)
    card_id, letter_id = repo.lead_messages(conn, vacancy_id)
    with conn:
        repo.add_action(conn, vacancy_id, kind, reason)
    if card_id:
        try:
            await bot.edit_message_text(format_collapsed(kind, row, reason=reason), chat_id=chat_id, message_id=card_id)
        except TelegramBadRequest as e:
            log.warning("Не удалось свернуть карточку %s: %s", vacancy_id, e.message)
    if letter_id:
        try:
            await bot.delete_message(chat_id, letter_id)
        except TelegramBadRequest as e:
            log.warning("Не удалось удалить письмо %s: %s", vacancy_id, e.message)
    return True


async def collapse_auto_responded(bot: Bot, conn: sqlite3.Connection, chat_id: int) -> int:
    """Leads the owner answered on hh.ru directly (applied=1) → collapse as auto_responded."""
    n = 0
    for row in repo.pending_auto_closes(conn):
        if await collapse_lead(bot, conn, chat_id, row["id"], "auto_responded"):
            n += 1
    if n:
        log.info("Автозакрыто лидов по откликам на hh.ru: %d", n)
    return n


async def cleanup_stale(bot: Bot, conn: sqlite3.Connection, chat_id: int, days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    n = 0
    for row in repo.open_leads_older_than(conn, cutoff):
        if await collapse_lead(bot, conn, chat_id, row["id"], "closed_stale"):
            n += 1
    return n
