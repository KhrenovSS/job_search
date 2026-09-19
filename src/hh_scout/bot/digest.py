"""Send the daily digest: header, then card + cover letter for each lead, and record it."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import LinkPreviewOptions

from hh_scout.bot.keyboards import vote_kb
from hh_scout.config import TZ, Settings
from hh_scout.db import open_db
from hh_scout.llm.bridge_client import BridgeError
from hh_scout.llm.cover_letter import CoverLetterWriter, rules_hash, usable_letter
from hh_scout.llm.evaluator import Evaluator
from hh_scout.pipeline import repo
from hh_scout.pipeline.digest_builder import finalize_digest, plan_digest
from hh_scout.pipeline.ranker import (digest_header, format_card, format_letter, format_queue_tail,
                                      row_site)

log = logging.getLogger(__name__)
INVITED_DAYS = 14  # how far back the header looks for invitations
PAUSE_S = 1.0  # v9.7: a digest is now ~20 messages at noon — do not crowd the Telegram API


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
    work = repo.work_totals(conn, datetime.now(TZ) - timedelta(hours=24))
    invited = repo.invited_since(conn, (datetime.now(TZ) - timedelta(days=INVITED_DAYS)).isoformat())
    if not plan.leads:
        await bot.send_message(chat_id, digest_header(0, plan.checked, open_before=open_before, work=work,
                                                  invited=invited, invited_days=INVITED_DAYS,
                                                  sent_today=plan.sent_today))
        finalize_digest(conn, settings, [], plan.checked, note)
        return 0
    # letters for leads that still lack a usable one: none at all (the bridge was down during the crawl),
    # or one written before the rules changed — a stale letter counts as missing (decision #46)
    rules = rules_hash(settings)
    missing = [r for r in plan.leads if not usable_letter(r, rules)]
    if missing:
        try:
            writer = CoverLetterWriter(settings, conn)
            for r in missing:
                await asyncio.to_thread(writer.write_for, r)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось дописать письма перед дайджестом: %s", e)
        plan = plan_digest(conn, settings)

    await bot.send_message(chat_id, digest_header(len(plan.leads), plan.checked, open_before=open_before, work=work,
                                                  invited=invited, invited_days=INVITED_DAYS,
                                                  sent_today=plan.sent_today))
    sent: list[tuple[sqlite3.Row, int | None, int | None]] = []
    for i, row in enumerate(plan.leads, 1):
        await asyncio.sleep(PAUSE_S)
        msg = await bot.send_message(chat_id, format_card(i, row, row), reply_markup=vote_kb(row["id"]))
        letter_id = None
        letter = usable_letter(row, rules)   # rewriting failed (bridge down) — the card goes out on its own
        if letter:
            await asyncio.sleep(PAUSE_S)
            letter_msg = await bot.send_message(chat_id, format_letter(row["employer"], letter, row_site(row)))
            letter_id = letter_msg.message_id
        sent.append((row, msg.message_id, letter_id))
    if plan.waiting:
        await asyncio.sleep(PAUSE_S)
        await bot.send_message(chat_id, format_queue_tail(plan.waiting, plan.waiting_total),
                               link_preview_options=LinkPreviewOptions(is_disabled=True))
    finalize_digest(conn, settings, sent, plan.checked, note)
    return len(sent)


INSTANT_HEADERS = {
    "profi": "⚡ Новые заказы на profi.ru: {n}. Отклики там платные и разбирают быстро.",
    "hh": "⚡ Новые лиды: {n}. Письма готовы — чем раньше отклик, тем он заметнее.",
}


async def send_instant_leads(bot: Bot, conn: sqlite3.Connection, settings: Settings, chat_id: int, site: str = "profi") -> int:
    """Right after a crawl: send the new leads of `site` at once instead of holding them until noon.

    Same card, buttons and letter as in the digest; recorded as a digest with note 'instant:<site>' so feedback,
    collapsing and /inbox work. Nothing below the threshold is rejected here — that is the noon digest's job.

    Two sites, two rhythms. profi.ru orders are taken within hours and their bid is short, so a missing one is
    written here and now. hh leads come off the queue in priority order and their letters were already written
    by the run that found them (`run.py` step 6); a lead whose letter did not make it waits for the next sitting
    or for the noon digest rather than holding up the chat for minutes of bridge calls. A letter written before
    the rules changed is held back the same way: the noon digest rewrites it (decision #46).
    """
    left = max(0, settings.digest_max_items - repo.leads_sent_today(conn))
    if not left:
        log.info("Мгновенная отправка (%s): суточная норма %d исчерпана", site, settings.digest_max_items)
        return 0
    rules = rules_hash(settings, site)
    if site == "hh":
        leads = [r for r in repo.lead_queue(conn, settings.score_threshold, left,
                                            wait_bonus_max=settings.queue_wait_bonus_max)
                 if usable_letter(r, rules)]
    else:
        leads = repo.evaluated_leads(conn, settings.score_threshold, left, site=site)
        missing = [r for r in leads if not usable_letter(r, rules)]
        if missing:
            try:
                writer = CoverLetterWriter(settings, conn)
                for r in missing:
                    await asyncio.to_thread(writer.write_for, r)
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось написать предложение для заказа: %s", e)
            leads = repo.evaluated_leads(conn, settings.score_threshold, left, site=site)
    if not leads:
        return 0
    await bot.send_message(chat_id, INSTANT_HEADERS[site].format(n=len(leads)))
    sent: list[tuple[sqlite3.Row, int | None, int | None]] = []
    for i, row in enumerate(leads, 1):
        await asyncio.sleep(PAUSE_S)
        msg = await bot.send_message(chat_id, format_card(i, row, row), reply_markup=vote_kb(row["id"]))
        letter_id = None
        letter = usable_letter(row, rules)
        if letter:
            await asyncio.sleep(PAUSE_S)
            letter_msg = await bot.send_message(chat_id, format_letter(row["employer"], letter, row_site(row)))
            letter_id = letter_msg.message_id
        sent.append((row, msg.message_id, letter_id))
    finalize_digest(conn, settings, sent, checked=0, note=f"instant:{site}", reject=False)
    return len(sent)
