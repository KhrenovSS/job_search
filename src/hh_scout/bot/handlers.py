"""Owner commands."""

from __future__ import annotations

import asyncio
import logging
import socket
import sqlite3
from datetime import datetime

import httpx
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from hh_scout.bot.digest import send_digest
from hh_scout.bot.keyboards import vote_kb
from hh_scout.config import TZ, Settings
from hh_scout.llm.cover_letter import CoverLetterWriter
from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import format_card, format_letter

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = (
    "<b>HH-Scout</b> — лиды с hh.ru для сотрудничества по ИП.\n"
    "Дайджест приходит каждый день в {digest}. Сбор идёт в случайное время в окне {window}.\n\n"
    "/status — состояние: Firefox, мост, последний и следующий сбор, лимиты\n"
    "/digest — прислать накопленные лиды сейчас\n"
    "/crawl — запустить сбор сейчас (30 мин — несколько часов, сериями с паузами)\n"
    "/next — когда следующий сбор\n"
    "/pause · /resume — приостановить/возобновить автоматические сборы\n"
    "/skipped [N] — последние отсеянные вакансии с причинами\n"
    "/letter &lt;hh_id&gt; — переписать отклик для вакансии\n"
    "/help — эта справка"
)


def _fmt_dt(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(raw).astimezone(TZ).strftime("%d.%m %H:%M")
    except ValueError:
        return raw


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@router.message(Command("start", "help"))
async def help_cmd(m: Message, settings: Settings) -> None:
    await m.answer(HELP.format(digest=settings.digest_time, window=settings.crawl_window))


@router.message(Command("status"))
async def status_cmd(m: Message, settings: Settings, conn: sqlite3.Connection, scheduler) -> None:
    ff = _port_open(settings.marionette_host, settings.marionette_port)
    bridge = "недоступен"
    try:
        r = await asyncio.to_thread(httpx.get, f"{settings.bridge_url}/health", timeout=5)
        if r.status_code == 200:
            bridge = f"ok, модель {r.json().get('model')}"
    except Exception:  # noqa: BLE001
        pass
    last = repo.last_run(conn)
    counts = repo.count_by_status(conn)
    used = repo.page_loads_today(conn)
    nxt = scheduler.next_crawl_at()
    db_mb = settings.db_path.stat().st_size / 1e6 if settings.db_path.exists() else 0
    lines = [
        "<b>Состояние HH-Scout</b>",
        f"Firefox/Marionette: {'✅ доступен' if ff else '❌ не отвечает (запустите Firefox с --marionette)'}",
        f"Мост Claude: {bridge}",
        f"Автосбор: {'⏸ пауза' if scheduler.paused() else '▶️ включён'} · сбор идёт: {'да' if scheduler.crawl_lock.locked() else 'нет'}",
        f"Следующий сбор: {nxt.strftime('%d.%m %H:%M') if nxt else '— (назначится после дайджеста в ' + settings.digest_time + ')'}",
        f"Загрузок страниц сегодня: {used} / {settings.max_page_loads_per_run}",
    ]
    if last:
        lines.append(f"Последний прогон: {_fmt_dt(last['started_at'])} → {last['status']} ({last['trigger']}); "
                     f"страниц {last['page_loads'] or 0}, новых {last['collected'] or 0}, оценено {last['evaluated'] or 0}"
                     + (f"; {last['error']}" if last['error'] else ""))
    order = ["new", "triage", "to_fetch", "prefiltered", "evaluated", "sent", "rejected", "skipped", "evaluation_failed"]
    lines.append("Вакансии: " + " · ".join(f"{k} {counts[k]}" for k in order if counts.get(k)))
    lines.append(f"БД: {db_mb:.1f} МБ")
    await m.answer("\n".join(lines))


@router.message(Command("digest"))
async def digest_cmd(m: Message, settings: Settings, conn: sqlite3.Connection) -> None:
    n = await send_digest(m.bot, conn, settings, m.chat.id, note="manual /digest")
    if n:
        await m.answer(f"Отправлено лидов: {n}")


@router.message(Command("crawl"))
async def crawl_cmd(m: Message, scheduler) -> None:
    if scheduler.crawl_lock.locked():
        await m.answer("Сбор уже идёт")
        return
    await scheduler.trigger_manual_crawl()


@router.message(Command("next"))
async def next_cmd(m: Message, scheduler, settings: Settings) -> None:
    nxt = scheduler.next_crawl_at()
    await m.answer(f"Следующий сбор: {nxt.strftime('%d.%m в %H:%M')}" if nxt
                   else f"Сбор пока не назначен — назначится после дайджеста в {settings.digest_time}.")


@router.message(Command("pause"))
async def pause_cmd(m: Message, scheduler) -> None:
    scheduler.set_paused(True)
    await m.answer("⏸ Автоматические сборы приостановлены. /resume — возобновить. Дайджест в 12:00 по-прежнему придёт.")


@router.message(Command("resume"))
async def resume_cmd(m: Message, scheduler) -> None:
    scheduler.set_paused(False)
    await m.answer("▶️ Автоматические сборы возобновлены.")


@router.message(Command("skipped"))
async def skipped_cmd(m: Message, command: CommandObject, conn: sqlite3.Connection) -> None:
    n = int(command.args) if command.args and command.args.isdigit() else 15
    rows = repo.recent_skipped(conn, min(n, 50))
    if not rows:
        await m.answer("Отсеянных пока нет.")
        return
    lines = [f"• <b>{r['title'][:60]}</b> — {r['employer'] or '—'}: {r['status']}/{r['skip_reason'] or ''}"
             + (f" ({r['triage_note']})" if r['triage_note'] and r['skip_reason'] == 'triage' else "") for r in rows]
    await m.answer("Последние отсеянные:\n" + "\n".join(lines))


@router.message(Command("letter"))
async def letter_cmd(m: Message, command: CommandObject, settings: Settings, conn: sqlite3.Connection) -> None:
    hh_id = (command.args or "").strip()
    if not hh_id.isdigit():
        await m.answer("Использование: /letter &lt;hh_id&gt; (число из ссылки hh.ru/vacancy/…)")
        return
    row = repo.lead_by_hh_id(conn, hh_id)
    if row is None:
        await m.answer("Такой оценённой вакансии нет в базе.")
        return
    await m.answer("Пишу отклик…")
    try:
        text = await asyncio.to_thread(CoverLetterWriter(settings, conn).write_for, row)
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не удалось: {e}")
        return
    if not text:
        await m.answer("ИИ вернул текст неподходящей длины, попробуйте ещё раз.")
        return
    row = repo.lead_by_hh_id(conn, hh_id)
    await m.answer(format_card(1, row, row), reply_markup=vote_kb(row["id"]))
    await m.answer(format_letter(row["employer"], text))
