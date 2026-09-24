"""Send the daily digest and the instant leads: header, then card + cover letter for each lead, and record it.

Both senders share one delivery loop (`_deliver`) and are serialised by the caller (`main.py` holds one
`asyncio.Lock` around them): the queue head is read before the messages go out, so two senders interleaving
on the event loop could send the same lead twice (v9.11).

Since v9.14 there is no daily quota, so a send can be dozens of leads — two messages each. Every lead is
recorded the moment it lands (`record_sent_lead`), and every message goes through `send_message`, which waits
out Telegram's flood control instead of dropping the rest of the batch.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from hh_scout.bot.keyboards import vote_kb
from hh_scout.config import TZ, Settings, in_span
from hh_scout.db import open_db
from hh_scout.llm.bridge_client import BridgeError
from hh_scout.llm.cover_letter import CoverLetterWriter, rules_hash, usable_letter
from hh_scout.llm.evaluator import Evaluator
from hh_scout.pipeline import outcomes, repo
from hh_scout.pipeline.digest_builder import (close_digest, daily_quota_left, open_digest, plan_digest, skip_unreachable,
                                              promote_floor, record_sent_lead)
from hh_scout.pipeline.ranker import digest_header, format_card, format_letter, format_queue_tail
from hh_scout.pipeline.rows import letter_key, row_site

log = logging.getLogger(__name__)
INVITED_DAYS = 14  # how far back the header looks for invitations
SEND_TRIES = 3
SITES = ("hh", "profi")


def silent_now(settings: Settings, now: datetime | None = None) -> bool:
    """Inside `QUIET_HOURS` (v9.15) messages go out with `disable_notification`: sittings run at night now, and a
    lead found at 03:00 should be in the chat by morning without waking anyone."""
    return in_span((now or datetime.now(TZ)).timetz(), settings.quiet_hours_parsed)


async def send_message(bot: Bot, chat_id: int, text: str, *, settings: Settings | None = None, **kw):
    """One message, waiting out flood control. Telegram answers a burst with `retry_after` rather than an
    error worth giving up on, and since v9.14 a send can be a hundred messages long — losing the tail of the
    batch (and re-sending it next time) over one 429 is the one failure this loop must not have.
    With `settings`, the message is silent inside the quiet hours.
    """
    if settings is not None and "disable_notification" not in kw and silent_now(settings):
        kw["disable_notification"] = True
    for attempt in range(1, SEND_TRIES + 1):
        try:
            return await bot.send_message(chat_id, text, **kw)
        except (TelegramRetryAfter, TelegramNetworkError) as e:
            if attempt == SEND_TRIES:
                raise
            wait = e.retry_after + 1 if isinstance(e, TelegramRetryAfter) else 2 * attempt
            log.warning("Telegram не принял сообщение (%s) — повтор через %d с (попытка %d из %d)",
                        e, wait, attempt, SEND_TRIES)
            await asyncio.sleep(wait)


class RulesByKey:
    """The current rules stamp per prompt family (`rows.letter_key`), computed once per send."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._cache: dict[str, str] = {}

    def for_row(self, row: sqlite3.Row) -> str:
        key = letter_key(row)
        if key not in self._cache:
            self._cache[key] = rules_hash(self._s, key)
        return self._cache[key]

    def letter(self, row: sqlite3.Row) -> str | None:
        return usable_letter(row, self.for_row(row))


def _evaluate_pending(settings: Settings) -> tuple[int, int]:
    """Blocking: score whatever has a description but no evaluation yet, and write letters for new leads.

    Runs in a worker thread with its own connection right before the digest, so a crawl that is still
    fetching descriptions does not delay today's digest — what is ready gets sent, the rest waits for tomorrow.
    It is the only letter-writing pass the digest makes: stale letters are rewritten here too (decision #50).
    """
    conn = open_db(settings.db_path)
    try:
        ev = Evaluator(settings, conn).run()
        lw = CoverLetterWriter(settings, conn).run()
        return ev.evaluated, lw.written
    finally:
        conn.close()


async def _deliver(bot: Bot, conn: sqlite3.Connection, settings: Settings, chat_id: int, digest_id: int,
                   rows: list[sqlite3.Row], rules: RulesByKey) -> int:
    """Card + letter for every row, in order; each lead is recorded as soon as it lands. Returns how many went out.

    Recording per lead rather than after the loop is what makes a long send safe: if Telegram or the network
    gives up on lead 40 of 60, the 39 already in the chat are `sent` and the rest simply stay in the queue.
    """
    pause = settings.telegram_pause_s
    delivered = 0
    for i, row in enumerate(rows, 1):
        await asyncio.sleep(pause)
        msg = await send_message(bot, chat_id, format_card(i, row, row), settings=settings, reply_markup=vote_kb(row["id"]))
        try:
            letter_id = None
            # hh leads are filtered before they get here; a profi bid whose letter failed still goes out on its own
            letter = rules.letter(row)
            if letter:
                await asyncio.sleep(pause)
                letter_msg = await send_message(bot, chat_id, format_letter(row["employer"], letter, letter_key(row)),
                                                settings=settings)
                letter_id = letter_msg.message_id
            record_sent_lead(conn, digest_id, i, row, msg.message_id, letter_id)
        except BaseException:
            # The card is out but the lead is not recorded: it will go out again, whole. A card left behind now
            # would be its twin without a letter (21.09: a restart between card and letter did exactly that).
            await _unsend(bot, chat_id, msg.message_id, row["hh_id"])
            raise
        delivered = i
    return delivered


