"""Entry point: Telegram bot (aiogram polling) + scheduler in one process."""

from __future__ import annotations

import asyncio
import logging
import sys

from hh_scout.bot.app import Notifier, create_bot, create_dispatcher
from hh_scout.bot.digest import send_digest
from hh_scout.config import load_settings
from hh_scout.db import kv_get, kv_set, open_db
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

    async def digest() -> int:
        if settings.tg_owner_chat_id is None:
            log.warning("Нет TG_OWNER_CHAT_ID — дайджест не отправлен")
            return 0
        return await send_digest(bot, conn, settings, settings.tg_owner_chat_id)

    scheduler = Scheduler(settings, conn, notify, digest)
    dp = create_dispatcher(settings, conn, scheduler)

    # First start: leads the owner already saw as previews must not be re-sent tomorrow.
    if kv_get(conn, "preview_marked") is None:
        n = mark_previewed_as_sent(conn, settings, note="preview before first service start")
        with conn:
            kv_set(conn, "preview_marked", "1")
        log.info("Первый запуск: %d ранее показанных лидов помечены как отправленные", n)

    scheduler.start()
    log.info("HH-Scout запущен: дайджест в %s, окно сбора %s, мост %s", settings.digest_time, settings.crawl_window, settings.bridge_url)
    await notify("🟢 HH-Scout запущен. /status — состояние, /help — команды.")
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
