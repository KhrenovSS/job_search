"""Send the daily digest: header, then card + cover letter for each lead, and record it."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from aiogram import Bot

from hh_scout.bot.keyboards import vote_kb
from hh_scout.config import Settings
from hh_scout.db import open_db
from hh_scout.llm.bridge_client import BridgeError
from hh_scout.llm.cover_letter import CoverLetterWriter
from hh_scout.llm.evaluator import Evaluator
from hh_scout.pipeline import repo
from hh_scout.pipeline.digest_builder import finalize_digest, plan_digest
from hh_scout.pipeline.ranker import digest_header, format_card, format_letter

log = logging.getLogger(__name__)
PAUSE_S = 0.6


def _evaluate_pending(settings: Settings) -> tuple[int, int]:
    """Blocking: score whatever has a description but no evaluation yet, and write letters for new leads.

    Runs in a worker thread with its own connection right before the digest, so a crawl that is still
    fetching descriptions does not delay today's digest — what is ready gets sent, the rest waits for tomorrow.
    """
    conn = open_db(settings.db_path)
    try:
        ev = Evaluator(settings, conn).run()
        lw = CoverLetterWriter(settings, conn).run()
        return ev.evaluated, lw.written
    finally:
        conn.close()


async def send_digest(bot: Bot, conn: sqlite3.Connection, settings: Settings, chat_id: int, note: str | None = None) -> int:
    try:
        evaluated, letters = await asyncio.to_thread(_evaluate_pending, settings)
        if evaluated or letters:
            log.info("Перед дайджестом дооценено %d, писем %d", evaluated, letters)
    except BridgeError as e:
        log.warning("Перед дайджестом не удалось дооценить (мост): %s", e)
    except Exception as e:  # noqa: BLE001
        log.exception("Дооценка перед дайджестом упала: %s", e)
    plan = plan_digest(conn, settings)
    open_before = len(repo.open_leads(conn))
    if not plan.leads:
        await bot.send_message(chat_id, digest_header(0, plan.checked, open_before=open_before))
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

    await bot.send_message(chat_id, digest_header(len(plan.leads), plan.checked, open_before=open_before))
    sent: list[tuple[sqlite3.Row, int | None, int | None]] = []
    for i, row in enumerate(plan.leads, 1):
        await asyncio.sleep(PAUSE_S)
        msg = await bot.send_message(chat_id, format_card(i, row, row), reply_markup=vote_kb(row["id"]))
        letter_id = None
        if row["letter"]:
            await asyncio.sleep(PAUSE_S)
            letter_msg = await bot.send_message(chat_id, format_letter(row["employer"], row["letter"]))
            letter_id = letter_msg.message_id
        sent.append((row, msg.message_id, letter_id))
    finalize_digest(conn, settings, sent, plan.checked, note)
    return len(sent)