async def _unsend(bot: Bot, chat_id: int, message_id: int, hh_id: str) -> None:
    """Best effort: take back a card whose lead did not make it. Never raises — the caller is already failing."""
    try:
        await asyncio.shield(bot.delete_message(chat_id, message_id))
        log.warning("Карточка %s отозвана: доставка прервана до письма, лид остался в очереди", hh_id)
    except BaseException as e:  # noqa: BLE001 — including CancelledError: shutdown must not be blocked by this
        log.warning("Карточка %s осталась в чате без письма (%s) — лид уйдёт ещё раз целиком", hh_id, e)


def _close(conn: sqlite3.Connection, settings: Settings, digest_id: int, checked: int, reject: bool = True) -> int:
    """`close_digest` that survives the service stopping mid-send: the connection is closed under us then, and a
    second traceback would only hide the first. `repo.repair_open_digests` settles the count on the next start."""
    try:
        return close_digest(conn, settings, digest_id, checked, reject)
    except sqlite3.ProgrammingError as e:
        log.warning("Дайджест #%d не закрыт — сервис остановлен во время отправки (%s); счётчик досчитается при старте",
                    digest_id, e)
        return repo.digest_item_count_safe(conn, digest_id)


async def send_digest(bot: Bot, conn: sqlite3.Connection, settings: Settings, chat_id: int, note: str | None = None,
                      *, evaluate: bool = True) -> int:
    """The noon digest. `evaluate=False` skips the pre-digest scoring pass — while a sitting is running it would
    race the sitting's own evaluation and letter pass; the sitting sends its leads itself when it ends."""
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
    rules = RulesByKey(settings)
    # A card without its letter is half a lead, and sending it would close the lead for good. Leads whose letter
    # is not written yet (the bridge was down, the time budget ran out) wait in the tail instead — v9.14 made
    # this real: without a quota the digest takes the whole queue, letters or not.
    ready = [r for r in plan.leads if rules.letter(r)]
    held = [r for r in plan.leads if not rules.letter(r)]
    if held:
        log.info("Лидов без готового письма: %d — ждут следующего подхода", len(held))
    tail = (held + plan.waiting)[:settings.digest_tail_items]
    tail_total = plan.waiting_total + len(held)
    open_before = len(repo.open_leads(conn))
    work = repo.work_totals(conn, datetime.now(TZ) - timedelta(hours=24))
    invited = outcomes.invited_count(repo.outcome_rows(conn, repo.iso_utc(datetime.now(TZ) - timedelta(days=INVITED_DAYS))))
    header = digest_header(len(ready), plan.checked, open_before=open_before, work=work,
                           invited=invited, invited_days=INVITED_DAYS, sent_today=plan.sent_today,
                           floor_added=floor_added)
    await send_message(bot, chat_id, header, settings=settings)
    digest_id = open_digest(conn, plan.checked, note)
    try:
        if ready:
            await _deliver(bot, conn, settings, chat_id, digest_id, ready, rules)
        if tail:
            await asyncio.sleep(settings.telegram_pause_s)
            await send_message(bot, chat_id, format_queue_tail(tail, tail_total), settings=settings,
                               link_preview_options=LinkPreviewOptions(is_disabled=True))
    finally:
        # Whatever reached the chat is recorded even if the send broke off — the rest keeps its place in the queue
        sent = _close(conn, settings, digest_id, plan.checked)
    return sent


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
    the rules changed is held back the same way: the next writing pass rewrites it (decision #50).
    """
    left = daily_quota_left(settings, repo.leads_sent_today(conn))
    if left == 0:
        log.info("Мгновенная отправка (%s): суточная норма %d исчерпана", site, settings.digest_max_items)
        return 0
    rules = RulesByKey(settings)
    if site == "hh":
        # hh vacancies and company leads alike: everything that is not a profi order goes out here
        skip_unreachable(conn, settings)   # a catalogue company without an e-mail is no lead (decision #56)
        queue = repo.lead_queue(conn, settings.score_threshold, None, wait_bonus_max=settings.queue_wait_bonus_max)
        leads = [r for r in queue if row_site(r) != "profi" and rules.letter(r)]
        if left is not None:
            leads = leads[:left]
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
    await send_message(bot, chat_id, INSTANT_HEADERS[site].format(n=len(leads)), settings=settings)
    digest_id = open_digest(conn, checked=0, note=f"instant:{site}")
    try:
        await _deliver(bot, conn, settings, chat_id, digest_id, leads, rules)
    finally:
        sent = _close(conn, settings, digest_id, checked=0, reject=False)
    return sent
