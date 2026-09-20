"""Send the daily digest and the instant leads: header, then card + cover letter for each lead, and record it.

Both senders share one delivery loop (`_deliver`) and are serialised by the caller (`main.py` holds one
`asyncio.Lock` around them): the quota and the queue head are read before the messages go out and committed
after, so two senders interleaving on the event loop could send the same lead twice (v9.11).
"""

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
from hh_scout.pipeline import outcomes, repo
from hh_scout.pipeline.digest_builder import finalize_digest, plan_digest, promote_floor
from hh_scout.pipeline.ranker import digest_header, format_card, format_letter, format_queue_tail
from hh_scout.pipeline.rows import row_site

log = logging.getLogger(__name__)
INVITED_DAYS = 14  # how far back the header looks for invitations
PAUSE_S = 1.0  # v9.7: a digest is now ~20 messages at noon — do not crowd the Telegram API
SITES = ("hh", "profi")


class RulesBySite:
    """The current rules stamp per site, computed once per send (the prompt files are re-read each time)."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._cache: dict[str, str] = {}

    def for_row(self, row: sqlite3.Row) -> str:
        site = row_site(row)
        if site not in self._cache:
            self._cache[site] = rules_hash(self._s, site)
        return self._cache[site]

    def letter(self, row: sqlite3.Row) -> str | None:
        return usable_letter(row, self.for_row(row))


def _evaluate_pending(settings: Settings) -> tuple[int, int]:
    """Blocking: score whatever has a description but no evaluation yet, and write letters for new leads.

    Runs in a worker thread with its own connection right before the digest, so a crawl that is still
    fetching descriptions does not delay today's digest — what is ready gets sent, the rest waits for tomorrow.
    It is the only letter-writing pass the digest makes: stale letters are rewritten here too (decision #46).
    """
    conn = open_db(settings.db_path)
    try:
        ev = Evaluator(settings, conn).run()
        lw = CoverLetterWriter(settings, conn).run()
        return ev.evaluated, lw.written
    finally:
        conn.close()


async def _deliver(bot: Bot, chat_id: int, rows: list[sqlite3.Row], rules: RulesBySite) -> list[tuple[sqlite3.Row, int, int | None]]:
    """Card + letter for every row, in order; returns what `finalize_digest` records."""
    sent: list[tuple[sqlite3.Row, int, int | None]] = []
    for i, row in enumerate(rows, 1):
        await asyncio.sleep(PAUSE_S)
        msg = await bot.send_message(chat_id, format_card(i, row, row), reply_markup=vote_kb(row["id"]))
        letter_id = None
        letter = rules.letter(row)   # no usable letter (the bridge was down) — the card goes out on its own
        if letter:
            await asyncio.sleep(PAUSE_S)
            letter_msg = await bot.send_message(chat_id, format_letter(row["employer"], letter, row_site(row)))
            letter_id = letter_msg.message_id
        sent.append((row, msg.message_id, letter_id))
    return sent


async def send_digest(bot: Bot, conn: sqlite3.Connection, settings: Settings, chat_id: int, note: str | None = None,
                      *, evaluate: bool = True) -> int:
    """The noon digest. `evaluate=False` skips the pre-digest scoring pass — while a sitting is running it would
    race the sitting's own evaluation and letter quota; the sitting sends its leads itself when it ends."""
    # The daily floor first (DB only): the scoring pass below then writes letters for what it took (decision #52).
    floor_added = 0
    try:
        floor_added = promote_floor(conn, settings)
    except Exception as e:  # noqa: BLE001
        log.exception("Дневной минимум не отработал: %s", e)
    if evaluate:
        try:
            evaluated, letters = await asyncio.to_thread(_evaluate_pending, settings)
            if evaluated or letters:
                log.info("Перед дайджестом дооценено %d, писем %d", evaluated, letters)
        except BridgeError as e:
            log.warning("Перед дайджестом не удалось дооценить (мост): %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("Дооценка перед дайджестом упала: %s", e)
    else:
        log.info("Идёт подход — дооценку перед дайджестом пропускаю, он пришлёт лиды сам")
    plan = plan_digest(conn, settings)
    open_before = len(repo.open_leads(conn))
    work = repo.work_totals(conn, datetime.now(TZ) - timedelta(hours=24))
    invited = outcomes.invited_count(repo.outcome_rows(conn, repo.iso_utc(datetime.now(TZ) - timedelta(days=INVITED_DAYS))))
    header = digest_header(len(plan.leads), plan.checked, open_before=open_before, work=work,
                           invited=invited, invited_days=INVITED_DAYS, sent_today=plan.sent_today,
                           floor_added=floor_added)
    await bot.send_message(chat_id, header)
    if not plan.leads:
        finalize_digest(conn, settings, [], plan.checked, note)
        return 0
    sent = await _deliver(bot, chat_id, plan.leads, RulesBySite(settings))
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
    the rules changed is held back the same way: the next writing pass rewrites it (decision #46).
    """
    left = max(0, settings.digest_max_items - repo.leads_sent_today(conn))
    if not left:
        log.info("Мгновенная отправка (%s): суточная норма %d исчерпана", site, settings.digest_max_items)
        return 0
    rules = RulesBySite(settings)
    if site == "hh":
        queue = repo.lead_queue(conn, settings.score_threshold, None, wait_bonus_max=settings.queue_wait_bonus_max)
        leads = [r for r in queue if row_site(r) == "hh" and rules.letter(r)][:left]
    else:
        leads = repo.evaluated_leads(conn, settings.score_threshold, left, site=site)
        missing = [r for r in leads if not rules.letter(r)]
        if missing:
            writer = CoverLetterWriter(settings, conn)
            for r in missing:
                try:
                    await asyncio.to_thread(writer.write_for, r)
                except Exception as e:  # noqa: BLE001 — one failed bid must not cost the others theirs
                    log.warning("Не удалось написать предложение для заказа %s: %s", r["hh_id"], e)
            leads = repo.evaluated_leads(conn, settings.score_threshold, left, site=site)
    if not leads:
        return 0
    await bot.send_message(chat_id, INSTANT_HEADERS[site].format(n=len(leads)))
    sent = await _deliver(bot, chat_id, leads, rules)
    finalize_digest(conn, settings, sent, checked=0, note=f"instant:{site}", reject=False)
    return len(sent)
