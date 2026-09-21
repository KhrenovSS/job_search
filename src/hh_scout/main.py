"""Entry point: Telegram bot (aiogram polling) + scheduler in one process."""

from __future__ import annotations

import asyncio
import logging
import sys

from hh_scout.bot.app import Notifier, create_bot, create_dispatcher
from hh_scout.bot.digest import send_digest, send_instant_leads
from hh_scout.bot.lead_actions import collapse_auto_responded
from hh_scout.config import load_settings
from hh_scout.db import kv_get, kv_set, open_db, utcnow
from hh_scout.logging_setup import setup_logging
from hh_scout.pipeline.digest_builder import mark_previewed_as_sent
from hh_scout.scheduler import Scheduler

log = logging.getLogger("hh_scout")


async def run() -> int:
    settings = load_settings()
    setup_logging(settings.log_level)
    if not settings.tg_bot_token:
        log.error("TG_BOT_TOKEN пуст — заполните .env")
        return 2
    conn = open_db(settings.db_path)
    bot = create_bot(settings)
    notify = Notifier(bot, settings.tg_owner_chat_id)

    # The noon digest and the instant sends after a sitting read the quota and the queue head before sending and
    # commit after — interleaved on one event loop they could send the same lead twice (v9.11).
    send_lock = asyncio.Lock()

    async def digest() -> int:
        if settings.tg_owner_chat_id is None:
            log.warning("Нет TG_OWNER_CHAT_ID — дайджест не отправлен")
            return 0
        async with send_lock:
            return await send_digest(bot, conn, settings, settings.tg_owner_chat_id,
                                     evaluate=not scheduler.crawl_lock.locked())

    async def after_crawl() -> None:
        if settings.tg_owner_chat_id is None:
            return
        # The run is closed by now, yet the chat work below takes minutes for dozens of leads: `svc.sh` and /status
        # read this flag so nobody restarts the service in the middle of a send (21.09: a card went out, its letter
        # did not). Cleared in `finally` and at every start, so a kill cannot leave it stuck.
        kv_set(conn, "sending_since", utcnow())
        try:
            n = await collapse_auto_responded(bot, conn, settings.tg_owner_chat_id)
            if n:
                await notify(f"✅ Свернул {n} лид(ов): вы уже откликнулись на них на hh.ru")
            # v9.8: a ready lead is not held until noon — the sooner the letter goes, the more it is worth.
            sites = ["hh"] + (["profi"] if settings.profi_enabled else [])
            for site in sites:
                try:
                    async with send_lock:
                        k = await send_instant_leads(bot, conn, settings, settings.tg_owner_chat_id, site=site)
                    if k:
                        log.info("%s: отправлено сразу %d лид(ов)", site, k)
                except Exception as e:  # noqa: BLE001
                    log.exception("Мгновенная отправка (%s) упала", site)
                    await notify(f"⚠️ Лиды ({site}) не отправлены: {e}")
        finally:
            try:
                kv_set(conn, "sending_since", None)
            except Exception:  # noqa: BLE001 — the connection may be gone if we are being stopped
                pass

    scheduler = Scheduler(settings, conn, notify, digest, after_crawl)
    dp = create_dispatcher(settings, conn, scheduler)
    dp["send_lock"] = send_lock

    # First start: leads the owner already saw as previews must not be re-sent tomorrow.
    if kv_get(conn, "preview_marked") is None:
        n = mark_previewed_as_sent(conn, settings, note="preview before first service start")
        with conn:
            kv_set(conn, "preview_marked", "1")
        log.info("Первый запуск: %d ранее показанных лидов помечены как отправленные", n)

    scheduler.start()
    log.info("HH-Scout запущен: дайджест в %s, окна сбора %s, мост %s", settings.digest_time, settings.crawl_windows, settings.bridge_url)
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        scheduler.shutdown()
        await bot.session.close()
        conn.close()
        log.info("HH-Scout остановлен")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
