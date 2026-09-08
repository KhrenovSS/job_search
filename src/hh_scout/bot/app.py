"""Bot wiring: owner-only middleware, routers, notifier."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions, TelegramObject, Update

from hh_scout.config import Settings

log = logging.getLogger(__name__)


class OwnerOnly(BaseMiddleware):
    """Silently drop every update that is not from the owner. With no owner configured, tell the sender their chat_id."""

    def __init__(self, owner_chat_id: int | None) -> None:
        self.owner = owner_chat_id

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        chat_id = None
        if isinstance(event, Update):
            if event.message:
                chat_id = event.message.chat.id
            elif event.callback_query and event.callback_query.message:
                chat_id = event.callback_query.message.chat.id
        if chat_id is None:
            return None
        if self.owner is None:
            if isinstance(event, Update) and event.message:
                await event.message.answer(f"Ваш chat_id: <code>{chat_id}</code> — впишите его в .env как TG_OWNER_CHAT_ID и перезапустите сервис.")
            return None
        if chat_id != self.owner:
            log.info("Игнорирую апдейт от чужого chat_id %s", chat_id)
            return None
        return await handler(event, data)


def create_bot(settings: Settings) -> Bot:
    return Bot(settings.tg_bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)))


def create_dispatcher(settings: Settings, conn: sqlite3.Connection, scheduler) -> Dispatcher:
    from hh_scout.bot import feedback, handlers

    dp = Dispatcher()
    dp["settings"] = settings
    dp["conn"] = conn
    dp["scheduler"] = scheduler
    dp.update.outer_middleware(OwnerOnly(settings.tg_owner_chat_id))
    dp.include_router(handlers.router)
    dp.include_router(feedback.router)
    return dp


class Notifier:
    def __init__(self, bot: Bot, chat_id: int | None) -> None:
        self.bot = bot
        self.chat_id = chat_id

    async def __call__(self, text: str) -> None:
        if self.chat_id is None:
            log.warning("Нет TG_OWNER_CHAT_ID — сообщение не отправлено: %s", text)
            return
        try:
            await self.bot.send_message(self.chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось отправить уведомление: %s", e)
