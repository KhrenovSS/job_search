"""Send the daily digest: header, then card + cover letter for each lead, and record it."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from aiogram import Bot

from hh_scout.bot.keyboards import vote_kb
from hh_scout.config import Settings
from hh_scout.llm.cover_letter import CoverLetterWriter
from hh_scout.pipeline import repo
from hh_scout.pipeline.digest_builder import finalize_digest, plan_digest
from hh_scout.pipeline.ranker import digest_header, format_card, format_letter

log = logging.getLogger(__name__)
PAUSE_S = 0.6


async def send_digest(bot: Bot, conn: sqlite3.Connection, settings: Settings, chat_id: int, note: str | None = None) -> int:
    plan = plan_digest(conn, settings)
    if not plan.leads:
        await bot.send_message(chat_id, digest_header(0, plan.checked))
        finalize_digest(conn, settings, [], plan.checked, note)
        return 0
    # letters for leads that still lack one (e.g. bridge was down during the crawl)
    missing = [r for r in plan.leads if not r["letter"]]
    if missing:
        try:
            writer = CoverLetterWriter(settings, conn)
            for r in missing:
                await asyncio.to_thread(writer.write_for, r)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось дописать письма перед дайджестом: %s", e)
        plan = plan_digest(conn, settings)

    await bot.send_message(chat_id, digest_header(len(plan.leads), plan.checked))
    sent: list[tuple[sqlite3.Row, int | None]] = []
    for i, row in enumerate(plan.leads, 1):
        await asyncio.sleep(PAUSE_S)
        msg = await bot.send_message(chat_id, format_card(i, row, row), reply_markup=vote_kb(row["id"]))
        sent.append((row, msg.message_id))
        if row["letter"]:
            await asyncio.sleep(PAUSE_S)
            await bot.send_message(chat_id, format_letter(row["employer"], row["letter"]))
    finalize_digest(conn, settings, sent, plan.checked, note)
    return len(sent)
