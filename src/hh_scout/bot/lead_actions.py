"""Collapse processed lead cards in the chat and delete their letters. Shared by callbacks, commands and the scheduler.

A single collapse is two API calls. The bulk ones (`collapse_auto_responded` after every sitting,
`cleanup_stale` on /cleanup) can walk dozens of cards at once since v9.14, so they pause between leads and
wait out flood control instead of hammering the API.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import format_collapsed

log = logging.getLogger(__name__)
BULK_PAUSE_S = 0.5   # between leads of a bulk collapse; a single one (a button press) needs no pause


async def _wait_out_flood(call, what: str, vacancy_id: int) -> bool:
    """Run one edit/delete, waiting out Telegram's flood control once. False means the card stayed as it was."""
    for attempt in (1, 2):
        try:
            await call()
            return True
        except TelegramRetryAfter as e:
            if attempt == 2:
                log.warning("Telegram не дал %s для %s: слишком часто", what, vacancy_id)
                return False
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            log.warning("Не удалось %s для %s: %s", what, vacancy_id, e.message)
            return False
    return False


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
        await _wait_out_flood(
            lambda: bot.edit_message_text(format_collapsed(kind, row, reason=reason), chat_id=chat_id,
                                          message_id=card_id), "свернуть карточку", vacancy_id)
    if letter_id:
        await _wait_out_flood(lambda: bot.delete_message(chat_id, letter_id), "удалить письмо", vacancy_id)
    return True


async def collapse_auto_responded(bot: Bot, conn: sqlite3.Connection, chat_id: int) -> int:
    """Leads the owner answered on hh.ru directly (applied=1) → collapse as auto_responded."""
    n = 0
    for row in repo.pending_auto_closes(conn):
        if n:
            await asyncio.sleep(BULK_PAUSE_S)
        if await collapse_lead(bot, conn, chat_id, row["id"], "auto_responded"):
            n += 1
    if n:
        log.info("Автозакрыто лидов по откликам на hh.ru: %d", n)
    return n


async def cleanup_stale(bot: Bot, conn: sqlite3.Connection, chat_id: int, days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    n = 0
    for row in repo.open_leads_older_than(conn, cutoff):
        if n:
            await asyncio.sleep(BULK_PAUSE_S)
        if await collapse_lead(bot, conn, chat_id, row["id"], "closed_stale"):
            n += 1
    return n
